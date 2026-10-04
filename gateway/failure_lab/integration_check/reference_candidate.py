"""A correct integration, so the judge is provably not vacuous.

This file exists for one reason: an assertion suite nobody has ever passed is
indistinguishable from an assertion suite nobody *can* pass. Its sibling
``broken_candidate.py`` exists for the opposite reason. Together they bracket
the judge: one passes, one fails, and the difference between them is a single
design decision about where the replay key comes from.

The integration itself is small, and every non-obvious line is a consequence
of one rule:

    **The replay key is derived from the business operation, never from the
    attempt.**

A key minted per attempt makes a retry a second authorization. A key minted
per process makes a restart a second authorization. A key derived from
"refund payment 1041" is the same key in the original process, in the retry,
and in the process that comes back after the crash -- which is the only
reason the gateway can recognise the third call as the same intent as the
first.

Two things this file deliberately does not do:

* It does not verify the receipt with the vendor SDK or with the endpoint the
  issuer serves for that purpose. A verifier supplied by the party being
  audited establishes nothing about that party. It uses
  :mod:`failure_lab.verifier`, which imports neither the application nor its
  SDK and returns three separate claims.
* It does not route around the refusal in operation 3. The permit forbids
  naming the settlement currency; an integration that drops the field and
  tries again has defeated the control it was asked to respect.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from failure_lab.integration_check.judge import (
    CandidateIntegration,
    IntegrationContext,
    OperationReport,
    ReceiptVerificationReport,
    RecoveryStage,
    Understanding,
)
from failure_lab.integration_check.sample_app import RefundIntent
from failure_lab.verifier import KeySource, parse_key_document, verify

#: Gaps found while building this integration from the published material.
#: Self-reported, like every gap list: nobody but the integrator knows where
#: they had to guess.
DOCUMENTATION_GAPS = (
    "A governed denial arrives as HTTP 200 carrying a JSON-RPC error member. "
    "Nothing in the published material says so, and an integration that "
    "branches on the status code reads a refusal as a success.",
    "The discovery contract does not say that an explicitly uncertain outcome "
    "stays charged and is not refunded on replay. docs/failure-semantics.md "
    "does, but an integrator working from the wire alone has to guess, and "
    "the guess that a charge will be reversed is the dangerous one.",
    "The key document is served by the same origin that issues the receipts, "
    "and no out-of-band distribution channel is documented. There is "
    "therefore no published procedure by which issuer trust can be "
    "established at all -- which is worth stating plainly rather than "
    "leaving an integrator to discover.",
    "Nothing published says the idempotency key must be derivable after a "
    "restart. A key is described as required; that it must be a function of "
    "the business operation rather than of the attempt is the load-bearing "
    "part, and it is left implicit.",
    "There is no documented way for a restarted agent to ask 'what happened "
    "to the operation I was in the middle of'. Re-presenting the original key "
    "works, but that it is the intended recovery procedure is inferred, not "
    "stated.",
)

#: Filename of the intent journal. Written before an attempt, read by whoever
#: comes back afterwards.
JOURNAL_NAME = "pending-operations.json"


class ReferenceIntegration(CandidateIntegration):
    """The integration the judge passes."""

    def __init__(self) -> None:
        self._gaps: list[str] = list(DOCUMENTATION_GAPS)

    # -- the rule ---------------------------------------------------------

    def replay_key(self, intent: RefundIntent) -> str:
        """Derived from the business operation, so every incarnation agrees.

        Nothing about the attempt, the process, or the wall clock goes in
        here. That is the whole trick.
        """
        return f"integration-check:{intent.operation_id}"

    # -- transport --------------------------------------------------------

    async def _invoke(
        self, ctx: IntegrationContext, intent: RefundIntent
    ) -> dict[str, Any]:
        """One governed call. A retry re-enters here with the same key."""
        body = {
            "jsonrpc": "2.0",
            "id": f"req-{uuid.uuid4().hex[:12]}",
            "method": "tools/call",
            "params": {
                "name": ctx.tool_id,
                "arguments": intent.as_tool_arguments(),
                "mcpContext": {
                    "wallet_id": ctx.wallet_id,
                    "permit_id": ctx.permit_id,
                    "idempotency_key": self.replay_key(intent),
                },
            },
        }
        response = await ctx.gateway.post(
            ctx.invoke_path, json=body, headers=ctx.auth_headers()
        )
        try:
            payload = response.json()
        except ValueError:
            return {"error": {"code": None, "message": f"http {response.status_code}"}}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _read(payload: dict[str, Any]) -> tuple[Understanding, str | None, str]:
        """Turn one gateway answer into an understanding, a receipt and a reason.

        The status code is not consulted: a refusal and a success both arrive
        as HTTP 200, and the difference lives in the JSON-RPC envelope.
        """
        if "result" in payload:
            result = payload.get("result") or {}
            receipt = result.get("receipt") or {}
            structured = result.get("structuredContent") or {}
            replayed = bool(structured.get("replayed"))
            return (
                Understanding.CONFIRMED_REPLAY
                if replayed
                else Understanding.CONFIRMED_SUCCESS,
                receipt.get("receipt_id"),
                "the gateway returned a terminal success",
            )
        error = payload.get("error") or {}
        data = error.get("data") or {}
        receipt = data.get("receipt") if isinstance(data, dict) else None
        outcome = receipt.get("outcome") if isinstance(receipt, dict) else None
        receipt_id = receipt.get("receipt_id") if isinstance(receipt, dict) else None
        message = str(error.get("message") or "")
        if outcome == "delivery_uncertain" or message == "delivery_uncertain":
            return (
                Understanding.KNOWN_AMBIGUOUS,
                receipt_id,
                "the gateway recorded the outcome as uncertain: it will not say "
                "the refund happened and it will not say it did not",
            )
        if outcome == "denied" or message.startswith("permit_"):
            return (
                Understanding.CONFIRMED_REFUSED,
                receipt_id,
                f"refused by authority: {message}",
            )
        return (Understanding.UNKNOWN, receipt_id, message or "no interpretable answer")

    # -- journal ----------------------------------------------------------

    def _journal_path(self, ctx: IntegrationContext) -> Path:
        return ctx.work_dir / JOURNAL_NAME

    def _record_intent(self, ctx: IntegrationContext, intent: RefundIntent) -> None:
        """Write the intent down before acting on it.

        The key is recomputable from ``operation_id`` alone, so this journal
        is not strictly required for recovery. It is written anyway, because
        an operator arriving after the crash should be able to see what the
        agent was in the middle of without re-deriving anything.
        """
        path = self._journal_path(ctx)
        entries = self._read_journal(ctx)
        entries[intent.operation_id] = {
            "operation_id": intent.operation_id,
            "idempotency_key": self.replay_key(intent),
            "payment_id": intent.payment_id,
            "amount_minor_units": intent.amount_minor_units,
            "state": "attempted",
        }
        path.write_text(json.dumps(entries, indent=2, sort_keys=True), encoding="utf-8")

    def _read_journal(self, ctx: IntegrationContext) -> dict[str, Any]:
        path = self._journal_path(ctx)
        if not path.is_file():
            return {}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    # -- the five operations ----------------------------------------------

    async def execute_authorized_operation(
        self, ctx: IntegrationContext
    ) -> OperationReport:
        self._record_intent(ctx, ctx.intent)
        understanding, receipt_id, reason = self._read(
            await self._invoke(ctx, ctx.intent)
        )
        return OperationReport(
            understanding=understanding,
            receipt_id=receipt_id,
            idempotency_key=self.replay_key(ctx.intent),
            detail=reason,
        )

    async def retry_after_lost_response(
        self, ctx: IntegrationContext
    ) -> OperationReport:
        """Attempt, then retry under the *same* key.

        The temptation after a lost response is a fresh key, because a fresh
        key always gets an answer. It gets an answer by authorizing the refund
        a second time. The same key gets the gateway's record of the first
        attempt, which is the answer that is actually true.
        """
        self._record_intent(ctx, ctx.intent)
        first = self._read(await self._invoke(ctx, ctx.intent))
        if first[0] in (
            Understanding.CONFIRMED_SUCCESS,
            Understanding.CONFIRMED_REPLAY,
        ):
            return OperationReport(
                understanding=first[0],
                receipt_id=first[1],
                idempotency_key=self.replay_key(ctx.intent),
                detail=f"first attempt was already terminal: {first[2]}",
            )
        understanding, receipt_id, reason = self._read(
            await self._invoke(ctx, ctx.intent)
        )
        return OperationReport(
            understanding=understanding,
            receipt_id=receipt_id,
            idempotency_key=self.replay_key(ctx.intent),
            detail=(
                f"first attempt: {first[2]}; retried under the same replay key "
                f"and got: {reason}"
            ),
        )

    async def attempt_unauthorized_operation(
        self, ctx: IntegrationContext
    ) -> OperationReport:
        """Ask for what the permit forbids, and stop when told no.

        The intent for this step names a settlement currency, which this
        permit does not authorize the agent to choose. The refusal is the
        correct outcome; there is no second attempt with the field removed,
        because that would be defeating the control rather than respecting it.
        """
        understanding, receipt_id, reason = self._read(
            await self._invoke(ctx, ctx.intent)
        )
        if understanding is not Understanding.CONFIRMED_REFUSED:
            self._gaps.append(
                "Expected an authorization refusal for an argument the permit "
                f"forbids ({', '.join(ctx.permit_forbidden_fields)}); got "
                f"{understanding.value!r} instead."
            )
        return OperationReport(
            understanding=understanding,
            receipt_id=receipt_id,
            idempotency_key=self.replay_key(ctx.intent),
            detail=reason,
        )

    async def verify_receipt_independently(
        self, ctx: IntegrationContext, receipt_id: str
    ) -> ReceiptVerificationReport:
        """Check the signature ourselves, and say what that does not prove.

        Three claims, not one boolean. The signature verifies. Issuer trust
        does not follow from it, because the key came from the same origin
        that issued the receipt, and an origin that can serve receipts can
        serve keys that validate forged ones. Downstream execution does not
        follow either: no signature ever established that money moved, and
        this integration has no independent view of the refund processor.
        """
        bundle_response = await ctx.gateway.get(
            ctx.portable_receipt_path.format(receipt_id=receipt_id),
            headers=ctx.auth_headers(),
        )
        bundle_response.raise_for_status()
        keys_response = await ctx.gateway.get(ctx.trust_keys_path)
        keys_response.raise_for_status()

        report = verify(
            bundle_response.json(),
            parse_key_document(keys_response.json()),
            # The key came from the audited origin. Saying OUT_OF_BAND_PIN
            # here would be lying to our own verifier to get a nicer answer.
            key_source=KeySource.ISSUER_ORIGIN,
            # No independent observation of the refund processor is available
            # to this integration, so downstream execution stays unestablished.
            downstream_observation=None,
        )
        return ReceiptVerificationReport(
            receipt_id=receipt_id,
            signature_valid=report.signature.established,
            issuer_trust_established=report.issuer_trust.established,
            downstream_execution_established=report.downstream_execution.established,
            verifier=(
                "failure_lab.verifier: a from-scratch Ed25519 check over the "
                "bundle's signing_input, importing neither the gateway nor its SDK"
            ),
            detail=(
                f"signature: {report.signature.reason} | "
                f"issuer trust: {report.issuer_trust.reason} | "
                f"downstream: {report.downstream_execution.reason}"
            ),
        )

    async def restart_and_recover(
        self, ctx: IntegrationContext, *, stage: RecoveryStage
    ) -> OperationReport:
        if stage is RecoveryStage.BEFORE_RESTART:
            # Write the intent down first. Whatever happens to this process
            # after the next line, the operation is on disk.
            self._record_intent(ctx, ctx.intent)
            understanding, receipt_id, reason = self._read(
                await self._invoke(ctx, ctx.intent)
            )
            return OperationReport(
                understanding=understanding,
                receipt_id=receipt_id,
                idempotency_key=self.replay_key(ctx.intent),
                detail=reason,
            )

        # A new process. Everything it knows is on disk or re-derivable from
        # the business operation -- and the replay key is re-derivable, which
        # is the only reason this recovery is possible at all.
        journaled = self._read_journal(ctx).get(ctx.intent.operation_id, {})
        key = self.replay_key(ctx.intent)
        if journaled.get("idempotency_key") not in (None, key):
            self._gaps.append(
                "The journalled replay key disagreed with the re-derived one; "
                "the re-derived key was used."
            )
        understanding, receipt_id, reason = self._read(
            await self._invoke(ctx, ctx.intent)
        )
        return OperationReport(
            understanding=understanding,
            receipt_id=receipt_id,
            idempotency_key=key,
            detail=(
                "recovered as a fresh process; re-presented the replay key "
                f"derived from {ctx.intent.operation_id!r} and the gateway "
                f"answered: {reason}"
            ),
        )

    def documentation_gaps(self) -> list[str]:
        return list(self._gaps)


CANDIDATE = ReferenceIntegration

__all__ = ["CANDIDATE", "DOCUMENTATION_GAPS", "JOURNAL_NAME", "ReferenceIntegration"]
