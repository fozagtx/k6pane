"""Tool handlers. Each takes (args: dict, **kwargs) and returns a JSON string; none raise."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List

from . import guard, redact, runner, scriptgen, store, summary
from .durations import parse_duration

_GRACE_S = 60
# A string literal assigned to a credential-looking key inside JS source.
_CRED_ASSIGN = re.compile(
    r"""(authorization|api[-_]?key|token|secret|password|passwd|bearer)['"]?\s*[:=]\s*(['"`])(.*?)\2""",
    re.IGNORECASE,
)


def _has_literal_cred(text: str) -> bool:
    for match in _CRED_ASSIGN.finditer(text or ""):
        value = match.group(3).strip()
        if "${" in value:
            continue
        bare = re.sub(r"^(bearer|basic|token)\s+", "", value, flags=re.IGNORECASE)
        if len(bare) >= 6:
            return True
    return False


def _ok(data: Dict[str, Any]) -> str:
    return json.dumps(data, default=str)


def _err(message: str, **extra: Any) -> str:
    return json.dumps({"error": message, **extra}, default=str)


# ---------------------------------------------------------------------------
# k6pane_doctor
# ---------------------------------------------------------------------------

def doctor(args: Dict[str, Any], **kwargs: Any) -> str:
    try:
        binary = runner.find_k6()
        return _ok({
            "k6_installed": bool(binary),
            "k6_path": binary,
            "k6_version": runner.k6_version(binary) if binary else None,
            "install_command": None if binary else runner.install_hint(),
            "limits": {
                "max_vus": guard.max_vus(),
                "max_duration_s": guard.max_duration_seconds(),
                "max_rps": guard.max_rps(),
            },
            "allowed_remote_hosts": guard.allowed_hosts(),
            "runs_dir": str(store.runs_dir()),
            "notes": "Targets must be localhost/private-network unless listed in K6PANE_ALLOWED_HOSTS.",
        })
    except Exception as exc:  # pragma: no cover - defensive
        return _err(f"doctor failed: {exc}")


# ---------------------------------------------------------------------------
# k6pane_run
# ---------------------------------------------------------------------------

def _int(args: Dict[str, Any], key: str, default: Any = None) -> Any:
    value = args.get(key, default)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"`{key}` must be an integer.") from None


def _build_spec(args: Dict[str, Any]) -> Dict[str, Any]:
    raw_requests = args.get("requests")
    if not raw_requests and args.get("url"):
        raw_requests = [{k: args[k] for k in ("url", "method", "headers", "json", "body", "expect_status", "name")
                         if args.get(k) is not None}]
    requests = scriptgen.normalize_requests(raw_requests or [])

    profile = str(args.get("profile") or "load").lower()
    if profile not in scriptgen.PROFILES:
        raise ValueError(f"`profile` must be one of {', '.join(scriptgen.PROFILES)}.")
    default_duration = {"smoke": "30s", "soak": "30m"}.get(profile, "1m")
    vus = _int(args, "vus", 10)
    if vus < 1:
        raise ValueError("`vus` must be at least 1.")
    return {
        "requests": requests,
        "profile": profile,
        "vus": vus,
        "duration_s": parse_duration(args.get("duration") or default_duration),
        "stages": args.get("stages"),
        "rate": _int(args, "rate"),
        "max_vus": _int(args, "max_vus"),
        "thresholds": args.get("thresholds"),
        "p95_ms": args.get("p95_ms"),
        "p99_ms": args.get("p99_ms"),
        "max_error_rate": args.get("max_error_rate"),
        "think_time": args.get("think_time", 1),
        "request_timeout": args.get("request_timeout", "30s"),
    }


def _check_limits(planned_s: float, peak_vus: int, rate: Any) -> None:
    if peak_vus > guard.max_vus():
        raise ValueError(f"Peak VUs {peak_vus} exceeds K6PANE_MAX_VUS={guard.max_vus()}.")
    if planned_s > guard.max_duration_seconds():
        raise ValueError(f"Planned duration {int(planned_s)}s exceeds K6PANE_MAX_DURATION "
                         f"({int(guard.max_duration_seconds())}s).")
    if rate and int(rate) > guard.max_rps():
        raise ValueError(f"Rate {rate} req/s exceeds K6PANE_MAX_RPS={guard.max_rps()}.")


