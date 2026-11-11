import json

import pytest


def test_parse_duration(plugin):
    from k6pane.durations import parse_duration
    assert parse_duration("30s") == 30
    assert parse_duration("1h30m") == 5400
    assert parse_duration("2m30s") == 150
    assert parse_duration("1500ms") == 1.5
    assert parse_duration(45) == 45
    for bad in ("", "5x", "m5", "1h junk"):
        with pytest.raises(ValueError):
            parse_duration(bad)


def test_guard_local_and_blocked(plugin, monkeypatch):
    from k6pane import guard
    real = guard.resolve_host
    fake = {"example.com": ["93.184.215.14"], "staging.example.com": ["93.184.215.15"], "api.localhost": ["127.0.0.1"]}
    monkeypatch.setattr(guard, "resolve_host", lambda h: fake.get(h, None) or real(h))
    monkeypatch.delenv("K6PANE_ALLOWED_HOSTS", raising=False)
    hosts, blocked = guard.check_targets([
        "http://localhost:3000/a", "http://127.0.0.1/b", "http://10.0.0.5/c", "http://[::1]:8080/",
        "http://api.localhost/", "https://example.com/x",
    ])
    assert blocked == ["example.com"]
    monkeypatch.setenv("K6PANE_ALLOWED_HOSTS", "*.example.com,example.com")
    assert guard.check_targets(["https://example.com/x", "https://staging.example.com"])[1] == []


def test_extract_urls(plugin):
    from k6pane.guard import extract_urls
    script = "http.get('http://localhost:3000/a'); http.post(`https://evil.example.org/x`, body);"
    assert extract_urls(script) == ["http://localhost:3000/a", "https://evil.example.org/x"]


def test_build_options_profiles(plugin):
    from k6pane import scriptgen
    reqs = scriptgen.normalize_requests([{"url": "http://localhost/a"}])
    base = {"requests": reqs, "vus": 40, "duration_s": 100}
    for profile in ("load", "stress", "spike", "soak"):
        built = scriptgen.build_options({**base, "profile": profile})
        stages = built["options"]["stages"]
        assert stages[-1]["target"] == 0
        assert built["peak_vus"] == 40
        assert max(s["target"] for s in stages) == 40
        assert 95 <= built["planned_seconds"] <= 105
    smoke = scriptgen.build_options({**base, "profile": "smoke"})
    assert smoke["options"]["vus"] == 1 and smoke["peak_vus"] == 1
    rate = scriptgen.build_options({**base, "profile": "load", "rate": 50})
    assert rate["options"]["scenarios"]["arrival_rate"]["rate"] == 50
    assert rate["peak_vus"] == 80


def test_thresholds_defaults_and_overrides(plugin):
    from k6pane import scriptgen
    reqs = scriptgen.normalize_requests([{"url": "http://localhost/a", "name": "home"}])
    spec = {"requests": reqs, "vus": 1, "duration_s": 10, "profile": "constant", "p95_ms": 200, "max_error_rate": 0.05}
    th = scriptgen.build_options(spec)["options"]["thresholds"]
    assert th["http_req_duration"] == ["p(95)<200"]
    assert th["http_req_failed"] == ["rate<0.05"]
    assert th["http_req_duration{name:home}"] == ["max>=0"]


def test_normalize_requests_validation(plugin):
    from k6pane import scriptgen
    with pytest.raises(ValueError):
        scriptgen.normalize_requests([])
    with pytest.raises(ValueError):
        scriptgen.normalize_requests([{"url": "ftp://x"}])
    with pytest.raises(ValueError):
        scriptgen.normalize_requests([{"url": "http://x", "method": "TRACE"}])
    out = scriptgen.normalize_requests([{"url": "http://localhost/a"}, {"url": "http://localhost/a"}])
    assert out[0]["name"] != out[1]["name"]


def test_script_embeds_config_safely(plugin):
    from k6pane import scriptgen
    reqs = scriptgen.normalize_requests([{"url": "http://localhost/a", "headers": {"X": "'); evil(); //</script>"}}])
    spec = {"requests": reqs, "vus": 1, "duration_s": 5, "profile": "constant"}
    script = scriptgen.render_script(spec, scriptgen.build_options(spec)["options"])
    blob = script.split("const CONFIG = ", 1)[1].split(";\n\nexport const options", 1)[0]
    assert json.loads(blob.replace("<\\/", "</"))["requests"][0]["headers"]["X"].startswith("');")


