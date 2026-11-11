"""Phase 1.1: secret references, auto-secretizing, env scrubbing and redaction."""

import json
import os

import pytest

from k6pane import redact, tools


def test_redact_keys_headers_query_and_values():
    data = {
        "headers": {"Authorization": "Bearer abc123", "X-Trace": "ok", "Cookie": "sid=1"},
        "api_key": "k-1", "nested": [{"password": "p"}], "ref": {"token": "${secret.TOKEN}"},
        "url": "http://h/x?access_token=zzz&page=2",
        "log": "got value s3cret-value-9 here\nAuthorization: Bearer raw",
    }
    out = redact.redact(data, ["s3cret-value-9"])
    assert out["headers"]["Authorization"] == redact.MASK
    assert out["headers"]["Cookie"] == redact.MASK
    assert out["headers"]["X-Trace"] == "ok"
    assert out["api_key"] == redact.MASK and out["nested"][0]["password"] == redact.MASK
    assert out["ref"]["token"] == "${secret.TOKEN}"
    assert "zzz" not in out["url"] and "page=2" in out["url"]
    assert "s3cret-value-9" not in out["log"] and "Bearer raw" not in out["log"]


def test_secretize_moves_literals_into_bag():
    bag = redact.SecretBag()
    reqs = redact.secretize_requests([{
        "url": "http://localhost/a?token=lit-q&x=1",
        "headers": {"Authorization": "Bearer lit-h", "Accept": "json", "X-Key": "${secret.KEY}"},
        "json": {"user": "u", "password": "lit-p"},
    }], bag)
    blob = json.dumps(reqs)
    assert "lit-q" not in blob and "lit-h" not in blob and "lit-p" not in blob
    assert sorted(bag.values.values()) == ["Bearer lit-h", "lit-p", "lit-q"]
    assert set(redact.find_refs(reqs, "secret")) == {"AUTO_1", "AUTO_2", "AUTO_3", "KEY"}
    assert "x=1" in reqs[0]["url"]


def test_secret_names_only_from_prefixed_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "should-never-be-read")
    monkeypatch.delenv("K6PANE_SECRET_OPENROUTER_API_KEY", raising=False)
    bag = redact.SecretBag()
    assert bag.resolve_env(["OPENROUTER_API_KEY"]) == ["OPENROUTER_API_KEY"]
    assert bag.values == {}
    with pytest.raises(ValueError):
        bag.resolve_env(["bad-name"])


def test_placeholder_in_host_rejected(k6home):
    out = json.loads(tools.run({"url": "http://${secret.HOST}/x", "dry_run": True}))
    assert "host" in out["error"]


def test_sensitive_env_keys_rejected(k6home):
    out = json.loads(tools.run({"url": "http://localhost/", "env": {"API_TOKEN": "x"}, "dry_run": True}))
    assert "credentials" in out["error"]


def test_inline_literal_credential_rejected_but_templates_allowed(k6home):
    bad = "export default () => http.get('http://localhost/', {headers: {Authorization: 'Bearer abcdef123'}})"
    assert "literal credential" in json.loads(tools.run({"script": bad, "dry_run": True}))["error"]
    good = "export default async () => http.get('http://localhost/', {headers: {Authorization: `Bearer ${t}`}})"
    assert "error" not in json.loads(tools.run({"script": good, "dry_run": True}))


def test_missing_secret_names_but_never_values(k6home, monkeypatch):
    monkeypatch.delenv("K6PANE_SECRET_NOPE", raising=False)
    from k6pane import runner
    if not runner.find_k6():
        pytest.skip("k6 not installed")
    out = json.loads(tools.run({"url": "http://localhost:1/", "headers": {"X-A": "${secret.NOPE}"}}))
    assert out["missing_secrets"] == ["NOPE"] and "K6PANE_SECRET_NOPE" in out["error"]


def test_dry_run_script_contains_no_literal(k6home):
    out = json.loads(tools.run({"url": "http://localhost/", "headers": {"Authorization": "Bearer lit-xyz-1"},
                                "dry_run": True}))
    assert "lit-xyz-1" not in json.dumps(out)
    assert "${secret.AUTO_1}" in out["script"]


# ---------------------------------------------------------------------------
# End to end: the acceptance test from the plan
# ---------------------------------------------------------------------------

