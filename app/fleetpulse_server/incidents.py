"""Turn Alertmanager webhooks into incidents with a timeline, and draft a blameless postmortem on close.

Alertmanager delivers at least once, repeats firing alerts, may send different groups for the same app at the same
moment, and can lose a notification while this service is down. So:
  - one lock serialises every webhook and sweep (check-then-create cannot race), backed by a unique index;
  - each alert's state is tracked per incident, so repeats and late, out-of-order notifications change nothing;
  - a whole payload is validated before anything is written;
  - a periodic sweep against Alertmanager resolves alerts whose "resolved" notification never arrived.
"""
import threading
from datetime import datetime, timedelta, timezone

from . import sources, store

LOCAL = timezone(timedelta(hours=7))
FMT = "%Y-%m-%dT%H:%M:%S%z"
LOCK = threading.Lock()


class InvalidPayload(ValueError):
    pass


def to_local(ts: str) -> str:
    """Alertmanager sends RFC3339 with fractions ('...T09:55:00.12Z'); store second precision, local time."""
    if not ts or ts.startswith("0001"):
        return store.now()
    ts = ts.replace("Z", "+00:00")
    if "." in ts:
        head, rest = ts.split(".", 1)
        tz = rest[next((i for i, ch in enumerate(rest) if ch in "+-"), len(rest)):]
        ts = head + tz
    return datetime.fromisoformat(ts).astimezone(LOCAL).strftime(FMT)


def opens_incident(labels: dict) -> bool:
    """Pages and SLO burns open incidents. Plain warnings (CPU, disk at 80%) stay in email only."""
    return labels.get("severity") == "critical" or labels.get("category") == "slo"


def incident_key(labels: dict) -> str:
    return labels.get("app") or labels.get("sloth_service") or labels.get("instance") or labels["alertname"]


def describe(alert: dict) -> str:
    labels, ann = alert["labels"], alert.get("annotations") or {}
    text = ann.get("summary") or ann.get("description") or labels["alertname"]
    return f"{labels['alertname']} ({labels.get('severity', '?')}): {text}"


def validate(payload) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("alerts", []), list):
        raise InvalidPayload("expected an Alertmanager webhook object with an alerts list")
    alerts = payload.get("alerts", [])
    for i, a in enumerate(alerts):
        if not isinstance(a, dict) or not isinstance(a.get("labels"), dict) or not a["labels"].get("alertname"):
            raise InvalidPayload(f"alert {i}: labels with an alertname are required")
        if a.get("status") not in ("firing", "resolved"):
            raise InvalidPayload(f"alert {i}: status must be firing or resolved")
        if not a.get("fingerprint"):
            raise InvalidPayload(f"alert {i}: fingerprint is required")
        try:
            to_local(a.get("startsAt", ""))
            if a["status"] == "resolved":
                to_local(a.get("endsAt", ""))
        except ValueError as e:
            raise InvalidPayload(f"alert {i}: bad timestamp ({e})") from e
    # Firing before resolved: "[resolved a1, firing a2]" must not close the incident a2 keeps open.
    return sorted(alerts, key=lambda a: a["status"] != "firing")


def handle_webhook(payload) -> int:
    """Apply one Alertmanager webhook. Returns how many alert changes were recorded. Raises InvalidPayload."""
    alerts = validate(payload)
    with LOCK:
        return sum(apply(a) for a in alerts)


def apply(alert: dict) -> int:
    labels = alert["labels"]
    key = incident_key(labels)
    fp = alert["fingerprint"]
    starts = to_local(alert.get("startsAt", ""))
    inc = store.open_incident(key)

    if alert["status"] == "firing":
        if inc:
            known = store.alert_state(inc["id"], fp)
            if known and known["state"] == "firing":
                return 0  # a repeat notification
            if known and known["state"] == "resolved" and (known["ends_at"] or "") >= starts:
                return 0  # a late firing for an episode that already ended
            # Something new fired: the cause the owner noted may not cover it, so the incident is unexplained again.
            store.clear_ack(inc["id"])
        else:
            if not opens_incident(labels):
                return 0
            last = store.rows("""SELECT max(ends_at) AS ended FROM incident_alerts
                                  WHERE fingerprint = ? AND state = 'resolved'""", (fp,))[0]["ended"]
            if last and last >= starts:
                return 0  # a late firing for an episode whose incident is already closed
            iid = store.create_incident(key, f"{key}: {labels['alertname']}", labels.get("severity", "critical"),
                                        labels.get("host"), starts, sources.rule_wait_seconds(labels["alertname"]))
            attach_recent_deploys(iid, key, starts)
            inc = {"id": iid}
        store.set_alert(inc["id"], fp, "firing", starts, None)
        store.add_event(inc["id"], starts, "firing", describe(alert), fp)
        return 1

    if not inc:
        return 0
    known = store.alert_state(inc["id"], fp)
    if not known or known["state"] != "firing":
        return 0  # resolved for an alert this incident never tracked, or a repeat
    ends = to_local(alert.get("endsAt", ""))
    resolve(inc["id"], fp, known["starts_at"], ends, describe(alert))
    return 1


