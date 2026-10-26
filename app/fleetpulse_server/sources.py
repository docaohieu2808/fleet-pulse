"""Read-only queries against Prometheus (SLOs, apps, alert rules) and Loki (request evidence for postmortems).

Every query distinguishes "no data" from "could not ask": the dashboard must never turn an unreachable Prometheus
into "0 healthy" or "0.00% failed". Label values that end up inside PromQL/LogQL are checked against a strict
character set first.
"""
import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime

PROMETHEUS = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
LOKI = os.environ.get("LOKI_URL", "http://loki:3100")
ALERTMANAGER = os.environ.get("ALERTMANAGER_URL", "http://alertmanager:9093")
SLO_DIR = os.environ.get("SLO_DIR", "/slo")
TIMEOUT_S = 8

_SAFE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


class Unavailable(Exception):
    """A data source could not be reached or answered something unusable."""


def safe(value: str | None) -> str | None:
    """The value when it can be put inside a quoted label matcher as is, else None."""
    return value if value and _SAFE.fullmatch(value) else None


def _get(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_S) as r:
            return json.load(r)
    except (OSError, ValueError) as e:
        raise Unavailable(str(e)) from e


def query(expr: str, at: float | None = None) -> list[dict]:
    """Instant query. Raises Unavailable; an empty list means "no series"."""
    url = f"{PROMETHEUS}/api/v1/query?query={urllib.parse.quote(expr)}"
    if at is not None:
        url += f"&time={at}"
    return _get(url)["data"]["result"]


def _num(v: str) -> float | None:
    f = float(v)
    return None if f != f else f


def services() -> list[dict]:
    """Up/down of every app in the blackbox `apps` job (probed every minute from server-3, inside the tailnet).
    The list comes from `up`, which exists for every configured target: an app whose probe cannot run shows as
    "unknown" instead of vanishing."""
    targets = query('up{job="apps"}')
    ok = {r["metric"]["instance"]: r["value"][1] for r in query('probe_success{job="apps"}')}
    code = {r["metric"]["instance"]: r["value"][1] for r in query('probe_http_status_code{job="apps"}')}
    secs = {r["metric"]["instance"]: r["value"][1] for r in query('probe_duration_seconds{job="apps"}')}
    out = []
    for r in targets:
        m = r["metric"]
        inst = m["instance"]
        if r["value"][1] != "1" or inst not in ok:
            status = "unknown"
        else:
            status = "up" if ok[inst] == "1" else "down"
        out.append({
            "id": m.get("app"), "name": m.get("name") or inst, "url": m.get("link") or inst,
            "group": m.get("app_group"), "host": m.get("host"), "status": status,
            "alerting": m.get("page") != "no",
            "last_code": int(float(code[inst])) if inst in code else None,
            "last_latency_ms": round(float(secs[inst]) * 1000) if inst in secs else None,
        })
    return sorted(out, key=lambda x: x["name"])


def specs() -> dict[tuple[str, str], dict]:
    """Per SLO, from the Sloth specs: its human sentence and its SLI queries (service, then each '- name' block)."""
    out = {}
    try:
        names = sorted(os.listdir(SLO_DIR))
    except OSError:
        return out
    for fname in names:
        if not fname.endswith(".yaml"):
            continue
        service, slo = None, None
        for line in open(os.path.join(SLO_DIR, fname)):
            if m := re.match(r'^service:\s*"?([^"\n]+)"?', line):
                service = m.group(1).strip()
            elif m := re.match(r'^\s*- name:\s*"?([^"\n]+)"?', line):
                slo = m.group(1).strip()
            elif service and slo and (m := re.match(r'^\s*(description|error_query|total_query):\s*"?(.+?)"?\s*$', line)):
                out.setdefault((service, slo), {})[m.group(1)] = m.group(2)
    return out


_events_cache: dict[str, tuple[float, float | None]] = {}


