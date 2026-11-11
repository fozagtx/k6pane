"""Turn k6 `--summary-export` JSON into a compact, model-friendly result."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

TREND_STATS = "avg,min,med,max,p(90),p(95),p(99)"
_SUB = re.compile(r"^(?P<metric>[a-z_]+)\{name:(?P<name>[^}]+)\}$")
# Always-true thresholds k6pane adds to force sub-metric export; hide them.
_INTERNAL = {"max>=0", "rate>=0"}


def _r(v: Optional[float], nd: int = 2) -> Optional[float]:
    return None if v is None else round(float(v), nd)


def _latency(m: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "avg": _r(m.get("avg")), "min": _r(m.get("min")), "p50": _r(m.get("med")),
        "p90": _r(m.get("p(90)")), "p95": _r(m.get("p(95)")), "p99": _r(m.get("p(99)")),
        "max": _r(m.get("max")),
    }


def summarize(export: Dict[str, Any]) -> Dict[str, Any]:
    metrics: Dict[str, Any] = export.get("metrics", {})
    dur = metrics.get("http_req_duration", {})
    reqs = metrics.get("http_reqs", {})
    failed = metrics.get("http_req_failed", {})
    checks = metrics.get("checks", {})
    iters = metrics.get("iterations", {})

    thresholds: List[Dict[str, Any]] = []
    for metric_name, m in sorted(metrics.items()):
        for expr, crossed in (m.get("thresholds") or {}).items():
            if expr in _INTERNAL:
                continue
            thresholds.append({"metric": metric_name, "threshold": expr, "passed": not crossed})

    endpoints: Dict[str, Dict[str, Any]] = {}
    for key, m in metrics.items():
        match = _SUB.match(key)
        if not match:
            continue
        ep = endpoints.setdefault(match.group("name"), {})
        if match.group("metric") == "http_req_duration":
            ep["latency_ms"] = _latency(m)
        elif match.group("metric") == "http_req_failed":
            ep["error_rate"] = _r(m.get("value"), 4)
            ep["failed_requests"] = m.get("passes")
            ep["requests"] = (m.get("passes") or 0) + (m.get("fails") or 0)

    failed_checks = []
    for name, c in ((export.get("root_group") or {}).get("checks") or {}).items():
        if c.get("fails"):
            failed_checks.append({"check": name, "fails": c["fails"], "passes": c.get("passes", 0)})

    return {
        "requests": reqs.get("count", 0),
        "rps": _r(reqs.get("rate")),
        "error_rate": _r(failed.get("value"), 4),
        "failed_requests": failed.get("passes", 0),
        "latency_ms": _latency(dur),
        "checks_pass_rate": _r(checks.get("value"), 4),
        "failed_checks": failed_checks,
        "iterations": iters.get("count", 0),
        "iterations_rate": _r(iters.get("rate")),
        # k6 omits the metric entirely when nothing was dropped.
        "dropped_iterations": (metrics.get("dropped_iterations") or {}).get("count", 0),
        "vus_max": (metrics.get("vus_max") or {}).get("max"),
        "data_received_bytes": (metrics.get("data_received") or {}).get("count"),
        "thresholds": thresholds,
        "thresholds_passed": all(t["passed"] for t in thresholds),
        "endpoints": endpoints,
    }


def _pct(new: Optional[float], old: Optional[float]) -> Optional[float]:
    if new is None or old is None or old == 0:
        return None
    return round((new - old) / old * 100, 1)


def compare(current: Dict[str, Any], baseline: Dict[str, Any], tolerance_pct: float = 10.0) -> Dict[str, Any]:
    """Compare two summaries. Latency/error increases and RPS drops beyond tolerance are regressions."""
    rows = []
    for stat in ("avg", "p50", "p90", "p95", "p99", "max"):
        rows.append(("latency_ms." + stat, current["latency_ms"].get(stat), baseline["latency_ms"].get(stat), "lower"))
    rows.append(("rps", current.get("rps"), baseline.get("rps"), "higher"))
    rows.append(("error_rate", current.get("error_rate"), baseline.get("error_rate"), "lower"))

    deltas, regressions, improvements = [], [], []
    for name, new, old, better in rows:
        change = _pct(new, old)
        entry = {"metric": name, "current": new, "baseline": old, "change_pct": change}
        deltas.append(entry)
        if name == "latency_ms.max":
            continue  # max is too noisy to judge on
        if name == "error_rate":
            if (new or 0) > (old or 0) + 0.001:
                regressions.append(entry)
            elif (new or 0) + 0.001 < (old or 0):
                improvements.append(entry)
            continue
        if change is None:
            continue
        worse = change > tolerance_pct if better == "lower" else change < -tolerance_pct
        nicer = change < -tolerance_pct if better == "lower" else change > tolerance_pct
        if worse:
            regressions.append(entry)
        elif nicer:
            improvements.append(entry)
    return {
        "tolerance_pct": tolerance_pct,
        "verdict": "regression" if regressions else "no_regression",
        "regressions": regressions,
        "improvements": improvements,
        "deltas": deltas,
    }


ACHIEVED_RATE_MIN = 0.95


def decide_verdict(
    exit_code: Optional[int],
    timed_out: bool,
    compact: Optional[Dict[str, Any]],
    target_rate: Optional[float] = None,
    rate_duration_s: Optional[float] = None,
    log_errors: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Return {"verdict": pass|fail|invalid|error, "invalid_reasons": [...], "explanation": str}.

    `invalid` means the numbers do not describe the target under the requested
    load, so they must not be read as a pass or a fail.
    """
    if compact is None:
        return {"verdict": "error", "invalid_reasons": [],
                "explanation": "k6 produced no summary (script or config error); see k6_errors/log_tail."}
    if exit_code not in (0, 99) and not timed_out:
        return {"verdict": "error", "invalid_reasons": [],
                "explanation": f"k6 exited with code {exit_code}; results may be partial."}

    reasons: List[str] = []
    if timed_out:
        reasons.append("The run hit k6pane's timeout and was interrupted, so it did not complete the planned load.")
    dropped = compact.get("dropped_iterations") or 0
    if dropped:
        reasons.append(
            f"{dropped} iterations were dropped: k6 had no free VU to start them on schedule. Either max_vus is too "
            "low for the target's response time (raise max_vus), or the machine running k6 is saturated."
        )
    if target_rate and rate_duration_s:
        achieved = (compact.get("iterations") or 0) / rate_duration_s
        if achieved < target_rate * ACHIEVED_RATE_MIN:
            reasons.append(
                f"Achieved {achieved:.1f} iterations/s against a target of {target_rate}/s "
                f"({achieved / target_rate:.0%}). The requested load was never actually applied."
            )
    if any("Insufficient VUs" in e for e in (log_errors or [])):
        reasons.append("k6 reported 'Insufficient VUs': it could not start enough VUs to sustain the schedule.")

    if reasons:
        return {"verdict": "invalid", "invalid_reasons": reasons,
                "explanation": "Invalid run: the load generator side did not deliver the requested load, so latency "
                               "and error numbers do not answer the question. Fix the cause and rerun; do not report "
                               "this as a pass or a fail of the target."}
    if exit_code == 0 and compact["thresholds_passed"]:
        return {"verdict": "pass", "invalid_reasons": [], "explanation": "All thresholds passed."}
    return {"verdict": "fail", "invalid_reasons": [],
            "explanation": "One or more thresholds were crossed by the target under the requested load."}
