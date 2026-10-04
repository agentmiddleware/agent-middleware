"""Fail-closed target resolution for scripts that mutate a live deployment."""

from __future__ import annotations

import ipaddress
import os
import re
import unicodedata
from collections.abc import Mapping
from urllib.parse import urlsplit

API_URL_ENV_VAR = "AGENT_MIDDLEWARE_API_URL"
CANONICAL_PRODUCTION_ORIGIN = "https://api.thisisatest.tech"

_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class LiveTargetError(ValueError):
    """Raised when a live-script target is missing or unsafe."""


def _normalize_hostname(hostname: str) -> tuple[str, bool]:
    if hostname.endswith(".."):
        raise LiveTargetError("API URL contains an invalid hostname")
    host = hostname[:-1] if hostname.endswith(".") else hostname
    host = host.lower()
    if not host:
        raise LiveTargetError("API URL must include a hostname")

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise LiveTargetError("API URL contains an invalid hostname") from exc

        labels = host.split(".")
        if len(host) > 253 or any(not _DNS_LABEL.fullmatch(label) for label in labels):
            raise LiveTargetError("API URL contains an invalid hostname")
        return host, host == "localhost"

    ipv4_mapped = getattr(address, "ipv4_mapped", None)
    is_loopback = address.is_loopback or bool(
        ipv4_mapped is not None and ipv4_mapped.is_loopback
    )
    return str(address), is_loopback


def resolve_live_target(
    api_url: str | None,
    *,
    confirm_production: bool,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Select, validate, and normalize a live API origin.

    An explicitly supplied ``api_url`` wins over ``AGENT_MIDDLEWARE_API_URL``.
    The result contains only a scheme, normalized host, and optional safe port.
    """

    source = os.environ if environ is None else environ
    selected = api_url if api_url is not None else source.get(API_URL_ENV_VAR)
    if selected is None or selected == "":
        raise LiveTargetError(
            "pass --api-url or set AGENT_MIDDLEWARE_API_URL explicitly"
        )
    value = selected
    if any(
        character.isspace() or unicodedata.category(character).startswith("C")
        for character in value
    ):
        raise LiveTargetError(
            "API URL must not contain whitespace or control characters"
        )

    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise LiveTargetError("API URL is malformed") from exc

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise LiveTargetError("API URL scheme must be http or https")
    if not parsed.netloc or parsed.hostname is None:
        raise LiveTargetError("API URL must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise LiveTargetError("API URL must not contain embedded credentials")
    if parsed.netloc.endswith(":"):
        raise LiveTargetError("API URL contains an invalid port")
    if "?" in value:
        raise LiveTargetError("API URL must not contain a query string")
    if "#" in value:
        raise LiveTargetError("API URL must not contain a fragment")
    if parsed.path not in {"", "/"}:
        raise LiveTargetError("API URL path must be empty or /")

    try:
        port = parsed.port
    except ValueError as exc:
        raise LiveTargetError("API URL contains an invalid port") from exc
    if port is not None and port == 0:
        raise LiveTargetError("API URL contains an invalid port")

    host, is_loopback = _normalize_hostname(parsed.hostname)
    if scheme == "http" and not is_loopback:
        raise LiveTargetError("remote API URLs require HTTPS; HTTP is loopback-only")

    if ":" in host:
        authority = f"[{host}]"
    else:
        authority = host
    if port is not None and not (scheme == "https" and port == 443):
        authority = f"{authority}:{port}"

    origin = f"{scheme}://{authority}"
    if origin == CANONICAL_PRODUCTION_ORIGIN and not confirm_production:
        raise LiveTargetError(
            "the canonical production target requires --confirm-production"
        )
    return origin
