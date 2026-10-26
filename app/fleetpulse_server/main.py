"""HTTP entry point, tailnet-only behind Traefik. Reads need no login (one owner, tailnet allow-list); writes
(Alertmanager webhooks, deploy events, known-cause notes) need the bearer token in FLEET_PULSE_TOKEN, because every tailnet device
can reach this service."""
import hmac
import json
import mimetypes
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import incidents, sources, store

MAX_BODY = 1 << 20
MAX_NOTE = 500
TOKEN = os.environ.get("FLEET_PULSE_TOKEN", "")
RECONCILE_EVERY_S = 300
# The dashboard: a static build (index.html + assets/) served as is; every non-API path gets index.html.
UI_DIR = os.path.realpath(os.environ.get("UI_DIR", "/app/ui"))

snapshot = sources.Snapshot()


class BadRequest(ValueError):
    pass


def summary() -> dict:
    """Everything the dashboard shows, in one call. Prometheus data comes from the background snapshot."""
    snap = snapshot.data
    return {
        "generated_at": store.now(),
        "prometheus": {"error": snap["error"],
                       "age_s": round(time.time() - snap["updated"]) if snap["updated"] else None},
        "open_incidents": store.rows(
            "SELECT id, title, severity, host, opened_at, acked_at, ack_note FROM incidents WHERE closed_at IS NULL "
            "ORDER BY opened_at DESC"),
        "services": snap["services"],
        "slos": snap["slos"],
        "deploys": store.rows(
            "SELECT id, at, app, result, image, rev, reason FROM deploys WHERE result != 'canary_started' "
            "ORDER BY at DESC LIMIT 20"),
        "past_incidents": store.rows(
            "SELECT id, title, severity, host, opened_at, closed_at FROM incidents WHERE closed_at IS NOT NULL "
            "ORDER BY opened_at DESC LIMIT 30"),
    }


def incident_detail(iid: int) -> dict | None:
    found = store.rows("SELECT * FROM incidents WHERE id = ?", (iid,))
    if not found:
        return None
    inc = found[0]
    events = store.rows("SELECT at, kind, text FROM incident_events WHERE incident_id = ? ORDER BY at, id", (iid,))
    return {"incident": {k: v for k, v in inc.items() if k != "postmortem"}, "events": events,
            "postmortem": inc["postmortem"]}


def authorized(header: str | None) -> bool:
    supplied = (header or "").removeprefix("Bearer ").strip()
    return bool(TOKEN) and hmac.compare_digest(supplied.encode(), TOKEN.encode())


class Handler(BaseHTTPRequestHandler):
    server_version = "fleet-pulse"
    timeout = 15  # a slow or stalled client cannot hold a thread forever

    def send(self, code: int, body: str, ctype: str = "text/plain; charset=utf-8") -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, code: int, body) -> None:
        self.send(code, json.dumps(body), "application/json")

    def body_json(self):
        raw = self.headers.get("Content-Length")
        try:
            n = int(raw or 0)
        except ValueError as e:
            raise BadRequest("bad Content-Length") from e
        if n < 0 or n > MAX_BODY:
            raise BadRequest("body missing or too large")
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError as e:
            raise BadRequest(f"invalid JSON: {e}") from e

    def static(self, path: str) -> None:
        target = os.path.realpath(os.path.join(UI_DIR, path.lstrip("/")))
        if not target.startswith(UI_DIR + os.sep) or not os.path.isfile(target):
            target = os.path.join(UI_DIR, "index.html")
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        with open(target, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # Hashed asset names never change; index.html must always be fresh.
        cache = "public, max-age=31536000, immutable" if "/assets/" in target else "no-cache"
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            store.ping()
            return self.send(200, "ok")
        if not path.startswith("/api/"):
            return self.static(path)
        if path == "/api/summary":
            return self.send_json(200, summary())
        m = re.fullmatch(r"/api/incidents/(\d+)", path)
        if m:
            body = incident_detail(int(m.group(1)))
            return self.send_json(200, body) if body else self.send(404, "not found")
        if path == "/api/incidents":
            return self.send_json(200, store.rows("SELECT * FROM incidents ORDER BY id DESC LIMIT 100"))
        if path == "/api/deploys":
            return self.send_json(200, store.rows("SELECT * FROM deploys ORDER BY id DESC LIMIT 100"))
        self.send(404, "not found")

    def do_POST(self):
        ack = re.fullmatch(r"/api/incidents/(\d+)/ack", self.path)
        if self.path not in ("/api/alertmanager", "/api/deploys") and not ack:
            return self.send(404, "not found")
        if not authorized(self.headers.get("Authorization")):
            return self.send(401, "bearer token required")
        try:
            payload = self.body_json()
            if ack:
                note = payload.get("note") if isinstance(payload, dict) else None
                if not isinstance(note, str) or not note.strip() or len(note) > MAX_NOTE:
                    raise BadRequest(f"note is required, at most {MAX_NOTE} characters")
                result = store.ack_incident(int(ack.group(1)), note.strip(), store.now())
                if result == "missing":
                    return self.send(404, "not found")
                if result == "closed":
                    return self.send(409, "incident is already closed")
                return self.send_json(200, {"acked": True})
            if self.path == "/api/alertmanager":
                return self.send_json(200, {"recorded": incidents.handle_webhook(payload)})
            if not isinstance(payload, dict) or not payload.get("app") or not payload.get("result"):
                raise BadRequest("app and result are required")
            return self.send_json(201, {"id": store.add_deploy(payload)})
        except (BadRequest, incidents.InvalidPayload, ValueError) as err:
            return self.send(400, str(err))

    def log_message(self, fmt, *args):  # one line per request on stdout, picked up by promtail
        print(f"{self.address_string()} {fmt % args}", flush=True)


def reconcile_loop() -> None:
    while True:
        time.sleep(RECONCILE_EVERY_S)
        try:
            fixed = incidents.reconcile(sources.active_alert_fingerprints())
            if fixed:
                print(f"reconcile: resolved {fixed} alerts Alertmanager no longer has", flush=True)
        except Exception as e:  # noqa: BLE001 - the sweep must keep running
            print(f"reconcile failed: {e}", flush=True)


def main():
    if not TOKEN:
        raise SystemExit("FLEET_PULSE_TOKEN is not set: writes would be open to every tailnet device")
    store.db()
    threading.Thread(target=snapshot.loop, daemon=True).start()
    threading.Thread(target=reconcile_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()


if __name__ == "__main__":
    main()