def run(args: Dict[str, Any], **kwargs: Any) -> str:
    try:
        return _run(args)
    except ValueError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err(f"k6pane_run failed: {type(exc).__name__}: {exc}")


def _run(args: Dict[str, Any]) -> str:
    binary = runner.find_k6()
    dry_run = bool(args.get("dry_run"))
    if not binary and not dry_run:
        return _err("k6 is not installed.", install_command=runner.install_hint())

    env = {str(k): str(v) for k, v in (args.get("env") or {}).items()}
    env, sensitive_env = redact.split_env(env)
    if sensitive_env:
        return _err(f"env keys {sensitive_env} look like credentials. Pass credentials as secrets instead: the user "
                    f"sets {redact.SECRET_ENV_PREFIX}<NAME> in ~/.hermes/.env, and you reference ${{secret.NAME}} "
                    "(structured mode) or list NAME in `secrets` and call secrets.get('NAME') (custom scripts).")
    bag = redact.SecretBag()
    script_text = args.get("script")
    script_path_arg = args.get("script_path")
    extra_args: List[str] = []
    warnings: List[str] = []
    cwd = None

    if script_text or script_path_arg:
        mode = "script"
        if script_path_arg:
            src = Path(str(script_path_arg)).expanduser().resolve()
            if not src.is_file():
                return _err(f"script_path not found: {src}")
            script_text = src.read_text()
            cwd = src.parent
        elif _has_literal_cred(script_text):
            return _err("The inline script contains what looks like a literal credential. Never inline secrets: "
                        "list the secret NAME in `secrets` and read it with "
                        "`import secrets from 'k6/secrets'; await secrets.get('NAME')`; the user provides "
                        f"the value as {redact.SECRET_ENV_PREFIX}NAME.")
        secret_names = [str(n) for n in (args.get("secrets") or [])]
        urls = guard.extract_urls(script_text) + guard.extract_urls(" ".join(env.values()))
        if not urls:
            warnings.append("Could not find any http(s) URL in the script or env to verify the target is local.")
        if args.get("vus") is not None:
            extra_args += ["--vus", str(_int(args, "vus"))]
        if args.get("duration"):
            extra_args += ["--duration", str(args["duration"])]
            planned = parse_duration(args["duration"])
        else:
            planned = guard.max_duration_seconds()
        if args.get("vus") is not None:
            _check_limits(0, _int(args, "vus"), None)
        spec_info: Dict[str, Any] = {"mode": "script", "source": str(script_path_arg or "inline")}
        profile = "custom-script"
    else:
        mode = "generated"
        spec = _build_spec(args)
        spec["requests"] = redact.secretize_requests(spec["requests"], bag)
        secret_names = redact.find_refs(spec["requests"], "secret")
        built = scriptgen.build_options(spec)
        _check_limits(built["planned_seconds"], built["peak_vus"], spec.get("rate"))
        script_text = scriptgen.render_script(spec, built["options"])
        urls = [r["url"] for r in spec["requests"]]
        planned = built["planned_seconds"]
        profile = spec["profile"] + (" (arrival-rate)" if spec.get("rate") else "")
        spec_info = {"mode": "generated", "peak_vus": built["peak_vus"], "options": built["options"],
                     "requests": [{k: r[k] for k in ("name", "method", "url", "expect_status")}
                                  for r in spec["requests"]]}

    hosts, blocked = guard.check_targets(urls)
    if blocked:
        return _err(guard.blocked_message(blocked), blocked_hosts=blocked)

    missing = bag.resolve_env(secret_names)
    if missing and not dry_run:
        return _err(f"Missing secret(s): {', '.join(missing)}. Ask the user to set "
                    + ", ".join(redact.SECRET_ENV_PREFIX + m for m in missing)
                    + " in ~/.hermes/.env and restart Hermes. Never ask the user to paste the value into chat.",
                    missing_secrets=missing)
    secret_values = list(bag.values.values())

    if dry_run:
        return _ok({"dry_run": True, "mode": mode, "profile": profile, "planned_seconds": planned,
                    "target_hosts": hosts, "warnings": warnings, "script": script_text})

    label = str(args.get("label") or (hosts[0] if hosts else "test"))
    run_dir = store.new_run_dir(label)
    script_file = run_dir / "script.js"
    script_file.write_text(redact.redact_text(script_text, secret_values) if mode == "script" else script_text)
    exec_script = Path(str(script_path_arg)).expanduser().resolve() if script_path_arg else script_file

    started_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    info = runner.run_k6(
        binary, exec_script, run_dir / "summary.json", run_dir / "k6.log",
        timeout_s=planned + _GRACE_S, env=env, extra_args=extra_args, cwd=cwd,
        secret_bytes=bag.secret_file_bytes(),
    )
    redact.redact_file(run_dir / "summary.json", secret_values)
    redact.redact_file(run_dir / "k6.log", secret_values)
    export = store.read_json(run_dir / "summary.json")
    compact = summary.summarize(export) if export else None
    logs = runner.log_excerpt(run_dir / "k6.log")

    if compact is None:
        verdict = "error"
    elif info["exit_code"] == 0 and compact["thresholds_passed"]:
        verdict = "pass"
    elif info["exit_code"] in (0, 99):
        verdict = "fail"
    else:
        verdict = "error"

    result: Dict[str, Any] = {
        "run_id": run_dir.name,
        "label": label,
        "verdict": verdict,
        "profile": profile,
        "started_at": started_at,
        "target_hosts": hosts,
        "k6": info,
        "summary": compact,
        "spec": spec_info,
        "warnings": warnings,
        "secrets_used": [n for n in secret_names if not n.startswith("AUTO_")],
        "auto_protected_values": sum(1 for n in bag.values if n.startswith("AUTO_")),
        "artifacts": {"dir": str(run_dir), "script": str(script_file),
                      "summary_export": str(run_dir / "summary.json"), "log": str(run_dir / "k6.log")},
    }
    if verdict == "error" or logs["errors"]:
        result["k6_errors"] = logs["errors"]
    if verdict == "error":
        result["log_tail"] = logs["tail"]

    baseline_id = args.get("baseline_run_id")
    if baseline_id and compact:
        base = store.load_run(str(baseline_id))
        if base and base.get("summary"):
            result["comparison"] = summary.compare(compact, base["summary"])
        else:
            result["comparison_error"] = f"Baseline run {baseline_id!r} not found or has no summary."

    result = redact.redact(result, secret_values)
    store.write_json(run_dir / "result.json", result)
    return _ok(result)


