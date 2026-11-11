"""Locate and execute the k6 binary."""

from __future__ import annotations

import os
import platform
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional

from .summary import TREND_STATS

# k6 exit codes (go.k6.io/k6/errext/exitcodes)
EXIT_CODES = {
    0: "ok",
    99: "thresholds_crossed",
    100: "setup_timeout",
    101: "teardown_timeout",
    102: "timeout",
    103: "engine_error",
    104: "invalid_config",
    105: "interrupted",
    107: "script_exception",
    108: "aborted_by_script",
}

_FALLBACK_PATHS = ("/opt/homebrew/bin/k6", "/usr/local/bin/k6", "/usr/bin/k6", "~/go/bin/k6")


def find_k6() -> Optional[str]:
    explicit = os.environ.get("K6PANE_K6_BIN")
    if explicit:
        p = Path(explicit).expanduser()
        return str(p) if p.is_file() and os.access(p, os.X_OK) else None
    found = shutil.which("k6")
    if found:
        return found
    for candidate in _FALLBACK_PATHS:
        p = Path(candidate).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


def k6_version(binary: str) -> Optional[str]:
    try:
        out = subprocess.run([binary, "version"], capture_output=True, text=True, timeout=15)
        return (out.stdout or out.stderr).strip().splitlines()[0]
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


def install_hint() -> str:
    system = platform.system()
    if system == "Darwin":
        return "brew install k6"
    if system == "Windows":
        return "winget install k6 --source winget   (or: choco install k6)"
    return ("See https://grafana.com/docs/k6/latest/set-up/install-k6/ "
            "(Debian/Ubuntu: apt repo; Fedora: dnf; or: docker pull grafana/k6)")


def run_k6(
    binary: str,
    script: Path,
    summary_path: Path,
    log_path: Path,
    timeout_s: float,
    env: Optional[Dict[str, str]] = None,
    extra_args: Optional[List[str]] = None,
    cwd: Optional[Path] = None,
) -> Dict[str, object]:
    """Run `k6 run` and return exit info. On timeout, SIGINT lets k6 still write its summary."""
    cmd = [
        binary, "run", "--quiet", "--no-color", "--no-usage-report",
        "--summary-trend-stats", TREND_STATS,
        "--summary-export", str(summary_path),
    ]
    for key, value in (env or {}).items():
        cmd += ["-e", f"{key}={value}"]
    cmd += list(extra_args or [])
    cmd.append(str(script))

    started = time.monotonic()
    timed_out = False
    with open(log_path, "w") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=str(cwd) if cwd else None)
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    code = proc.returncode
    return {
        "exit_code": code,
        "exit_reason": "timeout" if timed_out else EXIT_CODES.get(code, f"exit_{code}"),
        "timed_out": timed_out,
        "elapsed_s": round(time.monotonic() - started, 1),
        "command": " ".join(cmd),
    }


def log_excerpt(log_path: Path, max_chars: int = 2500) -> Dict[str, object]:
    try:
        text = log_path.read_text(errors="replace")
    except OSError:
        return {"errors": [], "tail": ""}
    errors = [ln.strip() for ln in text.splitlines() if "level=error" in ln or "level=warning" in ln]
    return {"errors": errors[-15:], "tail": text[-max_chars:]}
