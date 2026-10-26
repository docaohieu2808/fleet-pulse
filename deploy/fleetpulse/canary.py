"""Canary orchestration. Docker, Prometheus and the event sink are passed in, so every failure path is testable.

Results recorded for each deploy:
  promoted        the new version is live
  rolled_back     the canary was judged worse than stable and removed; production never changed
  aborted         the canary could not be judged (never healthy, not routable, controller stopped or failed);
                  it was removed and production never changed
  promote_failed  the canary passed but switching production failed; the previous version was restored,
                  or, if that failed too, the canary keeps serving and the reason says so
"""
import time

from . import analysis

FORCED_STOP = (KeyboardInterrupt, SystemExit)


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def cleanup(ops, cfg: dict) -> list[str]:
    """Take traffic back to stable, then remove the canary. Each step runs even if the other fails."""
    problems = []
    try:
        ops.remove_split(cfg)
    except Exception as e:  # noqa: BLE001 - every failure must be reported, none may skip the next step
        problems.append(f"traffic split NOT removed ({e})")
    try:
        if not ops.remove_canary(cfg):
            problems.append("canary container still present")
    except Exception as e:  # noqa: BLE001
        problems.append(f"canary NOT removed ({e})")
    return problems


def thresholds(cfg: dict) -> dict:
    """Analysis settings, with the minimum canary sample derived from what the synthetic traffic should deliver,
    so a slow endpoint lowers the bar instead of producing a false 'not enough requests'."""
    a = dict(cfg["analysis"])
    s = cfg["synthetic"]
    expected = s["rps"] * a["weight"] / 100 * a["duration_s"]
    a.setdefault("min_canary_requests", max(30, int(expected * 0.5)))
    a.setdefault("early_fail_min_requests", 20)
    a.setdefault("early_fail_error_ratio", 0.5)
    return a


def run(cfg: dict, image_ref: str, app_dir: str, base_event: dict, *, ops, prom, events, traffic_cls,
        sleep=time.sleep, clock=time.monotonic) -> str:
    a = thresholds(cfg)
    c = cfg["canary"]
    traffic = None

    def finish(result: str, reason: str, **detail) -> str:
        events.record({**base_event, "result": result, "reason": reason, **detail})
        log(f"{result.upper()}: {reason}")
        return result

    def abort(reason: str) -> str:
        problems = cleanup(ops, cfg)
        return finish("aborted", "; ".join([reason, *problems]))

    verdict, reason, canary, stable = "wait", "", {}, {}
    try:
        ops.start_canary(cfg, image_ref, app_dir)
        if not ops.wait_healthy(c["name"], c["networks"][0], c["port"], c["health_path"]):
            return abort("canary never became healthy")
        if not ops.wait_routable(cfg):
            return abort("canary healthy but not reachable through Traefik")

        ops.write_split(cfg, a["weight"])
        started = clock()
        s = cfg["synthetic"]
        traffic = traffic_cls(s["base"], s["paths"], s["rps"])
        traffic.start()
        events.record({**base_event, "result": "canary_started", "weight": a["weight"]})
        log(f"canary live at {a['weight']}%, judging for {a['duration_s']}s (needs {a['min_canary_requests']} requests)")

        while verdict == "wait":
            sleep(a["check_every_s"])
            elapsed = int(clock() - started)
            canary = prom.service_stats(cfg["traefik_instance"], f"{c['service']}@docker", elapsed)
            stable = prom.service_stats(cfg["traefik_instance"], cfg["stable_service"], elapsed)
            verdict, reason = analysis.judge(canary, stable, a, final=elapsed >= a["duration_s"])
            log(f"{elapsed:>4}s {verdict}: {reason}")
    except BaseException as e:
        if traffic:
            traffic.stop()
        why = "controller stopped" if isinstance(e, FORCED_STOP) else f"controller error: {e}"
        abort(why)
        raise
    traffic.stop()

    detail = {"canary": canary, "stable": stable, "synthetic_sent": traffic.sent}
    if verdict == "fail":
        problems = cleanup(ops, cfg)
        return finish("rolled_back", "; ".join([reason, *problems]), **detail)
    return promote(cfg, image_ref, app_dir, reason, detail, ops=ops, finish=finish)


def promote(cfg: dict, image_ref: str, app_dir: str, reason: str, detail: dict, *, ops, finish) -> str:
    """All traffic to the canary, recreate stable on the new image, check it through Traefik, hand traffic back.
    The previous image is kept as :previous and restored if any step fails."""
    stable_ref = f"{cfg['image']}:{cfg['stable_tag']}"
    previous_ref = f"{cfg['image']}:previous"
    previous = ops.image_id(stable_ref)
    if previous:
        ops.sh("docker", "tag", stable_ref, previous_ref)
    try:
        ops.write_split(cfg, 100)
        if not ops.run_stable(cfg, image_ref, app_dir):
            raise RuntimeError("new stable container did not become healthy")
        ops.remove_split(cfg)
        if not ops.wait_routable(cfg, host=cfg["route"]["host"]):
            raise RuntimeError("new stable not reachable through Traefik after the switch")
        ops.remove_canary(cfg)
        return finish("promoted", reason, previous_image=previous, **detail)
    except BaseException as e:
        outcome = restore(cfg, app_dir, previous_ref if previous else None, ops=ops)
        finish("promote_failed", f"{e}; {outcome}", previous_image=previous, **detail)
        if isinstance(e, FORCED_STOP):
            raise
        return "promote_failed"


def restore(cfg: dict, app_dir: str, previous_ref: str | None, *, ops) -> str:
    """Bring back the previous stable version. If that is impossible, leave all traffic on the canary (still
    running with the version that passed) rather than on a broken stable."""
    try:
        if previous_ref and ops.run_stable(cfg, previous_ref, app_dir):
            ops.remove_split(cfg)
            if ops.wait_routable(cfg, host=cfg["route"]["host"]):
                ops.remove_canary(cfg)
                return "previous version restored"
    except Exception as e:  # noqa: BLE001
        log(f"restore failed: {e}")
    try:
        ops.write_split(cfg, 100)
    except Exception as e:  # noqa: BLE001
        return f"RESTORE FAILED and traffic split could not be written ({e}): fix by hand"
    return "RESTORE FAILED: the canary is serving 100% of traffic; fix stable by hand, then --abort"
