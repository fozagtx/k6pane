"""Target and size guardrails.

k6pane is meant for load testing systems you run yourself. By default only
targets that resolve to loopback / private / link-local addresses are allowed.
Remote hosts must be listed by the user in K6PANE_ALLOWED_HOSTS (comma separated,
`*.example.com` wildcards allowed). The model cannot widen this list itself.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import re
import socket
from typing import Iterable, List, Tuple
from urllib.parse import urlsplit

from . import hermes_host
from .durations import parse_duration

DEFAULT_MAX_VUS = 200
DEFAULT_MAX_DURATION = "30m"
DEFAULT_MAX_RPS = 2000

_URL_RE = re.compile(r"""(?:https?|wss?)://[^\s'"`)<>\]}]+""", re.IGNORECASE)


def max_vus() -> int:
    try:
        return max(1, int(hermes_host.setting("max_vus", "K6PANE_MAX_VUS", DEFAULT_MAX_VUS)))
    except ValueError:
        return DEFAULT_MAX_VUS


def max_duration_seconds() -> float:
    try:
        return parse_duration(hermes_host.setting("max_duration", "K6PANE_MAX_DURATION", DEFAULT_MAX_DURATION))
    except ValueError:
        return parse_duration(DEFAULT_MAX_DURATION)


def max_rps() -> int:
    try:
        return max(1, int(hermes_host.setting("max_rps", "K6PANE_MAX_RPS", DEFAULT_MAX_RPS)))
    except ValueError:
        return DEFAULT_MAX_RPS


def allowed_hosts() -> List[str]:
    raw = hermes_host.setting("allowed_hosts", "K6PANE_ALLOWED_HOSTS", "")
    items = raw if isinstance(raw, list) else str(raw).split(",")
    return [str(h).strip().lower() for h in items if str(h).strip()]


def extract_urls(text: str) -> List[str]:
    return [u.rstrip(".,;") for u in _URL_RE.findall(text or "")]


def _is_local_ip(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def resolve_host(host: str) -> List[str]:
    """Return every IP address `host` resolves to (or the host itself if it is an IP literal)."""
    host = host.lower().strip("[]")
    try:
        return [str(ipaddress.ip_address(host.split("%", 1)[0]))]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return []
    return sorted({info[4][0].split("%", 1)[0] for info in infos})


def fence(ips: Iterable[str]) -> List[str]:
    """CIDR ranges k6 must refuse to connect to: everything except the validated IPs.

    Pass the IPs returned by `validate_targets` (never re-resolve), so the
    addresses that were checked are exactly the ones k6 may reach.

    k6 checks the address it actually dials, so this one fence also stops
    redirects to other hosts, DNS rebinding between validation and execution,
    and custom scripts that build URLs at runtime.
    """
    allowed = []
    for addr in ips:
        ip = ipaddress.ip_address(addr)
        allowed.append(ipaddress.ip_network(f"{ip}/{ip.max_prefixlen}"))
    blocked = []
    for universe in (ipaddress.ip_network("0.0.0.0/0"), ipaddress.ip_network("::/0")):
        nets = [universe]
        for keep in (a for a in allowed if a.version == universe.version):
            next_nets = []
            for net in nets:
                if keep.subnet_of(net):
                    next_nets.extend(net.address_exclude(keep))
                else:
                    next_nets.append(net)
            nets = next_nets
        blocked.extend(sorted(nets))
    return [str(n) for n in blocked]


def _host_is_allowlisted(host: str, patterns: Iterable[str]) -> bool:
    host = host.lower()
    return any(fnmatch.fnmatch(host, p) for p in patterns)


def validate_targets(urls: Iterable[str]) -> Tuple[List[str], List[str], List[str]]:
    """Resolve each target host once. Return (hosts, blocked_hosts, validated_ips).

    A host is allowed if it is `localhost`, or every address it resolves to is
    loopback/private/link-local, or it matches K6PANE_ALLOWED_HOSTS. Unresolvable
    hosts are blocked.
    """
    patterns = allowed_hosts()
    hosts: List[str] = []
    blocked: List[str] = []
    ips: List[str] = []
    for url in urls:
        host = urlsplit(url).hostname
        if not host or host in hosts:
            continue
        hosts.append(host)
        addrs = resolve_host(host)
        name = host.lower().strip("[]")
        local = bool(addrs) and all(_is_local_ip(a) for a in addrs)
        if name == "localhost" or name.endswith(".localhost"):
            local = bool(addrs) and all(ipaddress.ip_address(a).is_loopback for a in addrs)
        if not addrs or not (local or _host_is_allowlisted(host, patterns)):
            blocked.append(host)
            continue
        ips.extend(a for a in addrs if a not in ips)
    return hosts, blocked, ips


def check_targets(urls: Iterable[str]) -> Tuple[List[str], List[str]]:
    """Return (hosts, blocked_hosts) for the given URLs."""
    hosts, blocked, _ = validate_targets(urls)
    return hosts, blocked


def blocked_message(blocked: List[str]) -> str:
    return (
        f"Refusing to load test host(s) that are non-local or could not be resolved: {', '.join(blocked)}. "
        "k6pane only targets localhost/private networks by default. If you own or are "
        "authorized to test these hosts, the user must add them to k6pane's allowed_hosts setting "
        f"(hermes config set plugins.entries.k6pane.settings.allowed_hosts '[{', '.join(blocked)}]') or the "
        "K6PANE_ALLOWED_HOSTS environment variable, then restart Hermes. Do not change this setting yourself."
    )
