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
is a pre-flight guard, not a substitute for network-level egress policy,
and it intentionally does not try to defeat DNS rebinding.

Setting ``ALLOW_PRIVATE_NETWORK_TARGETS=true`` (local development against
mock servers) skips only the private-address checks; non-http(s) schemes
such as ``file://`` and ``javascript:`` are never allowed.

Non-canonical IPv4 literals (decimal ``2130706433``, hex ``0x7f000001``,
octal ``0177.0.0.1``, short form ``127.1``) are decoded locally. Browsers
treat those as addresses and do not ask DNS, so a resolver that answers
unknown names with a public address must not be allowed to approve them.
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
# Schemes a browser uses inside a page without opening a socket.
_PAGE_LOCAL_SCHEMES = frozenset({"about", "data", "blob"})


def _ipv4_number(part: str) -> int | None:
    """Parse one WHATWG IPv4 number, or None when the part is not one."""
    if part == "":
        return None
    if len(part) >= 2 and part[:2] == "0x":
        digits = part[2:]
        alphabet = "0123456789abcdef"
        base = 16
    elif len(part) >= 2 and part[0] == "0":
        digits = part[1:]
        alphabet = "01234567"
        base = 8
    else:
        digits = part
        alphabet = "0123456789"
        base = 10
    if digits == "":
        return 0
    if any(character not in alphabet for character in digits):
        return None
    return int(digits, base)


def _parse_legacy_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """Decode a browser IPv4 literal that ``ipaddress`` would reject.

    Returns None for ordinary hostnames so those still go through DNS.
    """
    parts = host.split(".")
    if not parts or len(parts) > 4:
        return None
    numbers: list[int] = []
    for part in parts:
        number = _ipv4_number(part)
        if number is None:
            return None
        numbers.append(number)
    if any(number > 255 for number in numbers[:-1]):
        return None
    if numbers[-1] >= 256 ** (5 - len(numbers)):
        return None
    ipv4 = numbers[-1]
    for index, number in enumerate(numbers[:-1]):
        ipv4 += number * (256 ** (3 - index))
    return ipaddress.IPv4Address(ipv4)


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


async def check_outbound_url(url: str) -> str | None:
    """Return a block reason for an agent-supplied outbound URL, or None."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "unparseable_url"

    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        return "scheme_not_allowed"

    host = parts.hostname
    if not host:
        return "missing_host"

    if get_settings().ALLOW_PRIVATE_NETWORK_TARGETS:
        return None

    host = host.strip("[]").rstrip(".").lower()
    if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_SUFFIXES):
        return "private_host_blocked"

    if _address_blocked(host):
        return "private_address_blocked"

    try:
        ipaddress.ip_address(host)
    except ValueError:
        # Browsers accept decimal, hex, octal, and short IPv4 forms that
        # ipaddress rejects, and they connect to the decoded address without
        # DNS. Do the same decode here so a public DNS answer cannot approve
        # a loopback or metadata target.
        literal = _parse_legacy_ipv4(host)
        if literal is not None:
            if _address_blocked(str(literal)):
                return "private_address_blocked"
            return None
        # A hostname, not a literal address: resolve it and require every
        # resolved address to be globally routable.
        try:
            infos = await _resolve_host(host)
        except (OSError, UnicodeError):
            # socket.gaierror is an OSError; UnicodeError covers names the
            # IDNA codec rejects (e.g. an over-long label). Fail closed.
            return "dns_resolution_failed"
        for info in infos:
            if _address_blocked(str(info[4][0])):
                return "private_address_blocked"

    return None


async def browser_request_block_reason(url: str) -> str | None:
    """Block reason for one browser request, including a redirect hop.

    about, data, and blob URLs stay inside the page and do not open a
    socket. Every other URL uses ``check_outbound_url``, so a public page
    that redirects to a metadata address is refused on that hop.
    """
    try:
        scheme = urlsplit(url).scheme.lower()
    except ValueError:
        return "unparseable_url"
    if scheme in _PAGE_LOCAL_SCHEMES:
        return None
    return await check_outbound_url(url)
