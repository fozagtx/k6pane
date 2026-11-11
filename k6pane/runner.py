"""Locate and execute the k6 binary."""

from __future__ import annotations

import os
import platform
import shutil
import signal
import subprocess
import threading
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


# Variables k6 itself may need from the parent environment. Everything else
# (API keys, cloud tokens, the user's whole shell env) is withheld from k6.
_PASSTHROUGH_ENV = ("PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "SYSTEMROOT", "USERPROFILE")


def child_env(script_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = {k: os.environ[k] for k in _PASSTHROUGH_ENV if k in os.environ}
    env.update(script_env or {})
    return env


def _feed_pipe(fd: int, data: bytes) -> None:
    try:
        with os.fdopen(fd, "wb") as w:
            w.write(data)
    except OSError:
        pass


def run_k6(
    binary: str,
    script: Path,
    summary_path: Path,
    log_path: Path,
    timeout_s: float,
    env: Optional[Dict[str, str]] = None,
    extra_args: Optional[List[str]] = None,
    cwd: Optional[Path] = None,
    secret_bytes: bytes = b"",
) -> Dict[str, object]:
    """Run `k6 run` and return exit info. On timeout, SIGINT lets k6 still write its summary.

    `env` reaches the script as __ENV through a scrubbed process environment (never argv).
    `secret_bytes` (KEY=VALUE lines) reaches k6's secret source through an inherited pipe,
    so secret values never touch disk or the command line, and k6 redacts them from logs.
    """
    cmd = [
        binary, "run", "--quiet", "--no-color", "--no-usage-report",
        "--summary-trend-stats", TREND_STATS,
        "--summary-export", str(summary_path),
    ]
    pass_fds: tuple = ()
    writer = None
    read_fd = write_fd = None
    if secret_bytes:
        read_fd, write_fd = os.pipe()
        pass_fds = (read_fd,)
        cmd.append(f"--secret-source=file=/dev/fd/{read_fd}")
        writer = threading.Thread(target=_feed_pipe, args=(write_fd, secret_bytes), daemon=True)
    cmd += list(extra_args or [])
    cmd.append(str(script))

    started = time.monotonic()
    timed_out = False
    try:
        with open(log_path, "w") as log:
            proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=str(cwd) if cwd else None,
                                    env=child_env(env), pass_fds=pass_fds)
            if read_fd is not None:
                os.close(read_fd)
                read_fd = None
            if writer:
                writer.start()
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
    finally:
        if read_fd is not None:
            os.close(read_fd)
        if writer is not None and not writer.is_alive() and writer.ident is None and write_fd is not None:
            os.close(write_fd)  # k6 never started; the writer thread never took ownership
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
