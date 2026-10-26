"""Prometheus queries used to judge a canary against the stable version."""
import json
import os
import time
import urllib.parse
import urllib.request

PROMETHEUS = os.environ.get("PROMETHEUS_URL", "http://100.73.222.43:9090")
RETRIES = 3


def query(expr: str) -> float | None:
    """Instant query; a brief Prometheus hiccup is retried instead of aborting the canary."""
    url = f"{PROMETHEUS}/api/v1/query?query={urllib.parse.quote(expr)}"
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                result = json.load(r)["data"]["result"]
            break
        except OSError:
            if attempt == RETRIES - 1:
                raise
            time.sleep(2 * (attempt + 1))
    if not result:
        return None
    value = float(result[0]["value"][1])
    return None if value != value else value  # NaN means no traffic in the window


def service_stats(instance: str, service: str, window_s: int) -> dict:
    """Requests, failure ratio (5xx and 499) and p95 latency of one Traefik service over the last window_s seconds."""
    sel = f'instance="{instance}",service="{service}"'
    w = f"{max(window_s, 30)}s"
    total = query(f"sum(increase(traefik_service_requests_total{{{sel}}}[{w}]))") or 0.0
    # 499 = the client gave up waiting: a canary Traefik cannot reach shows up as 499s, not 5xx.
    errors = query(f'sum(increase(traefik_service_requests_total{{{sel},code=~"5..|499"}}[{w}]))') or 0.0
    p95 = query(
        f"histogram_quantile(0.95, sum by (le)(increase(traefik_service_request_duration_seconds_bucket{{{sel}}}[{w}])))"
    )
    return {
        "requests": round(total),
        "errors": round(errors),
        "error_ratio": errors / total if total else 0.0,
        "p95_s": p95,
    }
