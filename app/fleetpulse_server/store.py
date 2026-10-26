"""SQLite storage for deploys, incidents, their alerts and timelines. One file, one process; every statement runs
under one lock, and multi-step changes (the webhook) take the incident lock in incidents.py."""
import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

DB_PATH = os.environ.get("FLEET_PULSE_DB", "/data/fleet-pulse.db")
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS deploys (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  app TEXT NOT NULL,
  result TEXT NOT NULL,          -- canary_started | promoted | rolled_back | aborted | promote_failed | refused
  image TEXT, rev TEXT, reason TEXT,
  detail TEXT                    -- JSON: canary/stable stats, previous image
);
CREATE INDEX IF NOT EXISTS deploys_app_at ON deploys(app, at);

CREATE TABLE IF NOT EXISTS incidents (
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL,             -- what the incident is about: app, else alert instance
  title TEXT NOT NULL,
  severity TEXT NOT NULL,
  host TEXT,
  opened_at TEXT NOT NULL,       -- when the first alert fired
  closed_at TEXT,
  postmortem TEXT                -- markdown draft, written when the incident closes
);
-- At most one open incident per key, enforced by the database, not only by the code.
CREATE UNIQUE INDEX IF NOT EXISTS incidents_one_open ON incidents(key) WHERE closed_at IS NULL;

-- State of each alert inside an incident, so repeated or out-of-order notifications change nothing.
CREATE TABLE IF NOT EXISTS incident_alerts (
  incident_id INTEGER NOT NULL REFERENCES incidents(id),
  fingerprint TEXT NOT NULL,
  state TEXT NOT NULL,           -- firing | resolved
  starts_at TEXT NOT NULL,
  ends_at TEXT,
  PRIMARY KEY (incident_id, fingerprint)
);

