"""Outbound-URL guard for agent-driven navigation and proxied requests.

Agent-supplied URLs reach two genuinely dangerous sinks: the AWI Playwright
bridge (a real headless browser that will happily open ``file://`` and
intranet URLs) and the behavioral sandbox's HTTP proxy mode (a raw
``httpx`` request). Both run inside our infrastructure, so an unchecked
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
import re
import socket
from collections.abc import Awaitable, Callable, Sequence
from urllib.parse import urlsplit

from app.core.config import get_settings

_ALLOWED_SCHEMES = {"http", "https"}
_BLOCKED_HOSTNAMES = {"localhost", "metadata.google.internal"}
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal")
# libc and browsers parse these as IPv4. ipaddress rejects them, so a guard
# that only calls ip_address() then DNS will follow a public DNS answer
# while the client dials the embedded private address (2130706433 is
# 127.0.0.1, 0177.0.0.1 is octal loopback, 127.1 is shorthand loopback).
_LEGACY_IPV4 = re.compile(
    r"^(?:0[xX][0-9a-fA-F]+|[0-9]+)(?:\.(?:0[xX][0-9a-fA-F]+|[0-9]+)){0,3}$"
)
# Deprecated IPv4-compatible form (::/96) and the well-known NAT64 prefix.
# DNS64 publishes public IPv4 destinations inside 64:ff9b::/96, so the whole
# prefix must not be refused: only an embedded address that is itself blocked.
_IPV4_COMPATIBLE = ipaddress.IPv6Network("::/96")
_NAT64_WELL_KNOWN = ipaddress.IPv6Network("64:ff9b::/96")
# RFC 2765 SIIT form. Not IPv4-mapped (::ffff:0:0/96). is_global stays true
# on Python 3.11 through 3.14, including when the low 32 bits are metadata.
_IPV4_TRANSLATED = ipaddress.IPv6Network("::ffff:0:0:0/96")


def _embedded_ipv4_literal(
    ip: ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | None:
    """Return the IPv4 a stack may dial for a compatible, translated, or NAT64 literal.

    IPv4-mapped addresses (``::ffff:0:0/96``) are omitted. ``is_global``
    already follows the mapped IPv4 for those. IPv4-translated addresses
    (``::ffff:0:0:0/96``) are not mapped, and ``is_global`` is true for them.
    """
    if ip.ipv4_mapped is not None:
        return None
    if ip in _IPV4_COMPATIBLE or ip in _NAT64_WELL_KNOWN or ip in _IPV4_TRANSLATED:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def address_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True when this address must not be fetched or dialed.

    ``is_global`` misses multicast (it can be global), deprecated site-local
    ``fec0::/10``, and IPv6 literals whose embedded IPv4 is private or
    link-local. ``::169.254.169.254``, ``::ffff:0:169.254.169.254``, and
    ``64:ff9b::10.0.0.1`` are ``is_global`` and still name metadata or
    RFC1918. A public embedding such as ``64:ff9b::8.8.8.8`` stays allowed
    so DNS64 can publish an IPv4-only host.
    """
    if ip.is_multicast or bool(getattr(ip, "is_site_local", False)) or not ip.is_global:
        return True
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = _embedded_ipv4_literal(ip)
        if embedded is not None and address_is_blocked(embedded):
            return True
    return False


def is_ambiguous_ipv4_literal(host: str) -> bool:
    """True for a numeric host that is not a canonical IP address.

    Callers use this after ``ip_address`` rejects ``host``. Canonical
    addresses such as ``8.8.8.8`` parse and return False.
    """
    if _LEGACY_IPV4.fullmatch(host) is None:
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return True
    return False


def _address_blocked(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return address_is_blocked(ip)


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
        # Reject before DNS. A public answer must not hide an octal, hex,
        # decimal, or shortened IPv4 literal the client will dial directly.
        if is_ambiguous_ipv4_literal(host):
            return "private_address_blocked", None
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
