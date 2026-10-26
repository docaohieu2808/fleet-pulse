"""Synthetic read-only traffic so a low-traffic app gives the canary enough requests to judge."""
import itertools
import threading
import time
import urllib.request

USER_AGENT = "fleet-pulse-canary/1.0 (synthetic traffic during a deploy)"
WORKERS = 4


class SyntheticTraffic:
    """A few worker threads share one pacing clock, so a slow endpoint does not lower the request rate,
    and any error (including a truncated body) is counted by Traefik rather than ending the traffic."""

    def __init__(self, base: str, paths: list[str], rps: float):
        self.urls = itertools.cycle([base.rstrip("/") + p for p in paths])
        self.interval = 1.0 / rps
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.next_at = time.monotonic()
        self.sent = 0
        self.threads = [threading.Thread(target=self._work, daemon=True) for _ in range(WORKERS)]

    def start(self):
        for t in self.threads:
            t.start()

    def _next(self) -> str | None:
        with self.lock:
            wait = self.next_at - time.monotonic()
            self.next_at = max(self.next_at, time.monotonic()) + self.interval
            url = next(self.urls)
        if wait > 0 and self.stop_event.wait(wait):
            return None
        return url

    def _work(self):
        while not self.stop_event.is_set():
            url = self._next()
            if url is None:
                return
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    r.read()
            except Exception:  # noqa: BLE001 - failures are what Traefik counts; the traffic must go on
                pass
            with self.lock:
                self.sent += 1

    def stop(self):
        self.stop_event.set()
