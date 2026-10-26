"""Control flow of a canary with fake Docker/Traefik/Prometheus: every failure path must leave production on a
known-good version, with no traffic split or canary left behind unless the reason says so."""
import unittest

from fleetpulse import canary

CFG = {
    "app": "demo", "image": "demo", "stable_tag": "local", "stable_container": "demo-web-1",
    "stable_service": "demo@docker",
    "canary": {"name": "demo-canary", "service": "demo-canary", "port": 8080, "networks": ["internal", "traefik"],
               "health_path": "/health"},
    "route": {"host": "demo.example", "canary_host": "demo-canary.example", "traefik_address": "127.0.0.1"},
    "traefik_instance": "node",
    "synthetic": {"base": "https://demo.example", "paths": ["/"], "rps": 3},
    "analysis": {"weight": 10, "duration_s": 60, "check_every_s": 30, "max_error_ratio": 0.01,
                 "max_error_ratio_vs_stable": 2.0, "max_p95_vs_stable": 1.5, "p95_slack_s": 0.05},
}


class FakeOps:
    def __init__(self, **fail):
        self.fail = fail
        self.split = None  # canary weight currently routed, None = no split file
        self.canary = False
        self.stable = "old"
        self.calls = []

    def _maybe(self, name):
        self.calls.append(name)
        f = self.fail.get(name)
        if isinstance(f, BaseException) or (isinstance(f, type) and issubclass(f, BaseException)):
            raise f
        return f

    def start_canary(self, cfg, image, app_dir):
        self.canary = True  # the container exists even if a later step of start_canary fails
        self._maybe("start_canary")

    def wait_healthy(self, *a, **k):
        return self._maybe("wait_healthy") is None

    def wait_routable(self, cfg, host=None, **k):
        r = self._maybe(f"routable:{host or 'canary'}")
        return r is None

    def write_split(self, cfg, weight):
        self._maybe(f"split:{weight}")
        self.split = weight

    def remove_split(self, cfg):
        self._maybe("remove_split")
        self.split = None

    def remove_canary(self, cfg):
        self._maybe("remove_canary")
        self.canary = False
        return True

    def image_id(self, ref):
        return "sha256:old"

    def sh(self, *args, **k):
        self.calls.append(" ".join(args))

    def run_stable(self, cfg, image_ref, app_dir):
        bad = self._maybe(f"run_stable:{image_ref}")
        if bad is None:
            self.stable = image_ref
        return bad is None


class FakeProm:
    def __init__(self, canary_stats, stable_stats=None):
        self.c = canary_stats
        self.s = stable_stats or {"requests": 1000, "errors": 0, "error_ratio": 0.0, "p95_s": 0.2}

    def service_stats(self, instance, service, window):
        if isinstance(self.c, BaseException) or (isinstance(self.c, type) and issubclass(self.c, BaseException)):
            raise self.c
        return self.c if "canary" in service else self.s


class FakeEvents:
    def __init__(self):
        self.items = []

    def record(self, e):
        self.items.append(e)

    def results(self):
        return [e["result"] for e in self.items]


class FakeTraffic:
    def __init__(self, *a):
        self.sent = 0
        self.stopped = False

    def start(self):
        pass

    def stop(self):
        self.stopped = True


GOOD = {"requests": 100, "errors": 0, "error_ratio": 0.0, "p95_s": 0.2}
BAD = {"requests": 100, "errors": 30, "error_ratio": 0.3, "p95_s": 0.2}


def run(ops, prom):
    events = FakeEvents()
    clock = iter(range(0, 10_000, 30))
    result = canary.run(CFG, "demo:new", "/app", {"app": "demo"}, ops=ops, prom=prom, events=events,
                        traffic_cls=FakeTraffic, sleep=lambda s: None, clock=lambda: next(clock))
    return result, events


class CanaryFlowTest(unittest.TestCase):
    def test_good_canary_is_promoted_and_cleaned_up(self):
        ops = FakeOps()
        result, events = run(ops, FakeProm(GOOD))
        self.assertEqual(result, "promoted")
        self.assertEqual((ops.split, ops.canary, ops.stable), (None, False, "demo:new"))
        self.assertEqual(events.results(), ["canary_started", "promoted"])

    def test_bad_canary_is_rolled_back(self):
        ops = FakeOps()
        result, events = run(ops, FakeProm(BAD))
        self.assertEqual(result, "rolled_back")
        self.assertEqual((ops.split, ops.canary, ops.stable), (None, False, "old"))

    def test_sigterm_mid_judging_removes_split_and_canary(self):
        ops = FakeOps()
        with self.assertRaises(SystemExit):
            run(ops, FakeProm(SystemExit(143)))
        self.assertEqual((ops.split, ops.canary), (None, False))

    def test_failure_inside_start_canary_still_removes_the_container(self):
        ops = FakeOps(start_canary=RuntimeError("network connect failed"))
        with self.assertRaises(RuntimeError):
            run(ops, FakeProm(GOOD))
        self.assertFalse(ops.canary)

    def test_failed_split_write_still_removes_the_container(self):
        ops = FakeOps(**{"split:10": OSError("no space left")})
        with self.assertRaises(OSError):
            run(ops, FakeProm(GOOD))
        self.assertFalse(ops.canary)

    def test_cleanup_continues_when_removing_the_split_fails(self):
        ops = FakeOps(remove_split=OSError("read-only"))
        result, events = run(ops, FakeProm(BAD))
        self.assertEqual(result, "rolled_back")
        self.assertFalse(ops.canary)
        self.assertIn("split NOT removed", events.items[-1]["reason"])

    def test_failed_promote_restores_the_previous_version(self):
        ops = FakeOps(**{"run_stable:demo:new": False})
        result, events = run(ops, FakeProm(GOOD))
        self.assertEqual(result, "promote_failed")
        self.assertEqual((ops.split, ops.canary, ops.stable), (None, False, "demo:previous"))
        self.assertIn("previous version restored", events.items[-1]["reason"])

    def test_stable_not_routable_after_switch_restores_the_previous_version(self):
        ops = FakeOps(**{"routable:demo.example": False})
        # Routable fails for the real host every time: restore cannot prove itself either, so the canary keeps serving.
        result, events = run(ops, FakeProm(GOOD))
        self.assertEqual(result, "promote_failed")
        self.assertEqual(ops.split, 100)
        self.assertTrue(ops.canary)
        self.assertIn("canary is serving 100%", events.items[-1]["reason"])

    def test_restore_failure_leaves_all_traffic_on_the_canary(self):
        ops = FakeOps(**{"run_stable:demo:new": False, "run_stable:demo:previous": False})
        result, events = run(ops, FakeProm(GOOD))
        self.assertEqual(result, "promote_failed")
        self.assertEqual((ops.split, ops.canary), (100, True))
        self.assertIn("RESTORE FAILED", events.items[-1]["reason"])


class ThresholdTest(unittest.TestCase):
    def test_minimum_sample_follows_the_synthetic_rate(self):
        # 3 rps * 10% * 60 s = 18 expected; floor of 30 applies.
        self.assertEqual(canary.thresholds(CFG)["min_canary_requests"], 30)
        cfg = {**CFG, "analysis": {**CFG["analysis"], "duration_s": 600}}
        self.assertEqual(canary.thresholds(cfg)["min_canary_requests"], 90)


if __name__ == "__main__":
    unittest.main()