def _need_k6():
    from k6pane import runner
    if not runner.find_k6():
        if os.environ.get("K6PANE_REQUIRE_K6"):
            pytest.fail("k6 required")
        pytest.skip("k6 not installed")


def _all_artifacts(root):
    return "\n".join(p.read_text(errors="replace") for p in root.rglob("*") if p.is_file())


def test_e2e_tokens_never_persisted_or_returned(k6home, local_server, monkeypatch, secure_values):
    _need_k6()
    SECURE = secure_values

    token = SECURE["authorization"].split(" ", 1)[1]
    monkeypatch.setenv("K6PANE_SECRET_API_TOKEN", token)
    response = tools.run({
        "requests": [
            {"name": "secure_get", "url": f"{local_server}/secure?token={SECURE['query_token']}",
             "headers": {"Authorization": "Bearer ${secret.API_TOKEN}", "X-Api-Key": SECURE["x-api-key"]}},
            {"name": "secure_post", "method": "POST", "url": f"{local_server}/secure?token={SECURE['query_token']}",
             "headers": {"Authorization": "Bearer ${secret.API_TOKEN}", "X-Api-Key": SECURE["x-api-key"]},
             "json": {"user": "load", "password": SECURE["password"]}},
        ],
        "profile": "constant", "vus": 1, "duration": "2s", "think_time": 0.1,
    })
    out = json.loads(response)
    assert out["verdict"] == "pass", out  # server only returns 200 if every secret arrived intact
    assert out["secrets_used"] == ["API_TOKEN"] and out["auto_protected_values"] == 3

    haystack = response + _all_artifacts(k6home)
    for value in (token, SECURE["x-api-key"], SECURE["query_token"], SECURE["password"]):
        assert value not in haystack, f"secret leaked: {value}"


def test_e2e_custom_script_env_is_scrubbed_and_secrets_redacted(k6home, local_server, monkeypatch, tmp_path):
    _need_k6()
    monkeypatch.setenv("K6PANE_TEST_LEAK", "leak-value-xyz-42")
    monkeypatch.setenv("K6PANE_SECRET_API_TOKEN", "custom-secret-val-88")
    script = tmp_path / "s.js"
    script.write_text(
        "import http from 'k6/http';\n"
        "import secrets from 'k6/secrets';\n"
        "export const options = { iterations: 1 };\n"
        "export default async function () {\n"
        "  console.log('leak=' + (__ENV.K6PANE_TEST_LEAK || 'absent'));\n"
        "  const t = await secrets.get('API_TOKEN');\n"
        "  console.log('secret=' + t);\n"
        "  http.get(__ENV.BASE_URL + '/');\n"
        "}\n"
    )
    response = tools.run({"script_path": str(script), "env": {"BASE_URL": local_server}, "secrets": ["API_TOKEN"]})
    out = json.loads(response)
    assert out["verdict"] == "pass", out
    log = (k6home / "runs" / out["run_id"] / "k6.log").read_text()
    assert "leak=absent" in log
    haystack = response + _all_artifacts(k6home)
    assert "leak-value-xyz-42" not in haystack and "custom-secret-val-88" not in haystack


@pytest.mark.parametrize("src", [
    "headers: { Authorization: 'Bearer ' + token }",
    '{"Authorization": "Bearer " + loginRes.json("token")}',
    "Authorization: `Bearer ${token}`",
    "{ password: await secrets.get('DEMO_PASSWORD') }",
])
def test_runtime_auth_values_are_not_literal_credentials(src):
    assert not tools._has_literal_cred(src)


@pytest.mark.parametrize("src", [
    "Authorization: 'Bearer abc123xyz'",
    "{ password: 'hunter2-real' }",
    "api_key: 'k-0123456789'",
])
def test_literal_credentials_still_rejected(src):
    assert tools._has_literal_cred(src)


def test_unawaited_secret_get_is_caught():
    assert tools._unawaited_secret("const pw = secrets.get('DEMO_PASSWORD');")
    assert not tools._unawaited_secret("const pw = await secrets.get('DEMO_PASSWORD');")
    assert not tools._unawaited_secret("const pw = await  secrets.get( 'A' ); const t = await secrets.get('B');")
    assert tools._unawaited_secret("const a = await secrets.get('A'); const b = secrets.get('B');")
