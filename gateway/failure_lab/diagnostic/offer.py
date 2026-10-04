"""The commercial offer, defined once, with no price invented for it.

The page that renders this module is allowed to show it only after a run has
produced a meaningful result, and only when the result points that way. That
rule lives in the page; what lives here is the content, in one place, so an
offer cannot drift between the result page and anywhere else that shows it.

Two things are deliberate and both are refusals:

**There is no price.** :data:`OFFER_PRICE` is ``None`` and renders as "not yet
published". A number invented to make a page feel complete is a number somebody
will quote back later, and the honest state of this product is that the price is
not set. A deployment that configures one gets one; nothing here fabricates one.

**"Start deployment" does not claim to deploy anything.** Two of the six items
need a human to look at a real downstream before anything is governed, and they
say so on the item, next to the item, rather than in a footnote under the
button. A surface that implies one click reaches production is lying about the
one thing a buyer of a *trust* plane is buying.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Set to a string to publish a price. Left unset on purpose -- see the module
#: docstring. Anything rendering an offer must go through :data:`OFFER`.
OFFER_PRICE: str | None = None

#: What the page prints where a price would go.
PRICE_NOT_PUBLISHED = "not yet published"


@dataclass(frozen=True)
class OfferItem:
    """One thing the offer includes, and whether a human has to size it."""

    title: str
    detail: str
    #: True when this item cannot be turned on from a web page because it
    #: depends on the shape of a downstream nobody here has seen.
    manual_review: bool = False

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class Offer:
    headline: str
    summary: str
    items: tuple[OfferItem, ...]
    action_label: str
    action_href: str
    price: str
    price_note: str
    manual_review_note: str

    @property
    def needs_manual_review(self) -> bool:
        return any(item.manual_review for item in self.items)

    def as_dict(self) -> dict[str, Any]:
        return {
            "headline": self.headline,
            "summary": self.summary,
            "items": [item.as_dict() for item in self.items],
            "action_label": self.action_label,
            "action_href": self.action_href,
            "price": self.price,
            "price_note": self.price_note,
            "manual_review_note": self.manual_review_note,
            "needs_manual_review": self.needs_manual_review,
        }


OFFER = Offer(
    headline="Govern one tool, end to end",
    summary=(
        "One tool call of yours, behind the same gateway this check just "
        "measured, with the same instruments pointed at it."
    ),
    items=(
        OfferItem(
            "One protected tool",
            "A single downstream operation -- the one that costs money when it "
            "happens twice -- registered as a governed upstream tool.",
            manual_review=True,
        ),
        OfferItem(
            "Scoped authorization",
            "A permit that names the tool, the ceiling and the fields the agent "
            "may not set. Calls outside the permit are refused before dispatch, "
            "not reconciled afterwards.",
        ),
        OfferItem(
            "Retry and dispatch protection",
            "One durable dispatch claim per operation id. A retry after a lost "
            "response resolves against the claim instead of executing again.",
        ),
        OfferItem(
            "Retained evidence",
            "Every authorization, dispatch, debit and outcome kept for the "
            "retention window you configure, exportable as the same bundle this "
            "check produced.",
        ),
        OfferItem(
            "Receipt verification",
            "Signed receipts and a published key document, verifiable by a third "
            "party with no access to the gateway. A verified signature "
            "establishes what the gateway recorded and does not establish that "
            "the downstream action occurred; both are reported separately.",
        ),
        OfferItem(
            "Deployment support",
            "A worked integration against your real downstream, including the "
            "failure runs above repeated against it before anything is switched "
            "on.",
            manual_review=True,
        ),
    ),
    action_label="Start deployment",
    action_href="/diagnostic/deployment",
    price=OFFER_PRICE or PRICE_NOT_PUBLISHED,
    price_note=(
        "No price is shown because none is configured on this instance. This "
        "page will not invent one."
    ),
    manual_review_note=(
        "Two of these need a technical review of your downstream before they "
        "can be turned on, and are marked. Nothing on this page deploys "
        "anything to production by itself."
    ),
)

__all__ = ["OFFER", "OFFER_PRICE", "PRICE_NOT_PUBLISHED", "Offer", "OfferItem"]
