"""A refund agent with nothing between it and the money.

This is the *starting point* of the clean-room exercise, not a target to
admire. It is deliberately short enough that nobody has to wonder what it
does: it takes a refund intent, POSTs it straight at the refund tool with a
long-lived bearer token, and — because a real agent runtime retries — calls
the same endpoint again whenever an attempt does not come back.

Everything the gateway exists to provide is missing here, and the absence is
load-bearing for the exercise:

* **No authority.** The agent holds the tool's own credential. Nothing scopes
  what it may refund, to whom, or how much in total.
* **No replay key.** ``POST /refunds`` is sent with no idempotency key, so the
  tool cannot tell a retry from a second refund.
* **No receipt.** When someone asks six weeks later whether this refund was
  authorized, the only evidence is an application log line the agent wrote
  about itself.
* **No outcome vocabulary.** :meth:`UnprotectedRefundAgent.issue_refund_with_retries`
  treats "the response did not arrive" as "it did not happen". That is the
  bug the lab measures: a lost response after a committed execution turns one
  refund into two.

The integration exercise is to put the gateway in front of this, without
editing the gateway and without turning any of its checks off. The judge
(:mod:`failure_lab.integration_check.judge`) then decides mechanically
whether the result survives the failure modes, from the independent effect
ledger rather than from anything the integration says about itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

#: The tool's REST surface. There is no gateway in this picture.
DEFAULT_TOOL_PATH = "/refunds"


@dataclass(frozen=True)
class RefundIntent:
    """One refund the business wants to exist in the world.

    ``operation_id`` is the property of the *world* the agent is trying to
    change, so it survives retries, replans and restarts. Every other id in
    the system (wire request ids, replay keys, attempt ids) is a property of
    an attempt and does not.

    ``currency`` is ``None`` when the refund settles in the wallet's own
    settlement currency. Naming a currency is a privileged choice: an
    authority that scopes this agent may forbid the field outright, in which
    case sending it is an operation the agent is not authorized to perform.
    """

    operation_id: str
    customer_id: str
    payment_id: str
    amount_minor_units: int
    currency: str | None = None

    def as_tool_arguments(self) -> dict[str, Any]:
        """The exact argument object ``refund.create`` accepts.

        The tool parses strictly: ``amount`` is an ``int`` of minor units and
        never the string ``"5000"``, and unknown fields are rejected rather
        than ignored.
        """
        arguments: dict[str, Any] = {
            "operation_id": self.operation_id,
            "customer_id": self.customer_id,
            "payment_id": self.payment_id,
            "amount": self.amount_minor_units,
        }
        if self.currency is not None:
            arguments["currency"] = self.currency
        return arguments


class UnprotectedRefundAgent:
    """Calls the refund tool directly, holding the tool's own credential."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        bearer_token: str,
        *,
        tool_path: str = DEFAULT_TOOL_PATH,
    ) -> None:
        self._client = client
        self._bearer_token = bearer_token
        self._tool_path = tool_path

    async def issue_refund(self, intent: RefundIntent) -> dict[str, Any]:
        """Issue one refund. No replay key, no permit, no receipt."""
        response = await self._client.post(
            self._tool_path,
            json=intent.as_tool_arguments(),
            headers={"Authorization": f"Bearer {self._bearer_token}"},
        )
        response.raise_for_status()
        return dict(response.json())

    async def issue_refund_with_retries(
        self, intent: RefundIntent, *, attempts: int = 3
    ) -> dict[str, Any]:
        """Retry until something answers.

        This is the failure mode, stated plainly: a timeout means the answer
        is missing, not that the refund is missing. Retrying here refunds the
        customer once per lost response.
        """
        last_error: Exception | None = None
        for _ in range(attempts):
            try:
                return await self.issue_refund(intent)
            except (
                httpx.TimeoutException,
                httpx.TransportError,
                httpx.HTTPStatusError,
            ) as exc:
                last_error = exc
        raise RuntimeError(
            f"refund {intent.operation_id} never came back"
        ) from last_error


__all__ = ["DEFAULT_TOOL_PATH", "RefundIntent", "UnprotectedRefundAgent"]
