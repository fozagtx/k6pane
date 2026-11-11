"""End-to-end runs against a local HTTP server. Skipped when k6 is not installed."""

import json
import os

import pytest


@pytest.fixture(autouse=True)
def _need_k6(plugin):
    from k6pane import runner
    if not runner.find_k6():
        if os.environ.get("K6PANE_REQUIRE_K6"):
            pytest.fail("K6PANE_REQUIRE_K6 is set but k6 was not found")
        pytest.skip("k6 not installed")


def test_generated_flow_pass_then_compare(plugin, k6home, local_server):
    from k6pane import tools
    args = {
        "requests": [
            {"name": "home", "url": f"{local_server}/"},
            {"name": "login", "method": "POST", "url": f"{local_server}/login", "json": {"u": "a"}},
            {"name": "gone", "url": f"{local_server}/missing", "expect_status": 404},
        ],
        "profile": "constant", "vus": 2, "duration": "3s", "think_time": 0.05, "label": "e2e",
    }
    first = json.loads(tools.run(args))
    assert first["verdict"] == "pass", first
    s = first["summary"]
    assert s["requests"] > 0 and s["error_rate"] == 0
    assert set(s["endpoints"]) == {"home", "login", "gone"}
    assert s["failed_checks"] == []

    second = json.loads(tools.run({**args, "baseline_run_id": first["run_id"]}))
    assert second["verdict"] == "pass"
    assert "comparison" in second

    listed = json.loads(tools.report({"action": "list"}))["runs"]
    assert [r["run_id"] for r in listed] == [second["run_id"], first["run_id"]]
    cmp = json.loads(tools.report({"action": "compare", "run_id": second["run_id"],
                                   "baseline_run_id": first["run_id"]}))
    assert cmp["verdict"] in ("regression", "no_regression")
    got = json.loads(tools.report({"action": "get", "run_id": first["run_id"], "include_script": True}))
    assert "CONFIG" in got["script"]


def test_threshold_failure_is_fail(plugin, k6home, local_server):
    from k6pane import tools
    out = json.loads(tools.run({"url": f"{local_server}/missing", "profile": "constant", "vus": 1,
                                "duration": "2s", "think_time": 0.1}))
    assert out["verdict"] == "fail"
    assert out["summary"]["error_rate"] == 1
    assert any(not t["passed"] for t in out["summary"]["thresholds"])


def test_custom_script_with_env(plugin, k6home, local_server, tmp_path):
    from k6pane import tools
    script = tmp_path / "custom.js"
    script.write_text(
        "import http from 'k6/http';\n"
        "export const options = { vus: 1, duration: '2s', thresholds: { http_req_failed: ['rate<0.01'] } };\n"
        "export default function () { http.get(__ENV.BASE_URL + '/'); }\n"
    )
    out = json.loads(tools.run({"script_path": str(script), "env": {"BASE_URL": local_server}}))
    assert out["verdict"] == "pass", out
    assert out["summary"]["requests"] > 0


def test_broken_script_is_error(plugin, k6home):
    from k6pane import tools
    out = json.loads(tools.run({"script": "this is not javascript (((", "duration": "2s"}))
    assert out["verdict"] == "error"
    assert out["log_tail"]
