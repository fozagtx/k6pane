"""Secret references, auto-secretizing of literal credentials, and redaction.

Rules:
- `${secret.NAME}` in a URL path/query, header value or body resolves at run
  time from the user's `K6PANE_SECRET_NAME` env var. The model cannot reach
  any other env var through it.
- A literal value under a sensitive header or key (Authorization, Cookie,
  api_key, password, ...) is moved into an auto-generated secret so the
  literal never lands in script.js.
- Everything persisted or returned to the model goes through `redact()`.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import hermes_host

MASK = "***REDACTED***"
SECRET_ENV_PREFIX = "K6PANE_SECRET_"
SENSITIVE_HEADERS = {"authorization", "cookie", "set-cookie", "proxy-authorization"}
SENSITIVE_KEY = re.compile(r"(api[-_]?key|token|secret|password|passwd|bearer)", re.IGNORECASE)
REF = re.compile(r"\$\{(secret|var|row)\.([A-Za-z0-9_]+)\}")
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


def is_sensitive_key(key: str) -> bool:
    k = str(key)
    return k.lower() in SENSITIVE_HEADERS or bool(SENSITIVE_KEY.search(k))


def find_refs(value: Any, namespace: str) -> List[str]:
    found: List[str] = []

    def walk(v: Any) -> None:
        if isinstance(v, str):
            found.extend(name for ns, name in REF.findall(v) if ns == namespace)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(value)
    return list(dict.fromkeys(found))


def _is_reference(value: Any) -> bool:
    return isinstance(value, str) and bool(REF.search(value))


class SecretBag:
    """Collects secret values for one run (env-resolved + auto-generated)."""

    def __init__(self) -> None:
        self.values: Dict[str, str] = {}
        self._auto = 0

    def auto(self, literal: str) -> str:
        for name, value in self.values.items():
            if value == literal and name.startswith("AUTO_"):
                return f"${{secret.{name}}}"
        self._auto += 1
        name = f"AUTO_{self._auto}"
        self.values[name] = literal
        return f"${{secret.{name}}}"

    def resolve_env(self, names: Iterable[str]) -> List[str]:
        """Load `K6PANE_SECRET_<NAME>` for each name; return the missing ones."""
        missing = []
        for name in names:
            if name.startswith("AUTO_"):
                continue
            if not _NAME.match(name):
                raise ValueError(f"Invalid secret name {name!r} (letters, digits, underscore).")
            try:
                value = hermes_host.get_secret(SECRET_ENV_PREFIX + name)
            except hermes_host.SecretUnavailable as exc:
                raise ValueError(str(exc)) from None
            if value is None:
                missing.append(name)
            else:
                self.values[name] = value
        for name, value in self.values.items():
            if "\n" in value or "\r" in value:
                raise ValueError(f"Secret {name} contains a newline, which k6 secret files cannot carry.")
        return missing

    def secret_file_bytes(self) -> bytes:
        return "".join(f"{k}={v}\n" for k, v in self.values.items()).encode()


def _secretize_url(url: str, bag: SecretBag) -> str:
    parts = urlsplit(url)
    if not parts.query:
        return url
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    changed = False
    out = []
    for k, v in pairs:
        if is_sensitive_key(k) and v and not _is_reference(v):
            v = bag.auto(v)
            changed = True
        out.append((k, v))
    if not changed:
        return url
    query = urlencode(out, safe="${}.")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def _secretize_tree(value: Any, bag: SecretBag) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if is_sensitive_key(k) and isinstance(v, (str, int, float)) and not isinstance(v, bool) \
                    and str(v) and not _is_reference(v):
                out[k] = bag.auto(str(v))
            else:
                out[k] = _secretize_tree(v, bag)
        return out
    if isinstance(value, list):
        return [_secretize_tree(v, bag) for v in value]
    return value


def secretize_requests(requests: List[Dict[str, Any]], bag: SecretBag) -> List[Dict[str, Any]]:
    """Move literal credentials out of the spec into `bag`; return the rewritten requests."""
    out = []
    for r in requests:
        r = dict(r)
        r["url"] = _secretize_url(r["url"], bag)
        r["headers"] = _secretize_tree(r.get("headers") or {}, bag)
        if r.get("json") is not None:
            r["json"] = _secretize_tree(r["json"], bag)
        out.append(r)
    return out


def check_host_is_literal(url: str) -> None:
    netloc = urlsplit(url).netloc
    scheme = url.split("://", 1)[0]
    if "${" in netloc or "${" in scheme:
        raise ValueError(f"Placeholders are not allowed in the URL scheme or host ({url!r}); "
                         "the target host must be literal so it can be validated.")


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

def _redact_str(text: str, values: Iterable[str]) -> str:
    for v in sorted({v for v in values if v and len(v) >= 3}, key=len, reverse=True):
        text = text.replace(v, MASK)
    return text


def _redact_query(text: str) -> str:
    return re.sub(
        r"([?&](?:[^=&\s]*?(?:api[-_]?key|token|secret|password|passwd|bearer)[^=&\s]*)=)([^&\s\"']+)",
        lambda m: m.group(1) + (m.group(2) if m.group(2).startswith("${") else MASK),
        text, flags=re.IGNORECASE,
    )


def _redact_header_lines(text: str) -> str:
    return re.sub(
        r"(?im)^(\s*(?:authorization|cookie|set-cookie|proxy-authorization)\s*:\s*)(.+)$",
        lambda m: m.group(1) + MASK, text,
    )


def redact_text(text: str, values: Iterable[str] = ()) -> str:
    return _redact_header_lines(_redact_query(_redact_str(text, values)))


def redact(obj: Any, values: Iterable[str] = ()) -> Any:
    """Recursively redact secret values, sensitive keys and sensitive query params."""
    values = list(values)

    def walk(v: Any, key: str = "") -> Any:
        if key and is_sensitive_key(key) and not isinstance(v, (dict, list)) and v is not None:
            if _is_reference(v):
                return redact_text(v, values)
            return MASK
        if isinstance(v, dict):
            return {k: walk(x, str(k)) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, str):
            return redact_text(v, values)
        return v

    return walk(obj)


def redact_file(path, values: Iterable[str]) -> None:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return
    cleaned = redact_text(text, values)
    if cleaned != text:
        path.write_text(cleaned)


def split_env(env: Dict[str, str]) -> Tuple[Dict[str, str], List[str]]:
    """Return (plain env, names of env keys that look sensitive and must use secrets instead)."""
    bad = [k for k in env if is_sensitive_key(k)]
    return {k: v for k, v in env.items() if k not in bad}, bad
