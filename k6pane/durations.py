"""Parse and format k6-style durations ("30s", "5m", "1h30m", "2m30s", "1500ms")."""

from __future__ import annotations

import re

_UNIT_SECONDS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}
_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h)")


def parse_duration(value) -> float:
    """Return seconds for a k6 duration string or a bare number of seconds."""
    if isinstance(value, bool):
        raise ValueError(f"Invalid duration: {value!r}")
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError(f"Duration must be positive: {value!r}")
        return float(value)
    text = str(value).strip().lower().replace(" ", "")
    if not text:
        raise ValueError("Empty duration")
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    pos = 0
    total = 0.0
    for match in _PART.finditer(text):
        if match.start() != pos:
            raise ValueError(f"Invalid duration: {value!r}")
        total += float(match.group(1)) * _UNIT_SECONDS[match.group(2)]
        pos = match.end()
    if pos != len(text):
        raise ValueError(f"Invalid duration: {value!r} (use forms like 30s, 5m, 1h30m)")
    return total


def format_seconds(seconds: float) -> str:
    """Format whole seconds as a k6 duration string (minimum 1s)."""
    return f"{max(1, int(round(seconds)))}s"
