"""Outbound-URL guard for agent-driven navigation and proxied requests.

Agent-supplied URLs reach two genuinely dangerous sinks: the AWI Playwright
bridge (a real headless browser that will happily open ``file://`` and
intranet URLs) and the behavioral sandbox's HTTP proxy mode (a raw
``aiohttp`` request). Both run inside our infrastructure, so an unchecked
URL is an SSRF primitive against link-local metadata services, RFC1918
networks, and the host filesystem.

``check_outbound_url`` returns ``None`` when the URL is safe to fetch and a
short machine-readable reason string when it must be blocked. Scheme and
literal-address checks are unconditional; hostname checks resolve DNS and
block names that resolve to non-global addresses. Resolution failure fails
closed (``dns_resolution_failed``): the guard cannot vouch for a name it
could not resolve, and the fetching client (a headless browser with its own
resolver, or a later retry) may well resolve it — possibly to an intranet
address. This matches the upstream MCP URL guard. Tests that exercise
public hostnames pin ``_resolve_host`` instead of relying on live DNS.

``pin_outbound_url`` goes one step further and returns the validated IP
address to connect to alongside the verdict. Hostname validation resolves
DNS exactly once, so callers that connect to the returned pinned address
(rather than resolving the hostname again) are not exposed to DNS
rebinding between validation and connection: the IP that passed validation
is the one connected to. This mirrors the upstream MCP pinned transport.
Callers that cannot control connection DNS (a headless browser) must still
revalidate immediately before use. Either way this guard is a pre-flight
check, not a substitute for network-level egress policy.

Setting ``ALLOW_PRIVATE_NETWORK_TARGETS=true`` (local development against
mock servers) skips only the private-address checks; non-http(s) schemes
such as ``file://`` and ``javascript:`` are never allowed.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Sequence
from urllib.parse import urlsplit

from app.core.config import get_settings

_ALLOWED_SCHEMES = {"http", "https"}
_BLOCKED_HOSTNAMES = {"localhost", "metadata.google.internal"}
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal")


def _address_blocked(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    # is_global is False for loopback, RFC1918/ULA, link-local (including
    # 169.254.169.254 metadata), CGNAT shared space, reserved, and unspecified
    # addresses. Multicast needs an explicit check: it can be classified global.
    return ip.is_multicast or not ip.is_global


async def _resolve_host(host: str):
    """Resolve a hostname through the event loop; split out for deterministic tests."""

    return await asyncio.get_running_loop().getaddrinfo(
        host, None, type=socket.SOCK_STREAM
    )


async def _default_resolver(host: str) -> Sequence[str]:
    """Resolve a hostname to address strings via the monkeypatchable hook."""
    infos = await _resolve_host(host)
    return tuple(dict.fromkeys(str(info[4][0]) for info in infos))


async def pin_outbound_url(
    url: str,
    *,
    resolver: Callable[[str], Awaitable[Sequence[str]]] | None = None,
) -> tuple[str | None, str | None]:
    """Validate an outbound URL and return the IP address to connect to.

    Returns ``(block_reason, pinned_address)``. When the URL is safe,
    ``block_reason`` is None and ``pinned_address`` is the IP that passed
    validation: the literal itself for literal hosts, or the first resolved
    address for hostnames. Hostnames are resolved exactly once and every
    resolved address must be globally routable, so connecting to the pinned
    address (instead of resolving again) closes DNS rebinding between
    validation and connection. When the URL is blocked, the pinned address
    is None. The optional ``resolver`` override exists for tests.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "unparseable_url", None

    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        return "scheme_not_allowed", None

    host = parts.hostname
    if not host:
        return "missing_host", None

    if get_settings().ALLOW_PRIVATE_NETWORK_TARGETS:
        return None, None

    host = host.strip("[]").rstrip(".").lower()
    if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_SUFFIXES):
        return "private_host_blocked", None

    if _address_blocked(host):
        return "private_address_blocked", None

    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return None, host

    # A hostname, not a literal address: resolve it once and require every
    # resolved address to be globally routable.
    try:
        addresses = tuple(await (resolver or _default_resolver)(host))
    except Exception:
        # socket.gaierror is an OSError; UnicodeError covers names the
        # IDNA codec rejects (e.g. an over-long label). Fail closed.
        return "dns_resolution_failed", None
    if not addresses:
        return "dns_resolution_failed", None
    for address in addresses:
        if _address_blocked(address):
            return "private_address_blocked", None

    return None, addresses[0]


async def check_outbound_url(url: str) -> str | None:
    """Return a block reason for an agent-supplied outbound URL, or None."""
    reason, _ = await pin_outbound_url(url)
    return reason