def events_30d(sli_query: str, max_age_s: int = 600) -> float | None:
    """How many events (probes, requests) an SLI query counts over its 30-day window so far. Counted from the raw
    series: Sloth's 30-day ratio is an average of 5-minute ratios and under-counts short outages. Its queries use
    rate(), a per-second average; increase() over the same range gives the count. A 30-day range is not cheap and
    the count moves slowly, so it is cached for ten minutes."""
    hit = _events_cache.get(sli_query)
    if hit and time.time() - hit[0] < max_age_s:
        return hit[1]
    res = query(sli_query.replace("{{.window}}", "30d").replace("rate(", "increase("))
    value = _num(res[0]["value"][1]) if res else None
    _events_cache[sli_query] = (time.time(), value)
    return value


PERIOD_MIN = 30 * 24 * 60


def budget_in_units(objective: float, error_ratio: float | None, hours: float | None, unit: str,
                    events: float | None) -> tuple[float | None, float | None]:
    """(allowed, used) for the 30-day period, in what a person can picture.
    minutes: probes run at a fixed pace, so failed share x observed time is downtime; the period allows
    (1 - objective) x 30 days. requests: failed requests so far, against (1 - objective) of the requests the
    period will see at the current pace. None when there is not enough to say."""
    if error_ratio is None or not hours:
        return None, None
    if unit == "minutes":
        return (1 - objective) * PERIOD_MIN, error_ratio * hours * 60
    if events is None:
        return None, None
    return (1 - objective) * events * 720 / min(720.0, hours), error_ratio * events


_SELECTOR = re.compile(r"([a-zA-Z_:][a-zA-Z0-9_:]*)\{([^}]*)\}")


def data_hours() -> dict[tuple[str, str], float]:
    """Hours of raw data each SLO's 30-day window really holds (max 720), read from the SLI's own source series
    (the denominator of Sloth's 5m rule)."""
    hours = {}
    for g in _get(f"{PROMETHEUS}/api/v1/rules?type=record")["data"]["groups"]:
        for r in g["rules"]:
            if r.get("name") != "slo:sli_error:ratio_rate5m":
                continue
            found = _SELECTOR.findall(r["query"])
            if not found:
                continue
            metric, labels = found[-1]
            res = query(f"(time() - min(min_over_time(timestamp({metric}{{{labels}}})[30d:15m]))) / 3600")
            if res:
                key = (r["labels"].get("sloth_service"), r["labels"].get("sloth_slo"))
                hours[key] = min(720.0, round(float(res[0]["value"][1]), 1))
    return hours


def slos(coverage: dict[tuple[str, str], float]) -> list[dict]:
    """One row per SLO: objective, budget left this period, burn rate over the last hour, data coverage."""
    def by_slo(expr):
        return {(r["metric"]["sloth_service"], r["metric"]["sloth_slo"]): (r["metric"], _num(r["value"][1]))
                for r in query(expr)}

    objective = by_slo("slo:objective:ratio")
    remaining = by_slo("slo:period_error_budget_remaining:ratio")
    error_1h = by_slo("slo:sli_error:ratio_rate1h")
    error_30d = by_slo("slo:sli_error:ratio_rate30d")
    spec = specs()
    out = []
    for key, (labels, obj) in sorted(objective.items()):
        e1h = error_1h.get(key, (None, None))[1]
        e30d = error_30d.get(key, (None, None))[1]
        budget = 1 - obj if obj is not None else None
        sli = spec.get(key, {})
        unit = "minutes" if "probe_success" in sli.get("total_query", "") else "requests"
        total = events_30d(sli["total_query"]) if sli.get("total_query") else None
        failed = events_30d(sli["error_query"]) if sli.get("error_query") else None
        ratio = failed / total if (failed is not None and total) else None
        allowed, used = budget_in_units(obj, ratio, coverage.get(key), unit, total) if obj is not None else (None, None)
        out.append({
            "app": key[0], "slo": key[1], "host": labels.get("host", ""),
            "objective": obj,
            "budget_remaining": remaining.get(key, (None, None))[1],
            # 1x spends the whole budget in exactly the 30-day period.
            "burn_rate": (e1h / budget) if (e1h is not None and budget) else None,
            "error_ratio_period": e30d,
            "data_hours": coverage.get(key),
            "description": spec.get(key, {}).get("description", ""),
            "unit": unit, "budget_allowed": allowed, "budget_used": used,
        })
    return out