# ---------------------------------------------------------------------------
# k6pane_report
# ---------------------------------------------------------------------------

def report(args: Dict[str, Any], **kwargs: Any) -> str:
    try:
        action = str(args.get("action") or "list").lower()
        if action == "list":
            limit = max(1, min(int(args.get("limit") or 10), 100))
            return _ok({"runs_dir": str(store.runs_dir()), "runs": store.list_runs(limit)})

        run_id = args.get("run_id")
        if not run_id:
            return _err(f"`run_id` is required for action={action}.")
        current = store.load_run(str(run_id))
        if not current:
            return _err(f"Run {run_id!r} not found. Use action=list to see run ids.")

        if action == "get":
            if args.get("include_script"):
                path = store.run_path(str(run_id))
                current["script"] = (path / "script.js").read_text() if path else None
            return _ok(current)

        if action == "compare":
            baseline_id = args.get("baseline_run_id")
            if not baseline_id:
                return _err("`baseline_run_id` is required for action=compare.")
            base = store.load_run(str(baseline_id))
            if not base:
                return _err(f"Baseline run {baseline_id!r} not found.")
            if not current.get("summary") or not base.get("summary"):
                return _err("One of the runs has no summary (it errored before k6 produced results).")
            tol = float(args.get("tolerance_pct") or 10)
            return _ok({"run_id": run_id, "baseline_run_id": baseline_id,
                        **summary.compare(current["summary"], base["summary"], tol)})

        return _err("`action` must be one of: list, get, compare.")
    except Exception as exc:
        return _err(f"k6pane_report failed: {type(exc).__name__}: {exc}")
