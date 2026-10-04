"""Three identifiers that are not the same thing.

Every attempt an agent makes carries three ids that are routinely conflated:

``request_id``
    Names one wire request. A retry is a new request and gets a new one.

``idempotency_key``
    What the caller presents to the gateway as its replay key. A well-built
    agent derives it from the business operation, so a retry after a restart
    presents the same key. A common failure mode derives it per attempt or per
    agent instance, so a restarted agent presents a *new* key for the *same*
    business action.

``business_operation_id``
    The intended business action ("refund payment 789 for $50"). It survives
    restarts, replans, and new keys, because it is a property of the world the
    agent is trying to change, not of the attempt.

The harness keeps all three explicit so a scenario can ask the precise
question: when the key changes but the business operation does not, does
anything in the system notice?
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from enum import Enum


class KeyPolicy(str, Enum):
    """How an agent derives the idempotency key it sends."""

    #: Key derived from the business operation. A restarted agent that
    #: re-derives the key presents the same one.
    BUSINESS = "business"
    #: Key minted per attempt (or per agent process). A restart mints a new
    #: key for the same business intent.
    ATTEMPT = "attempt"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class OperationIdentity:
    """The identity triple of one attempt at one business operation."""

    business_operation_id: str
    idempotency_key: str
    request_id: str
    key_policy: KeyPolicy = KeyPolicy.BUSINESS
    #: Which agent incarnation issued this attempt. Bumps on restart.
    agent_generation: int = 1

    @classmethod
    def first_attempt(
        cls,
        business_operation_id: str,
        *,
        key_policy: KeyPolicy = KeyPolicy.BUSINESS,
    ) -> OperationIdentity:
        if key_policy is KeyPolicy.BUSINESS:
            key = f"idem:{business_operation_id}"
        else:
            key = _new_id("idem")
        return cls(
            business_operation_id=business_operation_id,
            idempotency_key=key,
            request_id=_new_id("req"),
            key_policy=key_policy,
        )

    def retry(self) -> OperationIdentity:
        """The same agent retries: new request, same key, same business op."""
        return replace(self, request_id=_new_id("req"))

    def after_restart(self) -> OperationIdentity:
        """A fresh agent replans the same business operation.

        Under :attr:`KeyPolicy.BUSINESS` the re-derived key is identical.
        Under :attr:`KeyPolicy.ATTEMPT` the new agent has no memory of the
        old key and mints a new one -- the case Test 6 exists to measure.
        """
        if self.key_policy is KeyPolicy.BUSINESS:
            key = self.idempotency_key
        else:
            key = _new_id("idem")
        return replace(
            self,
            idempotency_key=key,
            request_id=_new_id("req"),
            agent_generation=self.agent_generation + 1,
        )

    def as_dict(self) -> dict[str, str | int]:
        return {
            "business_operation_id": self.business_operation_id,
            "idempotency_key": self.idempotency_key,
            "request_id": self.request_id,
            "key_policy": self.key_policy.value,
            "agent_generation": self.agent_generation,
        }


def new_business_operation_id(payment_id: str) -> str:
    """Business operation ids are stable per intended action, not per attempt."""
    return f"refund:{payment_id}"


__all__ = ["KeyPolicy", "OperationIdentity", "new_business_operation_id"]