def rule_wait_seconds(alertname: str) -> int:
    """The `for:` of an alerting rule, i.e. how long the failure lasted before the alert fired. 0 if unknown."""
    try:
        for g in _get(f"{PROMETHEUS}/api/v1/rules?type=alert")["data"]["groups"]:
            for r in g["rules"]:
                if r.get("name") == alertname:
                    return int(r.get("duration") or 0)
    except (Unavailable, KeyError, TypeError, ValueError):
        pass
    return 0


def active_alert_fingerprints() -> set[str] | None:
    """Fingerprints Alertmanager currently has active, or None when it cannot be asked."""
    try:
        return {a["fingerprint"] for a in _get(f"{ALERTMANAGER}/api/v2/alerts?active=true&silenced=true&inhibited=true")}
    except (Unavailable, KeyError, TypeError):
        return None


def _ns(ts: str) -> int:
    return int(datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z").timestamp() * 1e9)


def failed_requests(host: str | None, key: str, start: str, end: str, limit: int = 15) -> list[str]:
    """Sample of 5xx lines for this app (its name appears in the Traefik service) from the node's access log."""
    host, key = safe(host), safe(key)
    if not host or not key or host in ("external", "cloudflare"):
        return []
    q = (f'{{job="traefik-access",host="{host}"}} |= "{key}" '
         f'|~ "\\" 5[0-9][0-9] |\\"DownstreamStatus\\":5[0-9][0-9]"')
    params = urllib.parse.urlencode({"query": q, "start": _ns(start), "end": _ns(end), "limit": limit,
                                     "direction": "backward"})
    try:
        streams = _get(f"{LOKI}/loki/api/v1/query_range?{params}")["data"]["result"]
    except (Unavailable, KeyError):
        return []
    lines = [v[1] for s in streams for v in s["values"]]
    return [line[:300] for line in lines[:limit]]


def budget_burned(app: str, start: str, end: str) -> list[tuple[str, float | None, float]] | None:
    """(slo, error ratio during the window or None if no data, share of the 30-day budget) for the app's SLOs.
    None when Prometheus cannot be asked; [] when the app has no SLO."""
    app = safe(app)
    if not app:
        return []
    seconds = max(60, int((_ns(end) - _ns(start)) / 1e9))
    end_unix = _ns(end) / 1e9
    try:
        objectives = query(f'slo:objective:ratio{{sloth_service="{app}"}}')
        out = []
        for r in objectives:
            slo = safe(r["metric"]["sloth_slo"])
            if not slo:
                continue
            budget = 1 - float(r["value"][1])
            res = query(f'avg_over_time(slo:sli_error:ratio_rate5m{{sloth_service="{app}",sloth_slo="{slo}"}}'
                        f'[{seconds}s])', at=end_unix)
            ratio = _num(res[0]["value"][1]) if res else None
            share = ratio * seconds / (budget * 30 * 86400) if (ratio is not None and budget) else 0.0
            out.append((slo, ratio, share))
        return out
    except Unavailable:
        return None


class Snapshot:
    """What /api/summary shows from Prometheus, refreshed by one background thread so a slow Prometheus never
    piles up request threads. Readers get the last good values plus their age."""

    def __init__(self, every_s: int = 30, coverage_every_s: int = 600):
        self.every_s = every_s
        self.coverage_every_s = coverage_every_s
        self.data = {"services": None, "slos": None, "updated": None, "error": "not loaded yet"}
        self._coverage = {}
        self._coverage_at = 0.0

    def refresh(self) -> None:
        try:
            if time.time() - self._coverage_at > self.coverage_every_s:
                self._coverage = data_hours()
                self._coverage_at = time.time()
            self.data = {"services": services(), "slos": slos(self._coverage),
                         "updated": time.time(), "error": None}
        except Unavailable as e:
            self.data = {**self.data, "error": f"Prometheus unavailable: {e}"}

    def loop(self, sleep=time.sleep) -> None:
        while True:
            self.refresh()
            sleep(self.every_s)