CREATE TABLE IF NOT EXISTS incident_events (
  id INTEGER PRIMARY KEY,
  incident_id INTEGER NOT NULL REFERENCES incidents(id),
  at TEXT NOT NULL,
  kind TEXT NOT NULL,            -- firing | resolved | deploy | note (the owner's known-cause note)
  text TEXT NOT NULL,
  fingerprint TEXT
);
CREATE INDEX IF NOT EXISTS incident_events_incident ON incident_events(incident_id, at);
"""

LOCAL = timezone(timedelta(hours=7))
FMT = "%Y-%m-%dT%H:%M:%S%z"


def now() -> str:
    return datetime.now(LOCAL).strftime(FMT)


def local(ts: str | None) -> str:
    """All stored times share one offset, so plain string comparison orders them correctly.
    Raises ValueError for a time without an offset: guessing the zone would misorder events."""
    if not ts:
        return now()
    return datetime.strptime(ts, FMT).astimezone(LOCAL).strftime(FMT)


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(incidents)")}
    if "detected_after_s" not in cols:
        # How long the alert rule waits (its `for:`) before firing: the failure began about this much earlier.
        conn.execute("ALTER TABLE incidents ADD COLUMN detected_after_s INTEGER NOT NULL DEFAULT 0")
    if "acked_at" not in cols:
        # The owner looked at an open incident and wrote down the cause: shown grey on the page, kept in the postmortem.
        conn.execute("ALTER TABLE incidents ADD COLUMN acked_at TEXT")
        conn.execute("ALTER TABLE incidents ADD COLUMN ack_note TEXT")
    # Incidents opened before alert state was tracked: carry their open alerts over from the event log.
    conn.execute("""
      INSERT OR IGNORE INTO incident_alerts (incident_id, fingerprint, state, starts_at, ends_at)
      SELECT e.incident_id, e.fingerprint, e.kind, e.at, CASE WHEN e.kind = 'resolved' THEN e.at END
        FROM incident_events e
       WHERE e.fingerprint IS NOT NULL
         AND e.id = (SELECT max(id) FROM incident_events x WHERE x.incident_id = e.incident_id AND x.fingerprint = e.fingerprint)
    """)


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    with conn:
        _migrate(conn)
    return conn


_conn = None


def db() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            _conn = connect()
        return _conn


def write(sql: str, args: tuple = ()) -> int:
    with _lock, db():
        return db().execute(sql, args).lastrowid


def rows(sql: str, args: tuple = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in db().execute(sql, args).fetchall()]


def ping() -> None:
    with _lock:
        db().execute("SELECT 1").fetchone()


DEPLOY_RESULTS = {"canary_started", "promoted", "rolled_back", "aborted", "promote_failed", "refused"}


def add_deploy(e: dict) -> int:
    if e.get("result") not in DEPLOY_RESULTS:
        raise ValueError(f"unknown result {e.get('result')!r}")
    detail = {k: e[k] for k in ("canary", "stable", "synthetic_sent", "weight", "previous_image") if k in e}
    return write(
        "INSERT INTO deploys(at, app, result, image, rev, reason, detail) VALUES (?,?,?,?,?,?,?)",
        (local(e.get("at")), str(e["app"]), e["result"], e.get("image"), e.get("rev"), e.get("reason"),
         json.dumps(detail) if detail else None),
    )


def open_incident(key: str) -> dict | None:
    r = rows("SELECT * FROM incidents WHERE key = ? AND closed_at IS NULL", (key,))
    return r[0] if r else None


def create_incident(key: str, title: str, severity: str, host: str | None, at: str, detected_after_s: int) -> int:
    return write("INSERT INTO incidents(key, title, severity, host, opened_at, detected_after_s) VALUES (?,?,?,?,?,?)",
                 (key, title, severity, host, at, detected_after_s))


def alert_state(incident_id: int, fingerprint: str) -> dict | None:
    r = rows("SELECT * FROM incident_alerts WHERE incident_id = ? AND fingerprint = ?", (incident_id, fingerprint))
    return r[0] if r else None


def set_alert(incident_id: int, fingerprint: str, state: str, starts_at: str, ends_at: str | None) -> None:
    write("""INSERT INTO incident_alerts (incident_id, fingerprint, state, starts_at, ends_at) VALUES (?,?,?,?,?)
             ON CONFLICT(incident_id, fingerprint) DO UPDATE
             SET state = excluded.state, starts_at = excluded.starts_at, ends_at = excluded.ends_at""",
          (incident_id, fingerprint, state, starts_at, ends_at))


def firing_count(incident_id: int) -> int:
    return rows("SELECT count(*) AS n FROM incident_alerts WHERE incident_id = ? AND state = 'firing'",
                (incident_id,))[0]["n"]


def add_event(incident_id: int, at: str, kind: str, text: str, fingerprint: str | None = None) -> None:
    write("INSERT INTO incident_events(incident_id, at, kind, text, fingerprint) VALUES (?,?,?,?,?)",
          (incident_id, at, kind, text, fingerprint))


def close_incident(incident_id: int, at: str, postmortem: str) -> None:
    write("UPDATE incidents SET closed_at = ?, postmortem = ? WHERE id = ?", (at, postmortem, incident_id))


def ack_incident(incident_id: int, note: str, at: str) -> str:
    """Record that the cause of an open incident is known. Returns 'ok', 'missing' or 'closed'."""
    with _lock, db():
        found = db().execute("SELECT closed_at FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        if found is None:
            return "missing"
        if found["closed_at"] is not None:
            return "closed"
        db().execute("UPDATE incidents SET acked_at = ?, ack_note = ? WHERE id = ?", (at, note, incident_id))
        db().execute("INSERT INTO incident_events(incident_id, at, kind, text) VALUES (?,?,?,?)",
                     (incident_id, at, "note", f"Cause known: {note}"))
        return "ok"


def clear_ack(incident_id: int) -> None:
    write("UPDATE incidents SET acked_at = NULL WHERE id = ? AND acked_at IS NOT NULL", (incident_id,))
