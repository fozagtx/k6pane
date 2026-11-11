"""Phase 1.5: `invalid` verdict when the requested load was not actually applied."""

import json
import os

import pytest

from k6pane import summary, tools

OK = {"thresholds_passed": True, "dropped_iterations": 0, "iterations": 1000}


def test_pass_fail_error():
    assert summary.decide_verdict(0, False, OK)["verdict"] == "pass"
    assert summary.decide_verdict(99, False, {**OK, "thresholds_passed": False})["verdict"] == "fail"
    assert summary.decide_verdict(107, False, OK)["verdict"] == "error"
    assert summary.decide_verdict(0, False, None)["verdict"] == "error"


def test_timeout_is_invalid_even_if_thresholds_passed():
    d = summary.decide_verdict(0, True, OK)
    assert d["verdict"] == "invalid" and "timeout" in d["invalid_reasons"][0]


def test_dropped_iterations_invalid():
    d = summary.decide_verdict(0, False, {**OK, "dropped_iterations": 12})
    assert d["verdict"] == "invalid" and "12 iterations were dropped" in d["invalid_reasons"][0]


def test_achieved_rate_short_of_target_invalid():
    assert summary.decide_verdict(0, False, {**OK, "iterations": 960}, 100, 10)["verdict"] == "pass"
    d = summary.decide_verdict(0, False, {**OK, "iterations": 500}, 100, 10)
    assert d["verdict"] == "invalid" and "50.0 iterations/s" in d["invalid_reasons"][0]


def test_insufficient_vus_warning_invalid():
    d = summary.decide_verdict(0, False, OK, log_errors=['level=warning msg="Insufficient VUs, reached 1"'])
    assert d["verdict"] == "invalid"


def test_failed_thresholds_on_invalid_run_still_invalid():
    d = summary.decide_verdict(99, False, {**OK, "thresholds_passed": False, "dropped_iterations": 3})
    assert d["verdict"] == "invalid"


def test_compare_refuses_invalid_baseline(k6home):
    from k6pane import store
    for rid, verdict in (("20260101-000000-a-0001", "invalid"), ("20260101-000001-b-0002", "pass")):
        d = store.runs_dir() / rid
        d.mkdir()
        store.write_json(d / "result.json", {"verdict": verdict, "summary": {"latency_ms": {}, "rps": 1}})
    out = json.loads(tools.report({"action": "compare", "run_id": "20260101-000001-b-0002",
                                   "baseline_run_id": "20260101-000000-a-0001"}))
    assert "invalid" in out["error"]


def test_e2e_saturated_arrival_rate_is_invalid(k6home, local_server):
    from k6pane import runner
    if not runner.find_k6():
        if os.environ.get("K6PANE_REQUIRE_K6"):
            pytest.fail("k6 required")
        pytest.skip("k6 not installed")
    # /slow takes 200ms, so one VU can do at most ~5 iterations/s; asking for 50/s must be invalid, not pass.
    out = json.loads(tools.run({"url": f"{local_server}/slow", "rate": 50, "vus": 1, "max_vus": 1,
                                "duration": "3s", "p95_ms": 5000}))
    assert out["verdict"] == "invalid", out
    assert out["summary"]["dropped_iterations"] > 0
    assert out["invalid_reasons"]


def test_compare_flags_different_scripts(tmp_path, monkeypatch):
    import json as _json

    from k6pane import store, tools

    monkeypatch.setenv("K6PANE_HOME", str(tmp_path))
    summ = {"latency_ms": {"avg": 1, "p50": 1, "p90": 1, "p95": 1, "p99": 1, "max": 2},
            "rps": 100, "error_rate": 0}
    for rid, body in (("a", "http.get(u);"), ("b", "http.get(u);\nsleep(0.1);"), ("c", "http.get(u);")):
        d = store.runs_dir() / rid
        d.mkdir(parents=True)
        (d / "script.js").write_text(body)
        (d / "result.json").write_text(_json.dumps({"verdict": "pass", "summary": summ}))
    out = _json.loads(tools.report({"action": "compare", "run_id": "b", "baseline_run_id": "a"}))
    assert out["workload"]["same_workload"] is False
    assert "+sleep(0.1);" in out["workload"]["script_changes"]
    same = _json.loads(tools.report({"action": "compare", "run_id": "c", "baseline_run_id": "a"}))
    assert "workload" not in same
