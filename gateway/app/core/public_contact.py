"""Shared fail-closed validation for public operator contact metadata."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit


_CONTACT_PLACEHOLDER_MARKERS = (
    "placeholder",
    "yourdomain",
    "example.com",
    "support@agent-middleware.dev",
    "dev@agentnative.io",
)
_RESERVED_CONTACT_HOSTS = {"example", "invalid", "localhost", "test"}
_BLOCKED_BOOKING_HOSTS = {
    "api.thisisatest.tech",
    "thisisatest.tech",
    "www.thisisatest.tech",
}


def _reserved_contact_hostname(hostname: str) -> bool:
    normalized = hostname.casefold().rstrip(".")
    return normalized in _RESERVED_CONTACT_HOSTS or any(
        normalized.endswith(f".{suffix}") for suffix in _RESERVED_CONTACT_HOSTS
    )


def validated_public_contact(config: Any) -> dict[str, str] | None:
    """Return one complete public identity or fail on partial/provisional data."""

    values = {
        "name": config.PUBLIC_CONTACT_NAME.strip(),
        "email": config.PUBLIC_CONTACT_EMAIL.strip(),
        "url": config.PUBLIC_CONTACT_URL.strip(),
    }
    if not any(values.values()):
        return None
    if not all(values.values()):
        raise ValueError(
            "PUBLIC_CONTACT_NAME, PUBLIC_CONTACT_EMAIL, and PUBLIC_CONTACT_URL "
            "must be configured together"
        )
    if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", values["email"]) is None:
        raise ValueError("PUBLIC_CONTACT_EMAIL must be a valid monitored address")
    if _reserved_contact_hostname(values["email"].rsplit("@", 1)[1]):
        raise ValueError("PUBLIC_CONTACT_EMAIL must use a routable public domain")

    parsed_url = urlsplit(values["url"])
    if (
        parsed_url.scheme != "https"
        or not parsed_url.hostname
        or parsed_url.username
        or parsed_url.password
    ):
        raise ValueError("PUBLIC_CONTACT_URL must be an absolute HTTPS URL")
    if _reserved_contact_hostname(parsed_url.hostname):
        raise ValueError("PUBLIC_CONTACT_URL must use a routable public domain")
    if parsed_url.hostname.casefold() in _BLOCKED_BOOKING_HOSTS:
        raise ValueError("PUBLIC_CONTACT_URL must point to a booking service")

    normalized = " ".join(values.values()).casefold()
    if (
        any(marker in normalized for marker in _CONTACT_PLACEHOLDER_MARKERS)
        or len(values["name"]) < 3
        or values["name"].casefold() == "agent middleware api"
        or re.search(
            r"\b(?:example|fake|placeholder|provisional|test)\b",
            values["name"],
            flags=re.IGNORECASE,
        )
    ):
        raise ValueError("placeholder public contact metadata is forbidden")
    return values
