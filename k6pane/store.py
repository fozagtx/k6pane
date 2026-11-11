"""Run storage: one directory per run under the Hermes plugin data dir (<HERMES_HOME>/plugin-data/k6pane/runs)."""

from __future__ import annotations

import contextlib
import json
import re
import secrets
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from . import hermes_host

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None
    import msvcrt

_RUN_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


def runs_dir() -> Path:
    path = hermes_host.data_dir() / "runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def new_run_id(label: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", label).strip("-").lower()[:40] or "run"
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}-{secrets.token_hex(2)}"


def create_run_dir(run_id: str) -> Path:
    path = runs_dir() / run_id
    path.mkdir(parents=True)
    return path


def new_run_dir(label: str) -> Path:
    return create_run_dir(new_run_id(label))


def run_path(run_id: str) -> Optional[Path]:
    if not run_id or not _RUN_ID.match(run_id):
        return None
    path = runs_dir() / run_id
    return path if path.is_dir() else None


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, default=str))


def read_json(path: Path) -> Optional[Any]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def load_run(run_id: str) -> Optional[Dict[str, Any]]:
    path = run_path(run_id)
    if not path:
        return None
    return read_json(path / "result.json")


def list_runs(limit: int = 10) -> List[Dict[str, Any]]:
    entries = sorted((p for p in runs_dir().iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)
    out = []
    for p in entries:
        result = read_json(p / "result.json")
        if not result:
            continue
        s = result.get("summary") or {}
        out.append({
            "run_id": p.name,
            "label": result.get("label"),
            "verdict": result.get("verdict"),
            "started_at": result.get("started_at"),
            "profile": result.get("profile"),
            "rps": s.get("rps"),
            "p95_ms": (s.get("latency_ms") or {}).get("p95"),
            "error_rate": s.get("error_rate"),
        })
        if len(out) >= limit:
            break
    return out


class RunInProgress(Exception):
    def __init__(self, run_id: str):
        super().__init__(run_id)
        self.run_id = run_id


@contextlib.contextmanager
def run_lock(run_id: str) -> Iterator[None]:
    """Single-flight lock across processes. Concurrent k6 runs on one machine corrupt
    each other's measurements, so a second run fails fast instead of queueing.
    The OS releases the lock if the holder dies, so it can never go stale."""
    path = runs_dir().parent / "run.lock"
    fh = open(path, "a+")
    try:
        try:
            if fcntl:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:  # pragma: no cover
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            fh.seek(0)
            holder = fh.read().strip() or "unknown"
            raise RunInProgress(holder) from None
        fh.seek(0)
        fh.truncate()
        fh.write(run_id)
        fh.flush()
        try:
            yield
        finally:
            fh.seek(0)
            fh.truncate()
            fh.flush()
            if fcntl:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()
