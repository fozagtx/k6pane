---
name: k6pane
description: Load test local services with k6 via the k6pane tools (smoke, load, stress, spike, soak, fixed-RPS), read results, and catch performance regressions.
---

# k6pane: local load testing

Use the `k6pane_*` tools when the user wants to load test, stress test, benchmark, or check the latency/throughput of an HTTP service they run.

## Workflow

1. **Setup**: on the first use in a session, or after any error, call `k6pane_doctor`. If k6 is missing, give the user the `install_command` it returns.
2. **Find the target**: get the base URL (e.g. `http://localhost:3000`) and the endpoints. Look in the project if needed (routes, OpenAPI spec, README). Confirm the service is running.
3. **Smoke first**: run `profile: "smoke"` before any heavy test. If it fails, stop and fix that first, because load results on a broken endpoint mean nothing.
4. **Pick the test the user asked for**:
   - "can it handle normal traffic": `load` with the expected `vus`
   - "where does it break": `stress`, with `vus` set to an upper bound
   - "sudden traffic burst": `spike`
   - "memory leak / long-run stability": `soak` (long `duration`)
   - "can it do N requests per second": `rate: N`
5. **Thresholds**: map the user's SLO to `p95_ms`, `p99_ms` and `max_error_rate`. The defaults are p95 < 500ms and errors < 1%.
6. **Report** in plain terms: verdict, RPS, p95/p99, error rate, the slowest endpoint (from `summary.endpoints`), and any failed checks or thresholds. Name the bottleneck, don't just dump numbers.
7. **Regressions**: keep the `run_id`. After a code change, rerun with the same parameters and `baseline_run_id`, or use `k6pane_report action=compare`.

## Multi-step flows

Use `requests` for user journeys (each VU runs them in order every iteration):

```json
{"requests": [
  {"name": "login", "method": "POST", "url": "http://localhost:3000/api/login", "json": {"email": "load@test.local", "password": "x"}},
  {"name": "feed",  "url": "http://localhost:3000/api/feed"}
], "profile": "load", "vus": 25, "duration": "2m", "p95_ms": 300}
```

For anything the structured mode can't express (auth tokens passed between steps, WebSockets, custom logic), write a k6 script and pass it as `script` (or save it in the repo and use `script_path`). Use `dry_run: true` to generate a starting script.

## Credentials

Never put a real token, password or API key into a tool call, and never ask the user to paste one into chat.

- The user stores the value once in `~/.hermes/.env` as `K6PANE_SECRET_<NAME>` (e.g. `K6PANE_SECRET_API_TOKEN=...`) and restarts Hermes.
- Structured mode: reference it as `${secret.API_TOKEN}` in a URL path or query, a header value, or a body, e.g. `"headers": {"Authorization": "Bearer ${secret.API_TOKEN}"}`. Placeholders are not allowed in the host.
- Custom scripts: pass `"secrets": ["API_TOKEN"]` and read it with `import secrets from 'k6/secrets'; const t = await secrets.get('API_TOKEN');` inside an `async` function.
- If a run reports `missing_secrets`, tell the user exactly which `K6PANE_SECRET_*` variable to set.
- Secret values reach k6 through a pipe (never disk or the command line), k6 redacts them from logs, and k6pane redacts every stored file and tool response. Literal values under sensitive keys (Authorization, Cookie, `api_key`, `password`, ...) are moved into protected values automatically, but prefer `${secret.NAME}`.
- k6 runs with a scrubbed environment: scripts cannot read the user's other environment variables. Pass non-secret settings through `env`.

## Rules

- k6 can only connect to the IPs k6pane validated (an IP fence blocks everything else, including redirects to other hosts). If results show `ip is blacklisted` errors, the target redirected or connected somewhere that wasn't validated: add that URL to the test explicitly (and the allowlist if it's remote) rather than trying to work around it.
- Only test systems the user owns or runs. Remote hosts are blocked unless the user lists them in `K6PANE_ALLOWED_HOSTS`. Never try to get around this; tell the user how to allowlist the host instead.
- Limits come from `K6PANE_MAX_VUS`, `K6PANE_MAX_DURATION` and `K6PANE_MAX_RPS`. If a request goes over a limit, tell the user rather than silently shrinking the test.
- `think_time: 0` gives maximum pressure; the default of 1s is closer to real users.
- A run blocks for its whole duration. Tell the user how long it will take before starting anything longer than a minute.
