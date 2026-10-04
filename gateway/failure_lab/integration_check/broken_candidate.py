"""An integration that looks right and is not, so the judge is provably a judge.

A check that has only ever passed things proves nothing about the things it
passed. This candidate exists to be rejected.

It inherits every correct decision from
:class:`~failure_lab.integration_check.reference_candidate.ReferenceIntegration`
and changes exactly one line: how the replay key is derived. The reference
derives it from the **business operation**, so every incarnation of the agent
re-derives the same key and a retry is recognisably a retry. This one mints a
fresh key per attempt.

That is not a strawman. It is the single most common way a competent team gets
this wrong, because a per-attempt key is the obvious reading of "send a unique
idempotency key with each request", and because it works perfectly until the
first lost response. The gateway's guarantee is scoped to the key; two keys are
two operations by design. So the retry is admitted as new work, dispatched, and
the customer is refunded twice — from the gateway's point of view, entirely
correctly.

Keeping the break to one line is the point. Everything else about this
integration is the reference implementation, so when the judge rejects it, the
rejection is attributable to the key derivation and to nothing else.

The judge should fail ``retry_after_lost_response``. What it does with
``restart_recovery`` is worth reading rather than predicting: a fresh key after
a crash may or may not produce a second effect depending on where the crash
landed, and the judge reports what the effect ledger saw.
"""

from __future__ import annotations

import uuid

from failure_lab.integration_check.reference_candidate import ReferenceIntegration
from failure_lab.integration_check.sample_app import RefundIntent

#: What this candidate would say about the documentation if asked. It is
#: self-reported and the judge labels it as such, so it earns nothing.
DOCUMENTATION_GAPS = (
    "Nothing published states that the idempotency key must be a function of "
    "the business operation rather than of the attempt. This integration read "
    "'unique key per request' the obvious way and is wrong because of it.",
)


class BrokenIntegration(ReferenceIntegration):
    """The reference, with one load-bearing decision inverted."""

    def __init__(self) -> None:
        super().__init__()
        self._gaps = list(DOCUMENTATION_GAPS)

    def replay_key(self, intent: RefundIntent) -> str:
        """A fresh key per attempt: the mistake this file exists to embody.

        ``intent`` is ignored on purpose. Two attempts at the same refund
        therefore present two keys, the gateway sees two operations, and the
        second one is dispatched — which is the gateway behaving as documented
        and the integration being wrong.
        """
        del intent
        return f"integration-check:attempt:{uuid.uuid4().hex}"


CANDIDATE = BrokenIntegration
