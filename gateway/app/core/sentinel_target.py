"""Fail-closed origin policy for the optional Sentinel integration.

Sentinel requests carry approval details and, outside the read-only health
probe, a bearer credential. Every caller must therefore agree on one strict
origin shape before constructing an HTTP client.

This guard rejects dangerous literal and reserved destinations. It does not
replace network-level egress controls and cannot by itself prevent a public
DNS name from later rebinding to a private address.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from urllib.parse import urlsplit

_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_LEGACY_IPV4 = re.compile(
    r"^(?:0[xX][0-9a-fA-F]+|[0-9]+)(?:\.(?:0[xX][0-9a-fA-F]+|[0-9]+)){0,3}$"
)
_BLOCKED_HOSTNAMES = frozenset({"localhost.localdomain", "metadata.google.internal"})
_BLOCKED_SUFFIXES = (".internal", ".local")


class SentinelTargetError(ValueError):
    """The configured Sentinel origin is malformed or unsafe."""


def normalize_sentinel_origin(value: str, *, allow_loopback: bool = False) -> str:
    """Return a normalized root origin or raise ``SentinelTargetError``.

    Remote targets require HTTPS. Loopback HTTP/HTTPS is available only when
    the caller explicitly opts in for local development or tests. URL
    credentials, paths, queries, fragments, ambiguous numeric hosts, and
    non-global IP literals are always rejected.
    """
    if (
        not value
        or value != value.strip()
        or any(
            character.isspace() or unicodedata.category(character).startswith("C")
            for character in value
        )
    ):
        raise SentinelTargetError(
            "SENTINEL_API_URL must be non-empty without whitespace or control characters"
        )

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise SentinelTargetError("SENTINEL_API_URL is malformed") from exc

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise SentinelTargetError("SENTINEL_API_URL must use http or https")
    if not parsed.netloc or parsed.hostname is None:
        raise SentinelTargetError("SENTINEL_API_URL must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise SentinelTargetError(
            "SENTINEL_API_URL must not contain embedded credentials"
        )
    if parsed.netloc.endswith(":") or port == 0:
        raise SentinelTargetError("SENTINEL_API_URL contains an invalid port")
    if "?" in value or "#" in value or parsed.path not in {"", "/"}:
        raise SentinelTargetError(
            "SENTINEL_API_URL must be a root origin without path, query, or fragment"
        )

    hostname = parsed.hostname
    if hostname.endswith("..") or "%" in hostname:
        raise SentinelTargetError("SENTINEL_API_URL contains an invalid hostname")
    host = hostname[:-1] if hostname.endswith(".") else hostname
    host = host.lower()

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # ``urlsplit`` accepts bracketed IPvFuture syntax and returns the
        # bracket-free value as ``hostname``. It is not a DNS hostname.
        if parsed.netloc.startswith("["):
            raise SentinelTargetError(
                "SENTINEL_API_URL contains an unsupported address literal"
            )
        if any(ord(character) > 127 for character in host):
            raise SentinelTargetError(
                "SENTINEL_API_URL hostnames must use explicit ASCII/punycode form"
            )
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise SentinelTargetError(
                "SENTINEL_API_URL contains an invalid hostname"
            ) from exc

        # libc and HTTP stacks may interpret forms such as 2130706433,
        # 0177.0.0.1, or 0x7f000001 as IPv4 even though ``ipaddress`` does not.
        # Reject the ambiguity instead of validating one authority and
        # connecting to another.
        if _LEGACY_IPV4.fullmatch(host):
            raise SentinelTargetError(
                "SENTINEL_API_URL must use canonical IP-address notation"
            )
        labels = host.split(".")
        if len(host) > 253 or any(not _DNS_LABEL.fullmatch(label) for label in labels):
            raise SentinelTargetError("SENTINEL_API_URL contains an invalid hostname")

        is_loopback = host == "localhost" or host.endswith(".localhost")
        if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_SUFFIXES):
            raise SentinelTargetError(
                "SENTINEL_API_URL must not target reserved internal infrastructure"
            )
        if "." not in host and not is_loopback:
            raise SentinelTargetError(
                "SENTINEL_API_URL must not target a bare internal hostname"
            )
        if is_loopback and not allow_loopback:
            raise SentinelTargetError(
                "SENTINEL_API_URL loopback targets are local/test only"
            )
    else:
        ipv4_mapped = getattr(address, "ipv4_mapped", None)
        is_loopback = address.is_loopback or bool(
            ipv4_mapped is not None and ipv4_mapped.is_loopback
        )
        if is_loopback:
            if not allow_loopback:
                raise SentinelTargetError(
                    "SENTINEL_API_URL loopback targets are local/test only"
                )
        elif ipv4_mapped is not None:
            raise SentinelTargetError(
                "SENTINEL_API_URL must use canonical IPv4-address notation"
            )
        elif (
            address.is_multicast
            or address.is_reserved
            or address.is_private
            or address.is_link_local
            or address.is_unspecified
            or getattr(address, "is_site_local", False)
            or not address.is_global
        ):
            raise SentinelTargetError(
                "SENTINEL_API_URL must not target private, link-local, metadata, or reserved addresses"
            )
        host = str(address)

    if scheme == "http" and not is_loopback:
        raise SentinelTargetError("remote SENTINEL_API_URL targets require HTTPS")

    authority = f"[{host}]" if ":" in host else host
    default_port = 443 if scheme == "https" else 80
    if port is not None and port != default_port:
        authority = f"{authority}:{port}"
    return f"{scheme}://{authority}"


def sentinel_health_url(value: str, *, allow_loopback: bool = False) -> str:
    """Return the read-only health endpoint for a validated Sentinel origin."""
    return f"{normalize_sentinel_origin(value, allow_loopback=allow_loopback)}/health"


def sentinel_api_key_is_valid(value: str) -> bool:
    """Return whether a key is present without unsafe whitespace/control bytes."""
    return (
        bool(value)
        and value.isascii()
        and value == value.strip()
        and not any(
            character.isspace() or unicodedata.category(character).startswith("C")
            for character in value
        )
    )
