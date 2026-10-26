"""Canary verdict: compare the canary's errors and latency with the stable version over the same window."""


def judge(canary: dict, stable: dict, a: dict, final: bool) -> tuple[str, str]:
    """Return (verdict, reason). verdict is 'fail', 'pass' or 'wait' (not enough data yet)."""
    # Clear evidence does not wait for the full sample: a canary failing most requests leaves at once.
    if canary["requests"] >= a["early_fail_min_requests"] and canary["error_ratio"] >= a["early_fail_error_ratio"]:
        return "fail", f"canary failing {canary['error_ratio']:.0%} of {canary['requests']} requests"

    if canary["requests"] < a["min_canary_requests"]:
        if final:
            return "fail", f"only {canary['requests']} canary requests, need {a['min_canary_requests']} to judge"
        return "wait", f"{canary['requests']}/{a['min_canary_requests']} canary requests"

    # Errors (5xx and 499): an absolute ceiling, and no worse than a multiple of what stable shows right now
    # (so a shared outage, e.g. the database, does not get blamed on the new version).
    allowed = max(a["max_error_ratio"], stable["error_ratio"] * a["max_error_ratio_vs_stable"])
    if canary["error_ratio"] > allowed:
        return "fail", (f"canary errors {canary['error_ratio']:.2%} > allowed {allowed:.2%} "
                        f"(stable {stable['error_ratio']:.2%})")

    if canary["p95_s"] is not None and stable["p95_s"] is not None:
        limit = stable["p95_s"] * a["max_p95_vs_stable"] + a["p95_slack_s"]
        if canary["p95_s"] > limit:
            return "fail", f"canary p95 {canary['p95_s']*1000:.0f}ms > limit {limit*1000:.0f}ms"
    elif final:
        # A missing histogram must not switch the latency gate off silently.
        return "fail", "latency not measurable (no p95 for canary or stable), cannot judge"

    if final:
        return "pass", (f"canary {canary['requests']} req, errors {canary['error_ratio']:.2%}, "
                        f"p95 {fmt_ms(canary['p95_s'])} vs stable {fmt_ms(stable['p95_s'])}")
    return "wait", "healthy so far"


def fmt_ms(v: float | None) -> str:
    return "n/a" if v is None else f"{v*1000:.0f}ms"
