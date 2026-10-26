import os
import tempfile
import threading
import unittest
from unittest import mock

os.environ["FLEET_PULSE_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")

from fleetpulse_server import incidents, sources, store  # noqa: E402


def alert(status, name="EndpointDown", fp="a1", sev="critical", app="covumon", starts="2026-10-24T10:00:00.5Z",
          ends="2026-10-24T10:12:00Z"):
    return {"status": status, "fingerprint": fp, "startsAt": starts, "endsAt": ends,
            "labels": {"alertname": name, "severity": sev, "app": app, "host": "loadbalancer"},
            "annotations": {"summary": "down"}}


def hook(*alerts):
    return incidents.handle_webhook({"alerts": list(alerts)})


def open_keys():
    return [r["key"] for r in store.rows("SELECT key FROM incidents WHERE closed_at IS NULL")]


@mock.patch.object(sources, "rule_wait_seconds", return_value=120)
@mock.patch.object(sources, "budget_burned", return_value=[("availability", 0.5, 0.14)])
@mock.patch.object(sources, "failed_requests", return_value=[])
class IncidentTest(unittest.TestCase):
    def setUp(self):
        for t in ("incident_events", "incident_alerts", "incidents", "deploys"):
            store.write(f"DELETE FROM {t}")

    def test_open_then_close_writes_postmortem_from_failure_start(self, *_):
        hook(alert("firing"))
        inc = store.open_incident("covumon")
        self.assertEqual(inc["opened_at"], "2026-10-24T17:00:00+0700")
        hook(alert("resolved"))
        self.assertIsNone(store.open_incident("covumon"))
        pm = store.rows("SELECT postmortem FROM incidents")[0]["postmortem"]
        # The rule waited 2 min: failure began 16:58, resolved 17:12 -> 14 min.
        self.assertIn("Failure began about 16:58", pm)
        self.assertIn("about 14 min", pm)
        self.assertIn("14.0% of the 30-day error budget", pm)

    def test_stays_open_while_another_alert_fires(self, *_):
        hook(alert("firing", fp="a1"), alert("firing", name="SLOBurn", fp="a2"))
        hook(alert("resolved", fp="a1"))
        self.assertEqual(open_keys(), ["covumon"])
        hook(alert("resolved", name="SLOBurn", fp="a2"))
        self.assertEqual(open_keys(), [])

    def test_known_cause_note_lands_in_the_postmortem(self, *_):
        hook(alert("firing"))
        iid = store.open_incident("covumon")["id"]
        self.assertEqual(store.ack_incident(iid, "drill: web stopped on purpose", "2026-10-24T17:05:00+0700"), "ok")
        hook(alert("resolved"))
        pm = store.rows("SELECT postmortem FROM incidents")[0]["postmortem"]
        self.assertIn("Noted while open (17:05): drill: web stopped on purpose", pm)
        self.assertEqual(store.ack_incident(iid, "late", store.now()), "closed")
        self.assertEqual(store.ack_incident(iid + 99, "nope", store.now()), "missing")

    def test_a_new_alert_makes_a_known_incident_unexplained_again(self, *_):
        hook(alert("firing", fp="a1"))
        iid = store.open_incident("covumon")["id"]
        store.ack_incident(iid, "known", store.now())
        hook(alert("firing", fp="a1"))  # a repeat changes nothing
        self.assertIsNotNone(store.open_incident("covumon")["acked_at"])
        hook(alert("firing", name="SLOBurn", fp="a2"))
        self.assertIsNone(store.open_incident("covumon")["acked_at"])

    def test_repeated_firing_notifications_add_nothing(self, *_):
        for _ in range(3):
            hook(alert("firing"))
        self.assertEqual(len(store.rows("SELECT * FROM incident_events WHERE kind = 'firing'")), 1)

    def test_payload_order_does_not_matter(self, *_):
        hook(alert("firing", fp="a1"))
        hook(alert("resolved", fp="a1"), alert("firing", name="SLOBurn", fp="a2"))
        self.assertEqual(open_keys(), ["covumon"])
        self.assertEqual(len(store.rows("SELECT * FROM incidents")), 1)

    def test_late_firing_after_resolved_does_not_reopen(self, *_):
        hook(alert("firing"))
        hook(alert("resolved"))
        hook(alert("firing"))  # the same episode, delivered late
        self.assertEqual(open_keys(), [])
        self.assertEqual(len(store.rows("SELECT * FROM incidents")), 1)

    def test_a_new_episode_after_close_opens_a_new_incident(self, *_):
        hook(alert("firing"))
        hook(alert("resolved"))
        hook(alert("firing", starts="2026-10-24T11:00:00Z", ends="0001-01-01T00:00:00Z"))
        self.assertEqual(open_keys(), ["covumon"])

    def test_concurrent_webhooks_open_one_incident(self, *_):
        for _ in range(30):
            store.write("DELETE FROM incident_events"); store.write("DELETE FROM incident_alerts")
            store.write("DELETE FROM incidents")
            barrier = threading.Barrier(2)

            def send(fp, name):
                barrier.wait()
                hook(alert("firing", fp=fp, name=name))

            ts = [threading.Thread(target=send, args=("a1", "EndpointDown")),
                  threading.Thread(target=send, args=("a2", "SLOBurn"))]
            [t.start() for t in ts]
            [t.join() for t in ts]
            self.assertEqual(len(store.rows("SELECT * FROM incidents")), 1)

    def test_malformed_payload_writes_nothing(self, *_):
        with self.assertRaises(incidents.InvalidPayload):
            hook(alert("firing", fp="a1"), {"status": "firing", "labels": {}})
        self.assertEqual(store.rows("SELECT count(*) AS n FROM incidents")[0]["n"], 0)

    def test_plain_warning_does_not_open_incident(self, *_):
        hook(alert("firing", name="HighDiskUsage", sev="warning"))
        self.assertIsNone(store.open_incident("covumon"))

    def test_lost_resolved_notification_is_reconciled(self, *_):
        hook(alert("firing", fp="a1"))
        self.assertEqual(incidents.reconcile({"a1"}), 0)
        self.assertEqual(incidents.reconcile(None), 0)  # Alertmanager unreachable: change nothing
        self.assertEqual(incidents.reconcile(set()), 1)
        self.assertEqual(open_keys(), [])

    def test_recent_promoted_deploy_is_put_on_timeline(self, *_):
        store.add_deploy({"app": "covumon", "result": "promoted", "image": "x:c1", "rev": "abc",
                          "at": "2026-10-24T09:30:00+0000"})  # 16:30 local, 30 min before
        hook(alert("firing"))
        kinds = [r["kind"] for r in store.rows("SELECT kind FROM incident_events")]
        self.assertIn("deploy", kinds)

    def test_rolled_back_or_old_deploys_are_not_suspects(self, *_):
        store.add_deploy({"app": "covumon", "result": "rolled_back", "image": "x:c2", "rev": "bad",
                          "at": "2026-10-24T09:40:00+0000"})
        store.add_deploy({"app": "covumon", "result": "promoted", "image": "x:c0", "rev": "old",
                          "at": "2026-10-24T05:00:00+0000"})
        hook(alert("firing"))
        kinds = [r["kind"] for r in store.rows("SELECT kind FROM incident_events")]
        self.assertNotIn("deploy", kinds)

    def test_deploy_time_without_offset_is_rejected(self, *_):
        with self.assertRaises(ValueError):
            store.add_deploy({"app": "covumon", "result": "promoted", "at": "2026-10-24T09:40:00"})


class SafeLabelTest(unittest.TestCase):
    def test_label_injection_is_refused(self):
        self.assertEqual(sources.safe("job-radar"), "job-radar")
        self.assertIsNone(sources.safe('x"} or vector(1) #'))
        self.assertIsNone(sources.safe('lb"} # '))
        self.assertEqual(sources.failed_requests('lb"} #', "covumon", "2026-10-24T10:00:00+0700",
                                                 "2026-10-24T10:10:00+0700"), [])


if __name__ == "__main__":
    unittest.main()
