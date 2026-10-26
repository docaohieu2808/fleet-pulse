"""Deploy history: always appended to a local JSONL file, and posted to Fleet Pulse (FLEET_PULSE_URL, empty disables).
Fleet Pulse requires a bearer token for writes: FLEET_PULSE_TOKEN, or the file ~/.config/fleet-pulse/token."""
import json
import os
import pathlib
import time
import urllib.request

STATE = pathlib.Path(os.path.expanduser("~/.local/state/fleet-pulse"))
TOKEN_FILE = pathlib.Path(os.path.expanduser("~/.config/fleet-pulse/token"))


def token() -> str:
    if os.environ.get("FLEET_PULSE_TOKEN"):
        return os.environ["FLEET_PULSE_TOKEN"]
    return TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else ""


def record(event: dict) -> None:
    event = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **event}
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / "deploys.jsonl", "a") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    url = os.environ.get("FLEET_PULSE_URL", "https://pulse.docaohieu.com")
    if url:
        req = urllib.request.Request(
            url.rstrip("/") + "/api/deploys",
            data=json.dumps(event).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token()}"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=10).read()
        except OSError as e:  # history must never block a rollback
            print(f"warn: could not post deploy event to Fleet Pulse: {e}")
