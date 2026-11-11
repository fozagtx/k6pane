"""Run storage: one directory per run under $K6PANE_HOME (default ~/.hermes/k6pane/runs)."""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_RUN_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


def runs_dir() -> Path:
    home = os.environ.get("K6PANE_HOME")
    if home:
        base = Path(home).expanduser()
    else:
        base = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser() / "k6pane"
    path = base / "runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def new_run_dir(label: str) -> Path:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", label).strip("-").lower()[:40] or "run"
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}-{secrets.token_hex(2)}"
    path = runs_dir() / run_id
    path.mkdir(parents=True)
    return path


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
