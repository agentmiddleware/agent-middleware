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
public hostnames pin ``_resolve_host`` instead of relying on live DNS. This
is a pre-flight guard, not a substitute for network-level egress policy.

DNS rebinding (a name resolving to a public address here and to an
intranet address when the fetcher resolves it) defeats any pre-flight
check on its own. ``resolve_outbound_url`` therefore also returns the
validated addresses, and every fetching caller must connect to one of
those addresses instead of resolving the hostname again, mirroring the
upstream MCP pinned transport. Callers that cannot pin their connection
(a headless browser with its own resolver) must re-check the final URL
after navigation and fail closed when it is blocked.

Setting ``ALLOW_PRIVATE_NETWORK_TARGETS=true`` (local development against
mock servers) skips only the private-address checks; non-http(s) schemes
such as ``file://`` and ``javascript:`` are never allowed.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
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


async def resolve_outbound_url(url: str) -> tuple[str | None, list[str]]:
    """Check an agent-supplied outbound URL and return validated addresses.

    Returns a ``(block_reason, addresses)`` pair. ``block_reason`` follows
    the ``check_outbound_url`` contract (``None`` when the URL is safe to
    fetch). When the URL is allowed, ``addresses`` holds the validated
    destination addresses the fetcher must connect to instead of resolving
    the hostname again; connecting anywhere else re-opens DNS rebinding.
    ``addresses`` is empty when there is nothing to pin (the local-dev
    escape hatch, which skips resolution entirely).
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "unparseable_url", []

    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        return "scheme_not_allowed", []

    host = parts.hostname
    if not host:
        return "missing_host", []

    if get_settings().ALLOW_PRIVATE_NETWORK_TARGETS:
        return None, []

    host = host.strip("[]").rstrip(".").lower()
    if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_SUFFIXES):
        return "private_host_blocked", []

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _address_blocked(host):
            return "private_address_blocked", []
        # A literal address needs no resolution; pin it as written.
        return None, [str(literal)]

    # A hostname, not a literal address: resolve it and require every
    # resolved address to be globally routable.
    try:
        infos = await _resolve_host(host)
    except (OSError, UnicodeError):
        # socket.gaierror is an OSError; UnicodeError covers names the
        # IDNA codec rejects (e.g. an over-long label). Fail closed.
        return "dns_resolution_failed", []
    addresses = list(dict.fromkeys(str(info[4][0]) for info in infos))
    if not addresses:
        return "dns_resolution_failed", []
    for address in addresses:
        if _address_blocked(address):
            return "private_address_blocked", []
    return None, addresses


async def check_outbound_url(url: str) -> str | None:
    """Return a block reason for an agent-supplied outbound URL, or None."""
    block_reason, _ = await resolve_outbound_url(url)
    return block_reason
