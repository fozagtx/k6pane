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

## Rules

- Only test systems the user owns or runs. Remote hosts are blocked unless the user lists them in `K6PANE_ALLOWED_HOSTS`. Never try to get around this; tell the user how to allowlist the host instead.
- Limits come from `K6PANE_MAX_VUS`, `K6PANE_MAX_DURATION` and `K6PANE_MAX_RPS`. If a request goes over a limit, tell the user rather than silently shrinking the test.
- `think_time: 0` gives maximum pressure; the default of 1s is closer to real users.
- A run blocks for its whole duration. Tell the user how long it will take before starting anything longer than a minute.