def test_summarize_and_compare(plugin):
    from k6pane import summary
    export = {
        "metrics": {
            "http_req_duration": {"avg": 10, "min": 1, "med": 9, "max": 50, "p(90)": 20, "p(95)": 25, "p(99)": 40,
                                  "thresholds": {"p(95)<500": False}},
            "http_req_duration{name:home}": {"avg": 10, "med": 9, "p(95)": 25, "thresholds": {"max>=0": False}},
            "http_req_failed{name:home}": {"passes": 1, "fails": 99, "value": 0.01, "thresholds": {"rate>=0": False}},
            "http_req_failed": {"passes": 1, "fails": 99, "value": 0.01, "thresholds": {"rate<0.001": True}},
            "http_reqs": {"count": 100, "rate": 50},
            "checks": {"passes": 99, "fails": 1, "value": 0.99},
        },
        "root_group": {"checks": {"home status 200": {"passes": 99, "fails": 1}}},
    }
    s = summary.summarize(export)
    assert s["latency_ms"]["p95"] == 25 and s["rps"] == 50
    assert s["thresholds_passed"] is False
    assert [t["threshold"] for t in s["thresholds"]] == ["p(95)<500", "rate<0.001"]
    assert s["endpoints"]["home"]["requests"] == 100
    assert s["failed_checks"][0]["fails"] == 1

    worse = json.loads(json.dumps(s))
    worse["latency_ms"]["p95"] = 40
    worse["rps"] = 30
    cmp = summary.compare(worse, s)
    assert cmp["verdict"] == "regression"
    assert {r["metric"] for r in cmp["regressions"]} >= {"latency_ms.p95", "rps"}
    assert summary.compare(s, s)["verdict"] == "no_regression"


def test_run_blocks_remote_and_limits(plugin, k6home, monkeypatch):
    from k6pane import tools
    out = json.loads(tools.run({"url": "https://example.com/", "dry_run": True}))
    assert "Refusing" in out["error"] and out["blocked_hosts"] == ["example.com"]
    out = json.loads(tools.run({"script": "import http from 'k6/http'; export default () => http.get('https://example.com')",
                                "dry_run": True}))
    assert out["blocked_hosts"] == ["example.com"]
    monkeypatch.setenv("K6PANE_MAX_VUS", "5")
    out = json.loads(tools.run({"url": "http://localhost:1/", "vus": 50, "dry_run": True}))
    assert "K6PANE_MAX_VUS" in out["error"]


def test_report_errors(plugin, k6home):
    from k6pane import tools
    assert json.loads(tools.report({"action": "list"}))["runs"] == []
    assert "not found" in json.loads(tools.report({"action": "get", "run_id": "nope"}))["error"]
    assert "not found" in json.loads(tools.report({"action": "get", "run_id": "../etc"}))["error"]


def test_register_with_fake_ctx(plugin):
    calls = []

    class Ctx:
        def register_tool(self, **kw):
            calls.append(kw)

    plugin.register(Ctx())
    assert {c["name"] for c in calls} == {"k6pane_doctor", "k6pane_run", "k6pane_report"}
    assert all(c["toolset"] == "k6pane" and c["schema"]["name"] == c["name"] for c in calls)


def test_root_shim_loads_like_hermes(hermes_loader):

    calls = []

    class Ctx:
        def register_tool(self, **kw):
            calls.append(kw["name"])

    hermes_loader().register(Ctx())
    assert sorted(calls) == ["k6pane_doctor", "k6pane_report", "k6pane_run"]


def test_unresolvable_host_blocked(plugin, monkeypatch):
    from k6pane import guard
    monkeypatch.setattr(guard, "resolve_host", lambda h: [])
    monkeypatch.setenv("K6PANE_ALLOWED_HOSTS", "nowhere.invalid")
    assert guard.check_targets(["http://nowhere.invalid/"])[1] == ["nowhere.invalid"]


def test_localhost_must_be_loopback(plugin, monkeypatch):
    from k6pane import guard
    monkeypatch.setattr(guard, "resolve_host", lambda h: ["10.0.0.9"] if h == "localhost" else [])
    assert guard.check_targets(["http://localhost/"])[1] == ["localhost"]


def test_fence_excludes_exactly_validated_ips(plugin):
    import ipaddress

    from k6pane import guard
    ranges = [ipaddress.ip_network(r) for r in guard.fence(["127.0.0.1", "::1", "10.1.2.3"])]

    def blocked(ip):
        a = ipaddress.ip_address(ip)
        return any(a in n for n in ranges if n.version == a.version)

    for ok in ("127.0.0.1", "::1", "10.1.2.3"):
        assert not blocked(ok)
    for bad in ("127.0.0.2", "10.1.2.4", "8.8.8.8", "192.168.1.1", "::2", "2001:db8::1", "0.0.0.0", "255.255.255.255"):
        assert blocked(bad)
    assert guard.fence([]) == ["0.0.0.0/0", "::/0"]


def test_rate_cap_counts_requests_per_iteration(plugin, k6home, monkeypatch):
    from k6pane import tools
    monkeypatch.setenv("K6PANE_MAX_RPS", "250")
    reqs = [{"url": "http://localhost:1/a"}, {"url": "http://localhost:1/b"}, {"url": "http://localhost:1/c"}]
    out = json.loads(tools.run({"requests": reqs, "rate": 100, "duration": "5s", "dry_run": True}))
    assert "300 requests/s" in out["error"]
    assert "error" not in json.loads(tools.run({"requests": reqs, "rate": 80, "duration": "5s", "dry_run": True}))


def test_concurrent_run_rejected_with_holder_id(plugin, k6home, monkeypatch):
    from k6pane import runner, store, tools
    monkeypatch.setattr(runner, "find_k6", lambda: "/usr/bin/true")
    with store.run_lock("20260101-000000-busy-0001"):
        out = json.loads(tools.run({"url": "http://localhost:1/", "profile": "smoke", "duration": "1s"}))
    assert out["running_run_id"] == "20260101-000000-busy-0001"
    assert "already in progress" in out["error"]
    with store.run_lock("next"):  # released after the holder exits
        pass
