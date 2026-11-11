"""Target and size guardrails.

k6pane is meant for load testing systems you run yourself. By default only
targets that resolve to loopback / private / link-local addresses are allowed.
Remote hosts must be listed by the user in K6PANE_ALLOWED_HOSTS (comma separated,
`*.example.com` wildcards allowed). The model cannot widen this list itself.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import os
import re
import socket
from typing import Iterable, List, Tuple
from urllib.parse import urlsplit

from .durations import parse_duration

DEFAULT_MAX_VUS = 200
DEFAULT_MAX_DURATION = "30m"
DEFAULT_MAX_RPS = 2000

_URL_RE = re.compile(r"""https?://[^\s'"`)<>\]}]+""", re.IGNORECASE)


def max_vus() -> int:
    try:
        return max(1, int(os.environ.get("K6PANE_MAX_VUS", DEFAULT_MAX_VUS)))
    except ValueError:
        return DEFAULT_MAX_VUS


def max_duration_seconds() -> float:
    try:
        return parse_duration(os.environ.get("K6PANE_MAX_DURATION", DEFAULT_MAX_DURATION))
    except ValueError:
        return parse_duration(DEFAULT_MAX_DURATION)


def max_rps() -> int:
    try:
        return max(1, int(os.environ.get("K6PANE_MAX_RPS", DEFAULT_MAX_RPS)))
    except ValueError:
        return DEFAULT_MAX_RPS


def allowed_hosts() -> List[str]:
    raw = os.environ.get("K6PANE_ALLOWED_HOSTS", "")
    return [h.strip().lower() for h in raw.split(",") if h.strip()]


def extract_urls(text: str) -> List[str]:
    return [u.rstrip(".,;") for u in _URL_RE.findall(text or "")]


def _is_local_ip(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def _host_is_local(host: str) -> bool:
    host = host.lower().strip("[]")
    if host in ("localhost",) or host.endswith(".localhost"):
        return True
    if _is_local_ip(host):
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return False
    addrs = {info[4][0] for info in infos}
    return bool(addrs) and all(_is_local_ip(a) for a in addrs)


def _host_is_allowlisted(host: str, patterns: Iterable[str]) -> bool:
    host = host.lower()
    return any(fnmatch.fnmatch(host, p) for p in patterns)


def check_targets(urls: Iterable[str]) -> Tuple[List[str], List[str]]:
    """Return (hosts, blocked_hosts) for the given URLs."""
    patterns = allowed_hosts()
    hosts: List[str] = []
    blocked: List[str] = []
    for url in urls:
        host = urlsplit(url).hostname
        if not host or host in hosts:
            continue
        hosts.append(host)
        if not (_host_is_local(host) or _host_is_allowlisted(host, patterns)):
            blocked.append(host)
    return hosts, blocked


def blocked_message(blocked: List[str]) -> str:
    return (
        f"Refusing to load test non-local host(s): {', '.join(blocked)}. "
        "k6pane only targets localhost/private networks by default. If you own or are "
        "authorized to test these hosts, the user must add them to the "
        "K6PANE_ALLOWED_HOSTS environment variable (e.g. in ~/.hermes/.env: "
        f"K6PANE_ALLOWED_HOSTS={','.join(blocked)}) and restart Hermes."
    )
