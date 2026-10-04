"""What a registered tool costs, in one place.

The governed invoke path and the quote endpoint must agree on the price of a
tool to the credit, or a "locked" quote would lock a number the charge never
uses. Both call through here.

Two numbers are in play and they are not the same thing:

- **credits** — what the caller is told and what the permit budget is measured
  in (``credits_per_unit`` on the registration, falling back to the category's
  default price).
- **units** — what ``AgentMoney.charge`` takes, which multiplies back up by the
  category's default price. ``charge_units_for`` is the conversion, and it is
  what lets a quote pin the charged credits even if the tool's registered
  price has moved since the quote was issued.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from app.core.credits import credit_amount_fits_storage
from app.schemas.billing import ServiceCategory


# Credits per unit for each service. Credits convert to fiat at
# settings.EXCHANGE_RATE (1000 credits ≈ $1 USD by default).
DEFAULT_PRICING: dict[ServiceCategory, tuple[str, Decimal, str]] = {
    ServiceCategory.IOT_BRIDGE: (
        "request",
        Decimal("2.0"),
        "Per IoT message bridged",
    ),
    ServiceCategory.TELEMETRY_PM: (
        "event",
        Decimal("1.0"),
        "Per telemetry event ingested",
    ),
    ServiceCategory.MEDIA_ENGINE: (
        "frame",
        Decimal("0.5"),
        "Per video frame processed",
    ),
    ServiceCategory.AGENT_COMMS: (
        "message",
        Decimal("1.5"),
        "Per agent message routed",
    ),
    ServiceCategory.CONTENT_FACTORY: (
        "piece",
        Decimal("50.0"),
        "Per content piece generated",
    ),
    ServiceCategory.RED_TEAM: (
        "scan",
        Decimal("100.0"),
        "Per security scan executed",
    ),
    ServiceCategory.ORACLE: (
        "crawl",
        Decimal("25.0"),
        "Per API crawled and indexed",
    ),
    ServiceCategory.PLATFORM_FEE: (
        "request",
        Decimal("0.1"),
        "Base platform fee per API call",
    ),
    ServiceCategory.SWARM_DELEGATION: (
        "child",
        Decimal("5.0"),
        "Per child wallet spawned",
    ),
    ServiceCategory.PROTOCOL_GEN: (
        "generation",
        Decimal("200.0"),
        "Per llm.txt + OpenAPI spec generated",
    ),
    ServiceCategory.SANDBOX: (
        "session",
        Decimal("150.0"),
        "Per sandbox environment session",
    ),
    ServiceCategory.RTAAS: (
        "scan",
        Decimal("100.0"),
        "Per external Red Team scan",
    ),
}


# Categories whose only services are the frozen proof-surface routers in
# app/main.py. DEFAULT_PRICING stays authoritative server-side for all of them
# (it is the fallback price and the unit divisor in charge_units_for), but a
# deployment that does not mount those routers should not advertise them as
# purchasable: docs/PROOF_SURFACES.md rule 3 says not to present AWI, media,
# IoT, or oracle as the product in OpenAPI. PLATFORM_FEE and SWARM_DELEGATION
# are wallet/platform-level and stay listed either way.
PROOF_SURFACE_CATEGORIES: frozenset[ServiceCategory] = frozenset(
    {
        ServiceCategory.IOT_BRIDGE,
        ServiceCategory.TELEMETRY_PM,
        ServiceCategory.MEDIA_ENGINE,
        ServiceCategory.AGENT_COMMS,
        ServiceCategory.CONTENT_FACTORY,
        ServiceCategory.RED_TEAM,
        ServiceCategory.ORACLE,
        ServiceCategory.PROTOCOL_GEN,
        ServiceCategory.SANDBOX,
        ServiceCategory.RTAAS,
    }
)


def tool_price(service: dict[str, Any], category: ServiceCategory) -> Decimal:
    """Current price in credits for one call of a registered tool.

    Raises ``ValueError("tool_price_invalid")`` unless the registered price is
    a finite, positive, losslessly storable number. Every caller reads the price before any
    policy, permit, quote or ledger step, so refusing here keeps a bad
    registration out of all of them: a negative price would otherwise be
    reserved against the permit as a *negative* amount (growing its budget),
    and an infinite one signed into a quote or receipt.
    """
    default_price = DEFAULT_PRICING[category][1]
    exact_price = service.get("credits_per_unit_exact")
    raw_price = (
        exact_price
        if exact_price is not None
        else service.get("credits_per_unit", default_price)
    )
    try:
        price = Decimal(str(raw_price))
    except InvalidOperation:
        raise ValueError("tool_price_invalid") from None
    if not credit_amount_fits_storage(price) or price == 0:
        raise ValueError("tool_price_invalid")
    return price


def charge_units_for(credits: Decimal, category: ServiceCategory) -> Decimal:
    """Convert a credit amount into the units ``AgentMoney.charge`` expects."""
    default_price = DEFAULT_PRICING[category][1]
    return credits / default_price