def resolve(incident_id: int, fp: str, starts: str, ends: str, text: str) -> None:
    store.set_alert(incident_id, fp, "resolved", starts, ends)
    store.add_event(incident_id, ends, "resolved", text, fp)
    if store.firing_count(incident_id) == 0:
        close(incident_id, ends)


def reconcile(active_fingerprints: set[str] | None) -> int:
    """Resolve alerts that Alertmanager no longer has active: their resolved notification was lost.
    `None` (Alertmanager unreachable) changes nothing."""
    if active_fingerprints is None:
        return 0
    fixed = 0
    with LOCK:
        for a in store.rows("""SELECT a.incident_id, a.fingerprint, a.starts_at FROM incident_alerts a
                                 JOIN incidents i ON i.id = a.incident_id
                                WHERE i.closed_at IS NULL AND a.state = 'firing'"""):
            if a["fingerprint"] not in active_fingerprints:
                resolve(a["incident_id"], a["fingerprint"], a["starts_at"], store.now(),
                        "no longer active in Alertmanager (resolved notification not received)")
                fixed += 1
    return fixed


def attach_recent_deploys(incident_id: int, app: str, opened_at: str) -> None:
    """A deploy shortly before an incident is the first suspect; put it on the timeline. Only promoted deploys
    changed production: a rolled-back or aborted canary never touched the stable version."""
    since = (datetime.strptime(opened_at, FMT) - timedelta(hours=2)).strftime(FMT)
    for d in store.rows("SELECT * FROM deploys WHERE app = ? AND result = 'promoted' AND at >= ? AND at <= ? ORDER BY at",
                        (app, since, opened_at)):
        store.add_event(incident_id, d["at"], "deploy", f"deploy {d['result']}: {d['image']} ({d['rev']}) {d['reason'] or ''}")


def close(incident_id: int, at: str) -> None:
    inc = store.rows("SELECT * FROM incidents WHERE id = ?", (incident_id,))[0]
    events = store.rows("SELECT * FROM incident_events WHERE incident_id = ? ORDER BY at, id", (incident_id,))
    store.close_incident(incident_id, at, postmortem(inc, events, at))


def postmortem(inc: dict, events: list[dict], closed_at: str) -> str:
    fired = datetime.strptime(inc["opened_at"], FMT)
    wait = inc.get("detected_after_s") or 0
    # The rule waited `wait` seconds before firing, so the failure began about that much earlier.
    began = (fired - timedelta(seconds=wait)).strftime(FMT)
    end = datetime.strptime(closed_at, FMT)
    minutes = max(1, round((end - (fired - timedelta(seconds=wait))).total_seconds() / 60))
    lines = [
        f"# Postmortem draft: {inc['title']}",
        "",
        "Blameless: this is about what the system and process allowed, not who did what.",
        "",
        "## Summary",
        f"- Failure began about {began[11:16]} (the alert rule waits {wait // 60} min before firing), "
        f"alert fired {inc['opened_at'][11:16]}, resolved {closed_at[11:16]}: about {minutes} min.",
        f"- Severity: {inc['severity']}. Host: {inc['host'] or 'n/a'}.",
        "",
        "## Impact",
    ]
    burned = sources.budget_burned(inc["key"], began, closed_at)
    if burned is None:
        lines.append("- Prometheus could not be reached when the incident closed; impact not measured.")
    elif burned:
        for slo, ratio, share in burned:
            if ratio is None:
                lines.append(f"- SLO {slo}: no data for the incident window.")
            else:
                lines.append(f"- SLO {slo}: {ratio:.2%} of events failed during the incident, "
                             f"about {share:.1%} of the 30-day error budget.")
    else:
        lines.append("- No SLO is defined for this target; impact not measured.")
    lines += ["", "## Timeline"]
    lines += [f"- {e['at'][11:19]} {e['kind']}: {e['text']}" for e in events]
    if any(e["kind"] == "deploy" for e in events):
        lines += ["", "A deploy happened within 2 hours before the start: check it first."]
    sample = sources.failed_requests(inc["host"], inc["key"], began, closed_at)
    lines += ["", "## Evidence: failed requests (Traefik access log)"]
    lines += [f"    {s}" for s in sample] if sample else ["- None found for this app in the window."]
    lines += [
        "",
        "## Root cause",
        *([f"- Noted while open ({inc['acked_at'][11:16]}): {inc['ack_note']}"] if inc.get("ack_note") else []),
        "- TODO (owner): what broke, and why the system allowed it.",
        "",
        "## What went well / what did not",
        "- TODO",
        "",
        "## Action items",
        "- TODO: each item has an owner and a date; prefer fixes that make the failure impossible over reminders.",
    ]
    return "\n".join(lines) + "\n"
