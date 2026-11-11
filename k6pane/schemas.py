"""Tool schemas: what the model reads to decide when and how to call k6pane."""

_REQUEST = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "description": "Full URL, e.g. http://localhost:3000/api/login"},
        "method": {"type": "string", "description": "HTTP method (default GET)."},
        "name": {"type": "string", "description": "Short endpoint label used in per-endpoint results."},
        "headers": {"type": "object", "description": "Request headers."},
        "json": {"description": "JSON body (sets Content-Type: application/json)."},
        "body": {"type": "string", "description": "Raw string body (use instead of json)."},
        "expect_status": {"description": "Expected status code or list of codes (default 200)."},
        "sleep": {"type": "number", "description": "Seconds to pause after this request."},
    },
    "required": ["url"],
}

DOCTOR = {
    "name": "k6pane_doctor",
    "description": (
        "Check the local k6 load-testing setup: whether k6 is installed (and how to install it), "
        "its version, safety limits (max VUs, duration, RPS), which remote hosts are allowed, and "
        "where run results are stored. Call this first if a k6pane_run fails or before the first test."
    ),
    "parameters": {"type": "object", "properties": {}},
}

RUN = {
    "name": "k6pane_run",
    "description": (
        "Run a k6 load test locally and return parsed results: verdict (pass/fail/error), RPS, "
        "error rate, latency avg/p50/p90/p95/p99/max in ms, per-endpoint breakdown, threshold results, "
        "and a run_id for later comparison. Two modes: (1) structured: give `url` or `requests` plus "
        "`profile`/`vus`/`duration` and k6pane writes the script; (2) custom: give `script` (k6 JavaScript) "
        "or `script_path`. Profiles: smoke (1 VU sanity check), load (ramp to vus, hold, ramp down), "
        "stress (step up to vus), spike (sudden jump to vus), soak (long steady hold), constant (flat vus). "
        "Set `rate` for a fixed requests-per-second test. Targets must be localhost/private-network "
        "unless the user allowlisted the host in K6PANE_ALLOWED_HOSTS. Start with a smoke test before "
        "heavy load. The call blocks for the whole test duration. Use dry_run to just generate the script."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Single target URL (shortcut for one-request tests)."},
            "method": {"type": "string", "description": "HTTP method for `url` (default GET)."},
            "headers": {"type": "object", "description": "Headers for `url`."},
            "json": {"description": "JSON body for `url`."},
            "body": {"type": "string", "description": "Raw body for `url`."},
            "expect_status": {"description": "Expected status for `url` (int or list, default 200)."},
            "requests": {
                "type": "array", "items": _REQUEST,
                "description": "Multi-step user flow; each VU iteration runs these in order. Overrides `url`.",
            },
            "profile": {"type": "string", "enum": ["smoke", "load", "stress", "spike", "soak", "constant"],
                        "description": "Load shape (default load)."},
            "vus": {"type": "integer", "description": "Peak virtual users (default 10)."},
            "duration": {"type": "string",
                         "description": "Total test length, e.g. '30s', '5m', '1h' (default 1m; smoke 30s; soak 30m)."},
            "stages": {
                "type": "array",
                "items": {"type": "object", "properties": {"duration": {"type": "string"}, "target": {"type": "integer"}},
                          "required": ["duration", "target"]},
                "description": "Explicit ramp stages; overrides profile.",
            },
            "rate": {"type": "integer", "description": "Fixed arrival rate in requests (iterations) per second."},
            "max_vus": {"type": "integer", "description": "VU ceiling for `rate` mode (default 2x vus)."},
            "p95_ms": {"type": "number", "description": "Pass threshold: p95 latency below this (default 500)."},
            "p99_ms": {"type": "number", "description": "Optional pass threshold for p99 latency."},
            "max_error_rate": {"type": "number", "description": "Pass threshold: failed request ratio (default 0.01)."},
            "thresholds": {"type": "object",
                           "description": "Raw k6 thresholds, e.g. {\"http_req_duration\": [\"p(95)<300\"]}. Overrides p95_ms etc."},
            "think_time": {"type": "number", "description": "Seconds each VU sleeps between iterations (default 1; 0 for max pressure)."},
            "request_timeout": {"type": "string", "description": "Per-request timeout (default 30s)."},
            "script": {"type": "string", "description": "Full k6 JavaScript test script (custom mode)."},
            "script_path": {"type": "string", "description": "Path to an existing k6 script on disk (custom mode)."},
            "env": {"type": "object", "description": "Variables passed to the script as __ENV (e.g. BASE_URL)."},
            "label": {"type": "string", "description": "Human label for this run (used in run_id)."},
            "baseline_run_id": {"type": "string", "description": "Compare results against this earlier run."},
            "dry_run": {"type": "boolean", "description": "Generate and return the script without running it."},
        },
    },
}

REPORT = {
    "name": "k6pane_report",
    "description": (
        "Read stored k6pane load-test results. action=list shows recent runs (verdict, RPS, p95, errors); "
        "action=get returns one run's full result (include_script to also get the script); "
        "action=compare diffs a run against a baseline run and flags latency/RPS/error regressions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "get", "compare"], "description": "Default list."},
            "run_id": {"type": "string", "description": "Run to fetch or compare."},
            "baseline_run_id": {"type": "string", "description": "Baseline for action=compare."},
            "tolerance_pct": {"type": "number", "description": "Change % treated as regression (default 10)."},
            "include_script": {"type": "boolean", "description": "With action=get, include the k6 script."},
            "limit": {"type": "integer", "description": "With action=list, number of runs (default 10)."},
        },
    },
}
