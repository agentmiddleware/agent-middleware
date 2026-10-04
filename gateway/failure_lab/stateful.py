"""Property-based, stateful exploration of the governed-call lifecycle.

The fourteen scenarios in :mod:`failure_lab.scenarios` each ask one question
the authors already knew to ask. This module asks the questions nobody wrote
down: it generates command sequences from a seeded random source, drives them
against a real gateway, and checks a fixed set of invariants against what was
observed. Its value is entirely in finding interleavings a human would not
have enumerated -- a revoke between the debit and the claim, a reconcile
between two retries that present different keys for the same refund.

Three commitments make the output usable as evidence rather than anecdote:

**Seeded, therefore reproducible.** Every sequence comes from
``random.Random(seed)``. The master seed and each sequence's derived seed are
recorded in the result, so ``--seed 7`` run tomorrow generates the same work.
Identifiers are derived from the seed too, not minted per process.

**Invariants are stated over observations, never over the implementation.**
Each invariant is phrased as a relation between numbers taken from the
independent effect ledger, the fault layer outside the gateway, and the
client's own view of its attempts. None of them says "the code does X". Where
a check can only be made against a gateway-reported number -- the wallet's
own debit and receipt rows -- the finding carries ``source:
"gateway-reported"`` and is never presented as an independent observation.

**A violation is reduced before it is reported.** A forty-command sequence
that trips an invariant is not a bug report; it is a haystack. The shrinker
repeatedly drops commands and re-runs, keeping any shorter sequence that
still trips the *same* invariant, and records how much of its budget it
spent. A sequence that will not reproduce is reported unminimized and
labelled as such, because a flaky violation is a real thing to know about.

What this module does **not** establish: the absence of violations is not a
proof of correctness. It is the statement that this seed, at this length,
found none -- which is what the result document says, in those words.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import random
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from failure_lab.configurations import (
    AttemptOutcome,
    Configuration,
    LabEnvironment,
    Target,
    configured_target,
)
from failure_lab.faults import FaultMode, FaultPlan
from failure_lab.gateway import CRASH_BOUNDARIES, GatewayUnderTest, Tenant
from failure_lab.identity import KeyPolicy, OperationIdentity
from failure_lab.refund_tool import RefundRequest
from failure_lab.verifier import (
    ClaimStatus,
    KeySource,
    parse_key_document,
    tamper,
    verify,
)

#: The configuration this harness drives. The naive downstream is deliberate:
#: it has no idempotency of its own, so every guarantee observed here is the
#: gateway's and nothing is borrowed from a well-built tool.
EXPLORED_CONFIGURATION = Configuration.GATEWAY_NAIVE

DEFAULT_SEQUENCES = 8
DEFAULT_MAX_LENGTH = 12
DEFAULT_SHRINK_BUDGET = 40

#: Statuses that mean the gateway refused the call before any dispatch could
#: have happened. Membership is read from the client's view of the response,
#: not from gateway internals. Kept deliberately small: every status left out
#: is treated as "the key may have been accepted", which weakens the bounds
#: below rather than inventing violations.
#:
#: ``http_error`` is deliberately **not** here. It is what the client sees
#: when the gateway answered with a non-200 status, which tells the caller
#: nothing about whether a dispatch happened -- ``_gateway_visible_state``
#: in :mod:`failure_lab.configurations` classifies it ``no_information`` for
#: exactly that reason. Counting it as a refusal would let a 500 that
#: followed a real execution be read as "every attempt was refused, yet the
#: ledger holds an execution", which is an accusation built out of the
#: harness's own guess.
PRE_DISPATCH_REFUSALS = frozenset(
    {
        "denied",
        "insufficient_funds",
        "invalid_params",
        "key_conflict",
        "rejected",
    }
)

#: Fault labels under which the downstream's answer reached the gateway
#: intact. Only these count as evidence that a downstream execution was
#: confirmed; everything else withheld, corrupted or truncated the response.
EVIDENCED_FAULTS = frozenset({FaultMode.NORMAL.value, FaultMode.DELAYED.value})

#: Post-execution faults: the tool ran, the caller was not told so.
WITHHOLDING_MODES = (
    FaultMode.RESPONSE_LOST_AFTER_EXECUTION,
    FaultMode.CONNECTION_FAILURE_AFTER_EXECUTION,
    FaultMode.CRASH_AFTER_EXECUTION,
    FaultMode.HTTP_ERROR_AFTER_EXECUTION,
)

#: Pre-execution faults: nothing ran.
PRE_DISPATCH_MODES = (
    FaultMode.CONNECTION_FAILURE_BEFORE_EXECUTION,
    FaultMode.HTTP_ERROR_BEFORE_EXECUTION,
    FaultMode.INITIALIZE_FAILURE,
)


# --------------------------------------------------------------------------- #
# Redaction                                                                     #
# --------------------------------------------------------------------------- #

_SENSITIVE_FIELDS = frozenset(
    {
        "admin_api_key",
        "api_key",
        "apikey",
        "authorization",
        "bearer_token",
        "control_token",
        "credentials",
        "key_prefix",
        "password",
        "private_key",
        "secret",
        "signing_seed",
        "sponsor_wallet_id",
        "wallet",
        "wallet_id",
        "x-api-key",
        "x_api_key",
    }
)
_SENSITIVE_SUFFIXES = (
    "_api_key",
    "_credential",
    "_password",
    "_private_key",
    "_secret",
    "_token",
    "_wallet",
    "_wallet_id",
)
_CREDENTIAL_RE = re.compile(r"(?<![A-Za-z0-9])(?:b2a|amw)_[A-Za-z0-9_-]{12,}")
_LAB_ADMIN_RE = re.compile(r"(?<![A-Za-z0-9])lab-admin-[A-Za-z0-9_-]{8,}")
_WALLET_RE = re.compile(r"(?<![A-Za-z0-9])(?:agt|spn)-[A-Za-z0-9_-]{6,}")

#: Literal secret values this process minted, scrubbed by value wherever they
#: appear. Field names and prefix patterns cannot catch everything: the
#: downstream bearer token and the control token are bare
#: ``secrets.token_urlsafe`` strings with no prefix, so a copy of one inside a
#: free-text exception message would otherwise travel intact. Registered by
#: :func:`run_sequence` from the environment it was handed.
_LITERAL_SECRETS: set[str] = set()

#: Below this length a "secret" is too short to scrub by value without
#: mangling unrelated text.
_MIN_LITERAL_SECRET = 12


def register_secret(value: str | None) -> None:
    """Add a literal secret to the by-value scrub set. Idempotent."""
    if isinstance(value, str) and len(value) >= _MIN_LITERAL_SECRET:
        _LITERAL_SECRETS.add(value)


def _scrub_text(value: str) -> str:
    cleaned = value
    for secret in _LITERAL_SECRETS:
        if secret in cleaned:
            cleaned = cleaned.replace(secret, "<redacted-credential>")
    cleaned = _CREDENTIAL_RE.sub("<redacted-credential>", cleaned)
    cleaned = _LAB_ADMIN_RE.sub("<redacted-credential>", cleaned)
    return _WALLET_RE.sub("<redacted-wallet>", cleaned)


def redact(value: Any) -> Any:
    """Strip credential material and tenant wallet ids from anything emitted.

    Same shape as ``scripts/invariant_attacks/redact_evidence.py``: field
    names first, then a pass over every string for credential and wallet
    patterns, so a token that arrives under an unexpected key is still caught.

    Three additions over that file, each closing a hole a real document
    reached through: dict *keys* are scrubbed as well as values, because a
    mapping keyed by credential leaks through a key; ``set`` and ``bytes``
    are recursed into rather than passed through to ``json.dumps(...,
    default=str)``, which would have stringified them unredacted; and
    literal secrets registered for this run are removed by value, which is
    the only thing that catches an unprefixed token quoted inside an error
    message.

    What it does **not** do is guess at entropy. A string that is secret but
    matches no pattern, carries no telltale field name and was never
    registered survives -- which is why the emitted documents are built from
    hashes and ids rather than payloads in the first place.
    """
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            normalized = str(key).lower()
            safe_key = _scrub_text(key) if isinstance(key, str) else key
            if normalized in _SENSITIVE_FIELDS or normalized.endswith(
                _SENSITIVE_SUFFIXES
            ):
                out[safe_key] = "<redacted>"
            else:
                out[safe_key] = redact(item)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, (set, frozenset)):
        # Sorted, because a set's iteration order would make the emitted
        # document differ between runs of the same seed.
        return sorted((redact(item) for item in value), key=str)
    if isinstance(value, str):
        return _scrub_text(value)
    if isinstance(value, (bytes, bytearray)):
        return _scrub_text(bytes(value).decode("utf-8", "replace"))
    return value


# --------------------------------------------------------------------------- #
# The state machine                                                             #
# --------------------------------------------------------------------------- #


class OperationState(str, Enum):
    """The lifecycle of one governed operation, as the harness models it.

    This is the harness's model, not a mirror of any table in the product.
    Transitions are driven by what the client saw and what the fault layer
    observed, so the model can be wrong about the gateway's internal state
    without corrupting an invariant -- the invariants read observations.
    """

    #: A business operation exists; no authority has been attached to it.
    CREATED = "CREATED"
    #: A permit covering it is active and bound to the caller's key.
    AUTHORIZED = "AUTHORIZED"
    #: The gateway admitted the call (idempotency record, budget reservation)
    #: but no dispatch claim has been taken.
    ACCEPTED = "ACCEPTED"
    #: The one-shot dispatch claim is committed; the send may or may not have
    #: left the process.
    DISPATCH_PENDING = "DISPATCH_PENDING"
    #: The fault layer saw the execution request cross it.
    DISPATCHED = "DISPATCHED"
    #: The caller was told, in a form it can act on, that the action happened.
    SUCCEEDED = "SUCCEEDED"
    #: The caller was told the action did not happen.
    FAILED = "FAILED"
    #: Nobody can say. The honest terminal state for a severed post-dispatch
    #: outcome, and the one this whole lab exists to keep distinct.
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    #: An operator sweep has run over it since it was last ambiguous.
    RECONCILED = "RECONCILED"
    #: The authority behind it was withdrawn before it finalized.
    REVOKED = "REVOKED"


#: Client-visible status -> the state the model moves the operation to.
_STATUS_STATES: dict[str, OperationState] = {
    "success": OperationState.SUCCEEDED,
    "replayed": OperationState.SUCCEEDED,
    "denied": OperationState.FAILED,
    "insufficient_funds": OperationState.FAILED,
    "invalid_params": OperationState.FAILED,
    "key_conflict": OperationState.FAILED,
    "failed_refunded": OperationState.FAILED,
    "failed_unrefunded": OperationState.FAILED,
    "response_rejected": OperationState.FAILED,
    "rejected": OperationState.FAILED,
    "delivery_uncertain": OperationState.OUTCOME_UNKNOWN,
    "timeout": OperationState.OUTCOME_UNKNOWN,
    "transport_error": OperationState.OUTCOME_UNKNOWN,
    "gateway_process_died": OperationState.OUTCOME_UNKNOWN,
    "internal_error": OperationState.OUTCOME_UNKNOWN,
    "error": OperationState.OUTCOME_UNKNOWN,
    # A non-200 from the gateway and an exception inside it both leave the
    # caller unable to say whether the action happened. FAILED would be the
    # flattering reading of an answer that carries no information.
    "http_error": OperationState.OUTCOME_UNKNOWN,
    "gateway_error": OperationState.OUTCOME_UNKNOWN,
    "in_progress": OperationState.DISPATCH_PENDING,
}

#: Crash boundary -> how far the operation had provably got when the process
#: died. Read from the boundary the harness itself asked for, so it is a
#: statement about the injected fault, not a claim about the gateway.
_CRASH_STATES: dict[str, OperationState] = {
    "after_idempotency_begin": OperationState.ACCEPTED,
    "after_prepare": OperationState.ACCEPTED,
    "after_debit": OperationState.ACCEPTED,
    "after_attach_charge": OperationState.ACCEPTED,
    "after_claim": OperationState.DISPATCH_PENDING,
    "after_upstream_response": OperationState.DISPATCHED,
    "after_terminal_commit": OperationState.DISPATCHED,
}


class CommandKind(str, Enum):
    CREATE_PERMIT = "create_permit"
    AUTHORIZE = "authorize"
    INVOKE = "invoke"
    CONCURRENT_SAME_KEY = "concurrent_same_key"
    CONCURRENT_BUDGET_RACE = "concurrent_budget_race"
    INJECT_LOST_RESPONSE = "inject_lost_response"
    INJECT_PRE_DISPATCH_FAILURE = "inject_pre_dispatch_failure"
    TIME_OUT = "time_out"
    REVOKE_PERMIT = "revoke_permit"
    CRASH_AT = "crash_at"
    RESTART = "restart"
    RETRY_SAME_KEY = "retry_same_key"
    RETRY_NEW_KEY = "retry_new_key"
    RECONCILE = "reconcile"


@dataclass(frozen=True)
class Command:
    """One generated step, with every parameter fixed at generation time.

    Parameters are frozen into the command rather than drawn while running so
    that re-running a sub-sequence during shrinking replays the same intent.
    """

    kind: CommandKind
    params: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "params": dict(self.params)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Command:
        return cls(
            kind=CommandKind(data["kind"]), params=dict(data.get("params") or {})
        )

    def render(self) -> str:
        if not self.params:
            return self.kind.value
        args = ", ".join(
            f"{name}={value!r}" for name, value in sorted(self.params.items())
        )
        return f"{self.kind.value}({args})"


@dataclass(frozen=True)
class CommandOutcome:
    """What happened when the runner tried to apply one command."""

    index: int
    command: dict[str, Any]
    applied: bool
    state_before: str
    state_after: str
    #: Why the command could not apply. Absent when it applied.
    skip_reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "command": self.command,
            "applied": self.applied,
            "skip_reason": self.skip_reason,
            "state_before": self.state_before,
            "state_after": self.state_after,
            "detail": self.detail,
        }


# --------------------------------------------------------------------------- #
# Observation                                                                   #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Observation:
    """Everything one sequence produced, from the instruments that saw it.

    ``effects`` and ``crossings`` are independent of the gateway: the effect
    ledger is a SQLite file the gateway has no handle to, and the crossings
    were recorded by the ASGI layer sitting between the gateway and the tool.
    ``gateway`` is the gateway's own account of itself and is labelled that
    way wherever it is used.
    """

    seed: int
    sequence_seed: int
    sequence_index: int
    configuration: str
    credits_per_call: str
    commands: tuple[CommandOutcome, ...]
    #: business_operation_id -> final modelled state
    states: dict[str, str]
    #: business_operation_id -> idempotency keys the gateway did not refuse
    accepted_keys: dict[str, list[str]]
    #: business_operation_id -> every idempotency key presented for it
    presented_keys: dict[str, list[str]]
    #: permit_id -> {"max_credits": str, "keys": [idempotency keys presented]}
    permits: dict[str, dict[str, Any]]
    attempts: tuple[dict[str, Any], ...]
    effects: tuple[dict[str, Any], ...]
    crossings: tuple[dict[str, Any], ...]
    gateway: dict[str, Any] | None
    signature_checks: tuple[dict[str, Any], ...]
    #: The wallet's books as they stood the moment each reconciliation
    #: finished. A debit opened *after* a sweep has not been offered to one,
    #: so "reconciliation has run" is only meaningful as of a point in time.
    reconcile_checkpoints: tuple[dict[str, Any], ...]
    error: str | None = None

    @property
    def reconciled(self) -> bool:
        return bool(self.reconcile_checkpoints)

    # -- independent instruments -----------------------------------------

    def executions_for(self, operation_id: str) -> int:
        return sum(1 for row in self.effects if row.get("operation_id") == operation_id)

    def evidenced_crossings(self) -> list[dict[str, Any]]:
        """Crossings whose response reached the gateway intact."""
        return [
            row
            for row in self.crossings
            if row.get("reached_tool") and row.get("fault") in EVIDENCED_FAULTS
        ]

    def crossings_by_key(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.crossings:
            key = row.get("idempotency_key") or f"<no-key:{row.get('operation_id')}>"
            counts[key] = counts.get(key, 0) + 1
        return counts

    def executions_by_key(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.effects:
            key = str(row.get("idempotency_key"))
            counts[key] = counts.get(key, 0) + 1
        return counts

    def dispatches_by_key(self) -> dict[str, int]:
        """The larger of the two independent counts, per key.

        The fault layer records a crossing only once it has finished handling
        the request, so a caller that disconnects mid-delay leaves an
        execution the layer never logged. The effect ledger has it. Taking
        the maximum closes that hole in the direction that cannot invent a
        dispatch: both sources are outside the gateway.
        """
        crossings = self.crossings_by_key()
        executions = self.executions_by_key()
        return {
            key: max(crossings.get(key, 0), executions.get(key, 0))
            for key in set(crossings) | set(executions)
        }

    def refused_only_operations(self) -> list[str]:
        """Operations where every attempt the client made was refused."""
        by_operation: dict[str, list[str]] = {}
        for attempt in self.attempts:
            identity = attempt.get("identity") or {}
            operation_id = str(identity.get("business_operation_id"))
            by_operation.setdefault(operation_id, []).append(str(attempt.get("status")))
        return [
            operation_id
            for operation_id, statuses in by_operation.items()
            if statuses and all(status in PRE_DISPATCH_REFUSALS for status in statuses)
        ]

    # -- gateway-reported ------------------------------------------------

    def receipt_outcomes(self) -> list[str]:
        if not self.gateway:
            return []
        return [str(row.get("outcome")) for row in self.gateway.get("receipts", [])]

    def signature_check_errors(self) -> list[str]:
        """Receipts whose signature could not be checked at all.

        A receipt the gateway would not export, or a trust key document that
        would not parse, leaves ``SIGNED_RECEIPTS_RESIST_MUTATION`` with
        nothing to check. Silence would read as "checked, and fine", so the
        gap is reported as a run error.
        """
        return [
            f"receipt {check.get('receipt_id')}: signature not checked: {check['error']}"
            for check in self.signature_checks
            if check.get("error")
        ]

    def gateway_counts(self) -> dict[str, int]:
        """Wallet totals from the gateway's own tables, for custom invariants.

        Nothing in the default set reads this -- they use the per-sweep
        checkpoints instead, because an end-of-sequence total says nothing
        about whether a reconciler ever saw the rows. It is here because
        ``explore(invariants=...)`` takes a caller's own checks.
        """
        if not self.gateway:
            return {"debits": 0, "refunds": 0, "receipts": 0}
        return {
            "debits": int(self.gateway.get("debit_count", 0)),
            "refunds": int(self.gateway.get("refund_count", 0)),
            "receipts": int(self.gateway.get("receipt_count", 0)),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "sequence_seed": self.sequence_seed,
            "sequence_index": self.sequence_index,
            "configuration": self.configuration,
            "credits_per_call": self.credits_per_call,
            "commands": [item.as_dict() for item in self.commands],
            "states": dict(self.states),
            "accepted_keys": {k: list(v) for k, v in self.accepted_keys.items()},
            "presented_keys": {k: list(v) for k, v in self.presented_keys.items()},
            "permits": {k: dict(v) for k, v in self.permits.items()},
            "attempts": [dict(a) for a in self.attempts],
            "downstream_effects": {
                "source": "independent effect ledger (downstream-owned SQLite)",
                "rows": [dict(e) for e in self.effects],
            },
            "crossings": {
                "source": "fault layer between gateway and tool",
                "rows": [dict(c) for c in self.crossings],
            },
            "gateway": self.gateway,
            "signature_checks": [dict(s) for s in self.signature_checks],
            "reconcile_checkpoints": [dict(c) for c in self.reconcile_checkpoints],
            "reconciled": self.reconciled,
            "error": self.error,
        }


# --------------------------------------------------------------------------- #
# Invariants                                                                    #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Violation:
    """One invariant, one observation, and why the two disagree."""

    invariant: str
    statement: str
    detail: str
    #: Where the numbers in ``detail`` came from. A gateway-reported finding
    #: is a finding about the gateway's own books, and says so.
    source: str = "independent"
    evidence: dict[str, Any] = field(default_factory=dict)
    seed: int = 0
    sequence_seed: int = 0
    sequence_index: int = 0
    sequence: tuple[dict[str, Any], ...] = ()
    minimized_sequence: tuple[dict[str, Any], ...] = ()
    minimized: bool = False
    shrink_attempts: int = 0
    observation: dict[str, Any] = field(default_factory=dict)
    #: Which run ``observation`` came from: the minimized sequence when the
    #: shrinker reproduced the violation, the original otherwise.
    observation_of: str = "original"

    def as_dict(self) -> dict[str, Any]:
        return {
            "invariant": self.invariant,
            "statement": self.statement,
            "detail": self.detail,
            "source": self.source,
            "evidence": self.evidence,
            "seed": self.seed,
            "sequence_seed": self.sequence_seed,
            "sequence_index": self.sequence_index,
            "sequence": [dict(c) for c in self.sequence],
            "sequence_rendered": [Command.from_dict(c).render() for c in self.sequence],
            "minimized_sequence": [dict(c) for c in self.minimized_sequence],
            "minimized_sequence_rendered": [
                Command.from_dict(c).render() for c in self.minimized_sequence
            ],
            "minimized": self.minimized,
            "shrink_attempts": self.shrink_attempts,
            "observation_of": self.observation_of,
            "observation": self.observation,
        }


class Invariant:
    """A property of the observations. Never a property of the code.

    Subclasses state the property in English and check it arithmetically
    against :class:`Observation`. Returning ``None`` means "not contradicted
    by this observation" -- never "proved".
    """

    name: str = ""
    statement: str = ""

    def check(self, observation: Observation) -> Violation | None:
        raise NotImplementedError

    def describe(self) -> dict[str, str]:
        return {"name": self.name, "statement": self.statement}

    def _violation(
        self, detail: str, evidence: dict[str, Any], *, source: str = "independent"
    ) -> Violation:
        return Violation(
            invariant=self.name,
            statement=self.statement,
            detail=detail,
            source=source,
            evidence=evidence,
        )


class RejectionHasNoEffect(Invariant):
    name = "REJECTION_HAS_NO_EFFECT"
    statement = (
        "A business operation whose every attempt the gateway refused must "
        "leave no execution in the downstream system of record."
    )

    def check(self, observation: Observation) -> Violation | None:
        for operation_id in observation.refused_only_operations():
            executions = observation.executions_for(operation_id)
            if executions > 0:
                return self._violation(
                    f"{operation_id}: every attempt was refused, yet the effect "
                    f"ledger holds {executions} execution(s) of it",
                    {
                        "operation_id": operation_id,
                        "executions": executions,
                        "statuses": [
                            attempt.get("status")
                            for attempt in observation.attempts
                            if (attempt.get("identity") or {}).get(
                                "business_operation_id"
                            )
                            == operation_id
                        ],
                    },
                )
        return None


class ReceiptsDoNotOutrunEvidence(Invariant):
    name = "RECEIPTS_DO_NOT_OUTRUN_EVIDENCE"
    statement = (
        "The number of receipts claiming confirmed downstream success must "
        "not exceed the number of dispatches whose downstream response "
        "reached the gateway intact. A receipt is a statement about what the "
        "gateway knows; a withheld response is the gateway not knowing."
    )

    def check(self, observation: Observation) -> Violation | None:
        success_receipts = [
            outcome
            for outcome in observation.receipt_outcomes()
            if outcome == "success"
        ]
        evidenced = observation.evidenced_crossings()
        if len(success_receipts) > len(evidenced):
            return self._violation(
                f"{len(success_receipts)} receipt(s) record outcome 'success' but "
                f"only {len(evidenced)} dispatch(es) had their response delivered "
                "intact; the rest were withheld, severed or answered with an error",
                {
                    "success_receipts": len(success_receipts),
                    "evidenced_dispatches": len(evidenced),
                    "crossing_faults": [
                        row.get("fault") for row in observation.crossings
                    ],
                    "receipt_outcomes": observation.receipt_outcomes(),
                },
                source="gateway-reported receipts vs fault-layer observation",
            )
        return None


class AtMostOneDispatchPerKey(Invariant):
    name = "AT_MOST_ONE_DISPATCH_PER_ACCEPTED_KEY"
    statement = (
        "One accepted idempotency key yields at most one dispatch across the "
        "fault layer, however many times it is retried, crashed through or "
        "reconciled. This is the documented dispatch guarantee."
    )

    def check(self, observation: Observation) -> Violation | None:
        for key, count in sorted(observation.dispatches_by_key().items()):
            if count > 1:
                return self._violation(
                    f"idempotency key {key!r} produced {count} dispatches; the "
                    "documented guarantee is at most one per accepted key",
                    {
                        "idempotency_key": key,
                        "dispatches": count,
                        "fault_layer_crossings": observation.crossings_by_key().get(
                            key, 0
                        ),
                        "downstream_executions": observation.executions_by_key().get(
                            key, 0
                        ),
                        "crossings": [
                            row
                            for row in observation.crossings
                            if row.get("idempotency_key") == key
                        ],
                    },
                )
        return None


class BudgetNotExceeded(Invariant):
    name = "BUDGET_NOT_EXCEEDED"
    statement = (
        "Downstream work admitted under one permit, priced at the documented "
        "per-call rate, must not exceed that permit's credit limit -- however "
        "many callers presented it at once."
    )

    def check(self, observation: Observation) -> Violation | None:
        per_call = Decimal(observation.credits_per_call)
        executions_by_key = observation.executions_by_key()
        for permit_id, info in sorted(observation.permits.items()):
            limit = Decimal(str(info.get("max_credits", "0")))
            # One key presented many times is still one key's worth of work;
            # the ledger already counts executions, not presentations.
            keys = list(dict.fromkeys(info.get("keys", [])))
            executions = sum(executions_by_key.get(key, 0) for key in keys)
            consumed = per_call * executions
            if consumed > limit:
                return self._violation(
                    f"permit {permit_id} authorized {limit} credits but admitted "
                    f"{executions} downstream execution(s) at {per_call} credits each "
                    f"({consumed} credits of work)",
                    {
                        "permit_id": permit_id,
                        "max_credits": str(limit),
                        "credits_per_call": str(per_call),
                        "downstream_executions": executions,
                        "credits_of_work_admitted": str(consumed),
                        "keys_presented": keys,
                    },
                )
        if observation.gateway is not None:
            for row in observation.gateway.get("permits", []):
                spent = Decimal(str(row.get("spent_credits") or "0"))
                limit = Decimal(str(row.get("max_credits") or "0"))
                if spent > limit:
                    return self._violation(
                        f"permit {row.get('permit_id')} reports {spent} credits spent "
                        f"against a limit of {limit}",
                        {"permit": row},
                        source="gateway-reported",
                    )
        return None


class SignedReceiptsResistMutation(Invariant):
    name = "SIGNED_RECEIPTS_RESIST_MUTATION"
    statement = (
        "A receipt's signature must verify as issued, and must fail once a "
        "field the signature covers is edited. This says nothing about "
        "whether the receipted action occurred."
    )

    def check(self, observation: Observation) -> Violation | None:
        for check in observation.signature_checks:
            if check.get("error"):
                # The receipt would not export, or the trust key document
                # would not parse. That is the harness failing to obtain the
                # evidence, not the gateway failing to sign: reading it as a
                # signature failure would accuse the product of the one thing
                # this invariant is least entitled to guess at. It travels
                # out as a run error instead -- see
                # ``Observation.signature_check_errors``.
                continue
            if check.get("pristine") != ClaimStatus.ESTABLISHED.value:
                return self._violation(
                    f"receipt {check.get('receipt_id')} was issued by the gateway but "
                    f"its signature did not verify: {check.get('pristine_reason')}",
                    {"check": check},
                )
            if check.get("tampered") == ClaimStatus.ESTABLISHED.value:
                return self._violation(
                    f"receipt {check.get('receipt_id')} still verified after "
                    f"{check.get('tampered_field')} was edited to "
                    f"{check.get('tampered_value')!r}",
                    {"check": check},
                )
        return None


class EveryDebitAccountedFor(Invariant):
    name = "EVERY_DEBIT_ACCOUNTED_FOR"
    statement = (
        "At the moment a reconciliation sweep finishes, no debit stands "
        "alone: each one is either matched by a receipt stating the outcome "
        "it paid for, or reversed by a refund. Debits opened after that sweep "
        "are not its business."
    )

    def check(self, observation: Observation) -> Violation | None:
        for checkpoint in observation.reconcile_checkpoints:
            standing = int(checkpoint["debits"]) - int(checkpoint["refunds"])
            receipts = int(checkpoint["receipts"])
            if standing > receipts:
                return self._violation(
                    f"{standing} debit(s) stood unrefunded when the sweep after "
                    f"command {checkpoint['after_command']} finished, against "
                    f"{receipts} receipt(s)",
                    {"checkpoint": checkpoint},
                    source="gateway-reported",
                )
        return None


class ExecutionsBoundedByAcceptedKeys(Invariant):
    name = "EXECUTIONS_BOUNDED_BY_ACCEPTED_KEYS"
    statement = (
        "Downstream executions of one business operation never exceed the "
        "number of distinct idempotency keys the gateway accepted for it. An "
        "agent that presents one key gets at most one execution; an agent "
        "that mints a fresh key per attempt buys itself a fresh execution, "
        "and the count shows exactly that."
    )

    def check(self, observation: Observation) -> Violation | None:
        for operation_id, keys in sorted(observation.accepted_keys.items()):
            executions = observation.executions_for(operation_id)
            if executions > len(set(keys)):
                return self._violation(
                    f"{operation_id}: {executions} downstream execution(s) against "
                    f"{len(set(keys))} accepted idempotency key(s)",
                    {
                        "operation_id": operation_id,
                        "executions": executions,
                        "accepted_keys": sorted(set(keys)),
                        "presented_keys": sorted(
                            set(observation.presented_keys.get(operation_id, []))
                        ),
                    },
                )
        return None


def default_invariants() -> tuple[Invariant, ...]:
    return (
        RejectionHasNoEffect(),
        ReceiptsDoNotOutrunEvidence(),
        AtMostOneDispatchPerKey(),
        BudgetNotExceeded(),
        SignedReceiptsResistMutation(),
        EveryDebitAccountedFor(),
        ExecutionsBoundedByAcceptedKeys(),
    )


# --------------------------------------------------------------------------- #
# Generation                                                                    #
# --------------------------------------------------------------------------- #

_WEIGHTS: tuple[tuple[CommandKind, int], ...] = (
    (CommandKind.CREATE_PERMIT, 3),
    (CommandKind.AUTHORIZE, 4),
    (CommandKind.INVOKE, 5),
    (CommandKind.CONCURRENT_SAME_KEY, 3),
    (CommandKind.CONCURRENT_BUDGET_RACE, 3),
    (CommandKind.INJECT_LOST_RESPONSE, 3),
    (CommandKind.INJECT_PRE_DISPATCH_FAILURE, 2),
    (CommandKind.TIME_OUT, 2),
    (CommandKind.REVOKE_PERMIT, 2),
    (CommandKind.CRASH_AT, 3),
    (CommandKind.RESTART, 2),
    (CommandKind.RETRY_SAME_KEY, 4),
    (CommandKind.RETRY_NEW_KEY, 3),
    (CommandKind.RECONCILE, 3),
)


#: How often the generator ignores its own model and draws from everything.
#: Purely random sequences almost never reach an invoke -- a permit has to
#: exist before authority can be granted before a call can be made -- so a
#: generator with no model tests the skip path and nothing else. A generator
#: with a model and no wildcard never tests the skip path at all. This is the
#: mixture.
WILDCARD_RATE = 0.25


@dataclass
class _GeneratorModel:
    """A cheap, optimistic guess at where the sequence has got to.

    It exists only to bias generation toward sequences that reach an invoke.
    It cannot know whether a call will be denied, exhaust a budget or die at a
    boundary, so it assumes the happy path -- which is why even a
    model-guided sequence still produces skipped commands against the real
    system, and why the runner records them rather than failing.
    """

    permit_active: bool = False
    authorizable: bool = True
    authorized: bool = False
    invoked: bool = False
    crash_armed: bool = False
    crash_expected: bool = False

    def enabled(self, kind: CommandKind) -> bool:
        if kind is CommandKind.CREATE_PERMIT:
            return not self.permit_active
        if kind is CommandKind.AUTHORIZE:
            return self.permit_active and self.authorizable and not self.authorized
        if kind in (
            CommandKind.INVOKE,
            CommandKind.CONCURRENT_SAME_KEY,
            CommandKind.TIME_OUT,
        ):
            return self.authorized and not self.invoked
        if kind is CommandKind.CONCURRENT_BUDGET_RACE:
            return self.permit_active and (self.authorized or self.invoked)
        if kind is CommandKind.REVOKE_PERMIT:
            return self.permit_active
        if kind is CommandKind.CRASH_AT:
            return not self.crash_armed
        if kind is CommandKind.RESTART:
            return self.crash_armed or self.crash_expected
        if kind in (CommandKind.RETRY_SAME_KEY, CommandKind.RETRY_NEW_KEY):
            return self.invoked
        return True

    def advance(self, command: Command) -> None:
        kind = command.kind
        if kind is CommandKind.CREATE_PERMIT:
            self.permit_active = True
        elif kind is CommandKind.AUTHORIZE:
            self.authorized = True
            self.authorizable = False
        elif kind in (
            CommandKind.INVOKE,
            CommandKind.CONCURRENT_SAME_KEY,
            CommandKind.TIME_OUT,
        ):
            self.invoked = True
            self.authorized = False
            self.crash_armed = False
        elif kind is CommandKind.REVOKE_PERMIT:
            self.permit_active = False
            self.authorized = False
            # A revoked operation can be re-authorized under a fresh permit.
            self.authorizable = not self.invoked
        elif kind is CommandKind.CRASH_AT:
            self.crash_armed = True
        elif kind is CommandKind.RESTART:
            self.crash_armed = False
            self.crash_expected = False
        elif kind is CommandKind.INJECT_LOST_RESPONSE:
            if command.params.get("mode") == FaultMode.CRASH_AFTER_EXECUTION.value:
                self.crash_expected = True
        elif kind in (CommandKind.RETRY_SAME_KEY, CommandKind.RETRY_NEW_KEY):
            self.crash_armed = False


#: Commands that move the operation toward a call actually being made.
_LIFECYCLE_KINDS = frozenset(
    {
        CommandKind.CREATE_PERMIT,
        CommandKind.AUTHORIZE,
        CommandKind.INVOKE,
        CommandKind.CONCURRENT_SAME_KEY,
        CommandKind.CONCURRENT_BUDGET_RACE,
        CommandKind.TIME_OUT,
    }
)


def _progress_boost(kind: CommandKind, model: _GeneratorModel) -> int:
    """Weight lifecycle commands up until something has been invoked.

    Without this, a sequence spends its whole length arming faults and
    reconciling an empty wallet: arming commands are always applicable, so an
    unweighted draw finds them far more often than the three-step climb from
    permit to authority to call. The boost buys the sequence a dispatch, and
    the remaining draws are then spent on what happens around it.
    """
    if model.invoked or kind not in _LIFECYCLE_KINDS:
        return 1
    return 4


def generate_sequence(rng: random.Random, max_length: int) -> tuple[Command, ...]:
    """Draw one command sequence from the seeded source.

    Most draws come from the commands a rough model believes are currently
    applicable; one in four ignores the model entirely, so a revoke before any
    permit exists and a retry before anything was invoked both still occur.
    The runner decides legality against the real system, not this function.
    """
    kinds = [kind for kind, _ in _WEIGHTS]
    weights = [weight for _, weight in _WEIGHTS]
    length = rng.randint(3, max(3, max_length))
    model = _GeneratorModel()
    commands: list[Command] = []
    for _ in range(length):
        if rng.random() < WILDCARD_RATE:
            kind = rng.choices(kinds, weights=weights, k=1)[0]
        else:
            allowed = [
                (kind, weight * _progress_boost(kind, model))
                for kind, weight in _WEIGHTS
                if model.enabled(kind)
            ]
            if not allowed:
                allowed = list(_WEIGHTS)
            kind = rng.choices(
                [kind for kind, _ in allowed],
                weights=[weight for _, weight in allowed],
                k=1,
            )[0]
        command = Command(kind=kind, params=_draw_params(kind, rng))
        model.advance(command)
        commands.append(command)
    return tuple(commands)


def _draw_params(kind: CommandKind, rng: random.Random) -> dict[str, Any]:
    if kind is CommandKind.CREATE_PERMIT:
        # Small limits on purpose: a permit that can never be exhausted makes
        # the budget invariant unfalsifiable.
        return {"calls_authorized": rng.choice([1, 1, 2, 3, 8])}
    if kind is CommandKind.CRASH_AT:
        return {"boundary": rng.choice(list(CRASH_BOUNDARIES))}
    if kind is CommandKind.INJECT_LOST_RESPONSE:
        return {"mode": rng.choice([mode.value for mode in WITHHOLDING_MODES])}
    if kind is CommandKind.INJECT_PRE_DISPATCH_FAILURE:
        return {"mode": rng.choice([mode.value for mode in PRE_DISPATCH_MODES])}
    if kind is CommandKind.TIME_OUT:
        return {"where": rng.choice(["client", "upstream"])}
    if kind is CommandKind.CONCURRENT_SAME_KEY:
        return {"fanout": rng.choice([2, 3, 4])}
    if kind is CommandKind.CONCURRENT_BUDGET_RACE:
        return {"fanout": rng.choice([3, 4, 5])}
    return {}


# --------------------------------------------------------------------------- #
# Execution                                                                     #
# --------------------------------------------------------------------------- #


class _SequenceRunner:
    """Applies one command sequence to one freshly-built target.

    Holds the model state (permit, identity, lifecycle state) and the
    bookkeeping the invariants need: which keys were presented under which
    permit, and which of them the gateway did not refuse.
    """

    def __init__(
        self,
        target: Target,
        *,
        seed: int,
        sequence_seed: int,
        sequence_index: int,
        credits_per_call: Decimal,
        call_timeout_seconds: float,
    ) -> None:
        gateway, tenant, _ = target.require_gateway()
        self.target = target
        self.gateway: GatewayUnderTest = gateway
        self.tenant: Tenant = tenant
        self.seed = seed
        self.sequence_seed = sequence_seed
        self.sequence_index = sequence_index
        self.credits_per_call = credits_per_call
        self.call_timeout_seconds = call_timeout_seconds
        # Long enough for the gateway to finish its own upstream timeout and
        # answer; short enough that a same-key retry parked behind an
        # in-progress record (the gateway polls for the winner's result) does
        # not stall the whole exploration. Giving up here is recorded as a
        # client timeout, which is what it is.
        self.attempt_timeout = call_timeout_seconds + 2.0

        self.permit_id: str | None = None
        self.permit_active = False
        self.state = OperationState.CREATED
        self.identity: OperationIdentity | None = None
        self.new_key_ordinal = 0
        self.pending_crash: str | None = None
        self.gateway_crashed = False
        self.reconcile_checkpoints: list[dict[str, Any]] = []

        self.primary_operation = f"refund:pay-s{seed}-q{sequence_index}"
        self.race_ordinal = 0
        self.outcomes: list[CommandOutcome] = []
        self.attempts: list[AttemptOutcome] = []
        self.states: dict[str, OperationState] = {self.primary_operation: self.state}
        self.accepted_keys: dict[str, list[str]] = {}
        self.presented_keys: dict[str, list[str]] = {}
        self.permits: dict[str, dict[str, Any]] = {}
        #: idempotency key -> the first permit that admitted it. An execution
        #: is bought once, by one permit; see :meth:`_record_attempt`.
        self.key_permit: dict[str, str] = {}

    # -- helpers ---------------------------------------------------------

    def _refund(self, operation_id: str) -> RefundRequest:
        payment_id = operation_id.split(":", 1)[1]
        return RefundRequest(
            operation_id=operation_id,
            customer_id=f"cus_s{self.seed}",
            payment_id=payment_id,
            amount=5000,
        )

    def _identity_for(self, operation_id: str) -> OperationIdentity:
        return OperationIdentity.first_attempt(
            operation_id, key_policy=KeyPolicy.BUSINESS
        )

    def _record_attempt(
        self, outcome: AttemptOutcome, *, permit_id: str | None
    ) -> None:
        self.attempts.append(outcome)
        identity = outcome.identity
        operation_id = str(identity.get("business_operation_id"))
        key = str(identity.get("idempotency_key"))
        self.presented_keys.setdefault(operation_id, []).append(key)
        refused = outcome.status in PRE_DISPATCH_REFUSALS
        if not refused:
            self.accepted_keys.setdefault(operation_id, []).append(key)
        if permit_id is None or permit_id not in self.permits:
            return
        # BUDGET_NOT_EXCEEDED charges a permit for the downstream executions
        # of the keys recorded against it, so a key may be attributed to at
        # most one permit, and only when this permit did not refuse it
        # outright. Attributing a key to every permit it was ever presented
        # under manufactures violations: present a key that already executed
        # under a large permit to a fresh one-call permit, watch the gateway
        # correctly refuse it with `key_conflict`, and the arithmetic would
        # still bill the new permit for the old permit's execution. That is
        # an accusation against the gateway assembled entirely out of the
        # harness's own bookkeeping. First permit to admit a key owns it.
        if refused or key in self.key_permit:
            return
        self.key_permit[key] = permit_id
        self.permits[permit_id]["keys"].append(key)

    def _advance(self, operation_id: str, outcome: AttemptOutcome) -> OperationState:
        state = _STATUS_STATES.get(outcome.status, OperationState.OUTCOME_UNKNOWN)
        if outcome.status == "gateway_process_died" and self.pending_crash is not None:
            state = _CRASH_STATES.get(
                self.pending_crash, OperationState.OUTCOME_UNKNOWN
            )
        self.states[operation_id] = state
        return state

    async def _submit(
        self,
        identity: OperationIdentity,
        operation_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> AttemptOutcome:
        agent = self.target.gateway_agent(self.permit_id)
        kwargs = {"timeout_seconds": timeout_seconds or self.attempt_timeout}
        with contextlib.ExitStack() as stack:
            crash_state: dict[str, Any] | None = None
            if self.pending_crash is not None:
                crash_state = stack.enter_context(
                    self.gateway.crash_at(self.pending_crash)
                )
            outcome = await agent.submit(identity, self._refund(operation_id), **kwargs)
        if crash_state is not None and crash_state.get("fired"):
            self.gateway_crashed = True
        self._record_attempt(outcome, permit_id=self.permit_id)
        return outcome

    # -- command application ----------------------------------------------

    async def apply(self, index: int, command: Command) -> CommandOutcome:
        before = self.state
        handler = getattr(self, f"_do_{command.kind.value}")
        try:
            skip_reason, detail = await handler(command.params)
        except Exception as exc:  # noqa: BLE001 - one bad command must not end the run
            # Not the same thing as inapplicable: the command was legal and
            # the harness failed to carry it out. It is counted as not
            # applied and surfaced in the result's errors.
            skip_reason = f"command raised {type(exc).__name__}: {exc}"
            detail = {"error": f"{type(exc).__name__}: {exc}"}
        outcome = CommandOutcome(
            index=index,
            command=command.as_dict(),
            applied=skip_reason is None,
            state_before=before.value,
            state_after=self.state.value,
            skip_reason=skip_reason,
            detail=detail,
        )
        self.outcomes.append(outcome)
        return outcome

    async def _do_create_permit(
        self, params: dict[str, Any]
    ) -> tuple[str | None, dict[str, Any]]:
        if self.permit_active:
            return "a permit is already active for this operation", {}
        calls = int(params.get("calls_authorized", 1))
        max_credits = self.credits_per_call * calls
        permit = await self.gateway.issue_permit(self.tenant, max_credits=max_credits)
        self.permit_id = str(permit["permit_id"])
        self.permit_active = True
        self.permits[self.permit_id] = {
            "max_credits": str(max_credits),
            "calls_authorized": calls,
            "keys": [],
        }
        return None, {"permit_id": self.permit_id, "max_credits": str(max_credits)}

    async def _do_authorize(
        self, params: dict[str, Any]
    ) -> tuple[str | None, dict[str, Any]]:
        if not self.permit_active:
            return "no active permit to authorize against", {}
        if self.state not in (OperationState.CREATED, OperationState.REVOKED):
            return f"operation is {self.state.value}, not awaiting authority", {}
        self.state = OperationState.AUTHORIZED
        self.states[self.primary_operation] = self.state
        return None, {"permit_id": self.permit_id}

    async def _do_invoke(
        self, params: dict[str, Any]
    ) -> tuple[str | None, dict[str, Any]]:
        if self.state is not OperationState.AUTHORIZED:
            return f"operation is {self.state.value}, not AUTHORIZED", {}
        if self.identity is not None:
            return "already invoked; use a retry command", {}
        self.identity = self._identity_for(self.primary_operation)
        outcome = await self._submit(self.identity, self.primary_operation)
        self.state = self._advance(self.primary_operation, outcome)
        self.pending_crash = None
        return None, {
            "status": outcome.status,
            "client_visible": outcome.client_visible_state,
        }

    async def _do_concurrent_same_key(
        self, params: dict[str, Any]
    ) -> tuple[str | None, dict[str, Any]]:
        if self.state is not OperationState.AUTHORIZED:
            return f"operation is {self.state.value}, not AUTHORIZED", {}
        if self.identity is not None:
            return "already invoked; use a retry command", {}
        fanout = int(params.get("fanout", 2))
        base = self._identity_for(self.primary_operation)
        self.identity = base
        identities = [base] + [base.retry() for _ in range(fanout - 1)]
        results = await asyncio.gather(
            *(self._submit_isolated(i, self.primary_operation) for i in identities)
        )
        for outcome in results:
            self._record_attempt(outcome, permit_id=self.permit_id)
        chosen = _dominant(results)
        self.state = self._advance(self.primary_operation, chosen)
        self.pending_crash = None
        return None, {
            "fanout": fanout,
            "statuses": sorted(outcome.status for outcome in results),
        }

    async def _do_concurrent_budget_race(
        self, params: dict[str, Any]
    ) -> tuple[str | None, dict[str, Any]]:
        if not self.permit_active:
            return "no active permit to race against", {}
        if self.state is OperationState.CREATED:
            return "operation is CREATED; authorize first", {}
        fanout = int(params.get("fanout", 3))
        operations = []
        for _ in range(fanout):
            self.race_ordinal += 1
            operations.append(f"{self.primary_operation}-r{self.race_ordinal}")
        identities = [self._identity_for(operation) for operation in operations]
        results = await asyncio.gather(
            *(
                self._submit_isolated(identity, operation)
                for identity, operation in zip(identities, operations, strict=True)
            )
        )
        for outcome, operation in zip(results, operations, strict=True):
            self._record_attempt(outcome, permit_id=self.permit_id)
            self._advance(operation, outcome)
        self.pending_crash = None
        return None, {
            "fanout": fanout,
            "operations": operations,
            "statuses": sorted(outcome.status for outcome in results),
        }

    async def _submit_isolated(
        self, identity: OperationIdentity, operation_id: str
    ) -> AttemptOutcome:
        """A concurrent submit. Crash injection is not applied to these.

        A crash hook patches a class method for the duration of one call; with
        several calls in flight it would fire on whichever arrived first,
        which is a race the harness could not attribute afterwards. Concurrent
        commands therefore run uninjected, and the result says so.
        """
        agent = self.target.gateway_agent(self.permit_id)
        return await agent.submit(
            identity, self._refund(operation_id), timeout_seconds=self.attempt_timeout
        )

    async def _do_inject_lost_response(
        self, params: dict[str, Any]
    ) -> tuple[str | None, dict[str, Any]]:
        mode = FaultMode(
            str(params.get("mode", FaultMode.RESPONSE_LOST_AFTER_EXECUTION.value))
        )
        plan = self.target.injector.arm(
            FaultPlan(
                mode=mode,
                operation_id=None,
                remaining=1,
                hold_seconds=min(5.0, self.call_timeout_seconds + 3.0),
                label="stateful:lost_response",
            )
        )
        return None, {"mode": plan.mode.value}

    async def _do_inject_pre_dispatch_failure(
        self, params: dict[str, Any]
    ) -> tuple[str | None, dict[str, Any]]:
        mode = FaultMode(
            str(params.get("mode", FaultMode.CONNECTION_FAILURE_BEFORE_EXECUTION.value))
        )
        plan = self.target.injector.arm(
            FaultPlan(
                mode=mode,
                operation_id=None,
                remaining=1,
                label="stateful:pre_dispatch",
            )
        )
        return None, {"mode": plan.mode.value}

    async def _do_time_out(
        self, params: dict[str, Any]
    ) -> tuple[str | None, dict[str, Any]]:
        if self.identity is not None:
            return "already invoked; use a retry command", {}
        if self.state is not OperationState.AUTHORIZED:
            return f"operation is {self.state.value}, not AUTHORIZED", {}
        where = str(params.get("where", "upstream"))
        delay_ms = int(self.call_timeout_seconds * 1000) + 600
        self.target.injector.arm(
            FaultPlan(
                mode=FaultMode.DELAYED,
                operation_id=self.primary_operation,
                remaining=1,
                delay_ms=delay_ms,
                label=f"stateful:timeout:{where}",
            )
        )
        self.identity = self._identity_for(self.primary_operation)
        timeout = 0.35 if where == "client" else self.attempt_timeout
        outcome = await self._submit(
            self.identity, self.primary_operation, timeout_seconds=timeout
        )
        self.state = self._advance(self.primary_operation, outcome)
        self.pending_crash = None
        return None, {"where": where, "delay_ms": delay_ms, "status": outcome.status}

    async def _do_revoke_permit(
        self, params: dict[str, Any]
    ) -> tuple[str | None, dict[str, Any]]:
        if not self.permit_active or self.permit_id is None:
            return "no active permit to revoke", {}
        result = await self.gateway.revoke_permit(self.tenant, self.permit_id)
        self.permit_active = False
        if self.state in (
            OperationState.CREATED,
            OperationState.AUTHORIZED,
            OperationState.ACCEPTED,
            OperationState.DISPATCH_PENDING,
        ):
            self.state = OperationState.REVOKED
            self.states[self.primary_operation] = self.state
        return None, {"permit_id": self.permit_id, "status": result.get("status")}

    async def _do_crash_at(
        self, params: dict[str, Any]
    ) -> tuple[str | None, dict[str, Any]]:
        if self.pending_crash is not None:
            return f"a crash at {self.pending_crash} is already armed", {}
        boundary = str(params.get("boundary", "after_claim"))
        if boundary not in CRASH_BOUNDARIES:
            return f"unknown crash boundary {boundary}", {}
        self.pending_crash = boundary
        return None, {"boundary": boundary, "simulated": True}

    async def _do_restart(
        self, params: dict[str, Any]
    ) -> tuple[str | None, dict[str, Any]]:
        """Bring back whatever died, and drop the process's pooled state.

        The downstream comes back up. The gateway's connection pool is
        disposed, which is the part of a restart that is observable in
        process: committed rows are untouched, and anything a dead activation
        was holding is released. Nothing here recovers an in-flight call --
        that is reconciliation's job, and a separate command.
        """
        downstream_down = self.target.injector.crashed
        if (
            not downstream_down
            and not self.gateway_crashed
            and self.pending_crash is None
        ):
            return "nothing has crashed; there is nothing to restart", {}
        self.target.injector.restart()
        self.pending_crash = None
        self.gateway_crashed = False
        pool_disposed = False
        try:
            from app.db.database import get_engine

            engine = get_engine()
            if engine is not None:
                await engine.dispose()
                pool_disposed = True
        except Exception as exc:  # noqa: BLE001 - a restart that cannot recycle is still a restart
            return None, {"downstream_was_down": downstream_down, "error": str(exc)}
        return None, {
            "downstream_was_down": downstream_down,
            "gateway_pool_disposed": pool_disposed,
        }

    async def _do_retry_same_key(
        self, params: dict[str, Any]
    ) -> tuple[str | None, dict[str, Any]]:
        if self.identity is None:
            return "nothing has been invoked, so there is no key to retry", {}
        retry = self.identity.retry()
        outcome = await self._submit(retry, self.primary_operation)
        self.state = self._advance(self.primary_operation, outcome)
        self.pending_crash = None
        return None, {"status": outcome.status, "idempotency_key_reused": True}

    async def _do_retry_new_key(
        self, params: dict[str, Any]
    ) -> tuple[str | None, dict[str, Any]]:
        if self.identity is None:
            return "nothing has been invoked, so there is nothing to re-key", {}
        self.new_key_ordinal += 1
        fresh = replace(
            self.identity,
            idempotency_key=f"idem:{self.primary_operation}:k{self.new_key_ordinal}",
            request_id=f"req_s{self.seed}q{self.sequence_index}n{self.new_key_ordinal}",
            agent_generation=self.identity.agent_generation + 1,
        )
        self.identity = fresh
        outcome = await self._submit(fresh, self.primary_operation)
        self.state = self._advance(self.primary_operation, outcome)
        self.pending_crash = None
        return None, {"status": outcome.status, "new_key_ordinal": self.new_key_ordinal}

    async def _do_reconcile(
        self, params: dict[str, Any]
    ) -> tuple[str | None, dict[str, Any]]:
        # The idle window is set to zero rather than waited out, and the
        # attempt rows are deliberately NOT backdated: ageing them writes a
        # `dispatched_at` onto attempts that never dispatched, which is a row
        # shape the product cannot produce and which its reconciler then
        # refuses -- a finding about the harness, not about the gateway.
        summary = await self.gateway.reconcile(idle_seconds=0)
        settled = await self.gateway.snapshot(self.tenant)
        checkpoint = {
            "after_command": len(self.outcomes),
            "debits": settled.debit_count,
            "refunds": settled.refund_count,
            "receipts": settled.receipt_count,
            "receipt_outcomes": settled.receipt_outcomes(),
            "source": "gateway-reported",
        }
        self.reconcile_checkpoints.append(checkpoint)
        for operation_id, state in list(self.states.items()):
            if state in (
                OperationState.ACCEPTED,
                OperationState.DISPATCH_PENDING,
                OperationState.DISPATCHED,
                OperationState.OUTCOME_UNKNOWN,
            ):
                self.states[operation_id] = OperationState.RECONCILED
        self.state = self.states.get(self.primary_operation, self.state)
        return None, {"idle_seconds": 0, "summary": summary, "checkpoint": checkpoint}


def _dominant(outcomes: Sequence[AttemptOutcome]) -> AttemptOutcome:
    """Pick the outcome that best describes a concurrent burst.

    A success beats an in-progress beats a refusal: the burst as a whole got
    as far as its furthest member.
    """
    order = {
        "success": 0,
        "delivery_uncertain": 1,
        "timeout": 2,
        "gateway_process_died": 2,
        "in_progress": 3,
    }
    return min(outcomes, key=lambda outcome: order.get(outcome.status, 4))


async def _collect_signature_checks(
    gateway: GatewayUnderTest,
    tenant: Tenant,
    receipts: Sequence[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Verify each receipt as issued, then verify it again once edited.

    The edit targets a field inside ``signing_input``, which is the only edit
    an attacker could usefully make: the envelope is not covered and editing
    it proves nothing either way.
    """
    if not receipts:
        return []
    try:
        keys = parse_key_document(await gateway.trust_keys())
    except Exception as exc:  # noqa: BLE001 - a missing key document is a finding, not a crash
        return [{"receipt_id": None, "error": f"{type(exc).__name__}: {exc}"}]
    checks: list[dict[str, Any]] = []
    for row in list(receipts)[:limit]:
        receipt_id = str(row.get("receipt_id"))
        try:
            bundle = await gateway.portable_receipt(tenant, receipt_id)
        except Exception as exc:  # noqa: BLE001
            checks.append(
                {"receipt_id": receipt_id, "error": f"{type(exc).__name__}: {exc}"}
            )
            continue
        pristine = verify(bundle, keys, key_source=KeySource.ISSUER_ORIGIN)
        original_outcome = str(row.get("outcome"))
        forged = "success" if original_outcome != "success" else "denied"
        mutated = verify(
            tamper(bundle, "signing_input.outcome", forged),
            keys,
            key_source=KeySource.ISSUER_ORIGIN,
        )
        checks.append(
            {
                "receipt_id": receipt_id,
                "receipt_outcome": original_outcome,
                "pristine": pristine.signature.status.value,
                "pristine_reason": pristine.signature.reason,
                "tampered_field": "signing_input.outcome",
                "tampered_value": forged,
                "tampered": mutated.signature.status.value,
                "tampered_reason": mutated.signature.reason,
                "issuer_trust": pristine.issuer_trust.status.value,
                "downstream_execution": pristine.downstream_execution.status.value,
            }
        )
    return checks


async def run_sequence(
    env: LabEnvironment,
    commands: Sequence[Command],
    *,
    seed: int,
    sequence_seed: int,
    sequence_index: int,
    label: str,
    signature_check_limit: int = 3,
) -> Observation:
    """Build a fresh target, apply every command to it, and observe the result.

    Each call gets its own effect ledger file, its own fault injector and its
    own wallet, so nothing a previous sequence did can show up in this one's
    numbers. That isolation is what makes a shrink re-run comparable to the
    original.
    """
    credits_per_call = Decimal(env.credits_per_call)
    error: str | None = None
    # The environment's own tokens are bare `secrets.token_urlsafe` strings:
    # no prefix for a regex to find and no field name of their own once they
    # have been quoted into an exception message. Registering them by value
    # is the only thing that removes them from free text.
    register_secret(env.admin_api_key)
    register_secret(env.downstream_bearer_token)
    register_secret(env.control_token)
    async with configured_target(
        env, EXPLORED_CONFIGURATION, ledger_suffix=f"-{label}"
    ) as target:
        if target.tenant is not None:
            register_secret(target.tenant.api_key)
        runner = _SequenceRunner(
            target,
            seed=seed,
            sequence_seed=sequence_seed,
            sequence_index=sequence_index,
            credits_per_call=credits_per_call,
            call_timeout_seconds=env.call_timeout_seconds,
        )
        for index, command in enumerate(commands):
            await runner.apply(index, command)
        try:
            snapshot = await runner.gateway.snapshot(runner.tenant)
            gateway_view: dict[str, Any] | None = snapshot.as_dict()
            checks = await _collect_signature_checks(
                runner.gateway,
                runner.tenant,
                snapshot.receipts,
                limit=signature_check_limit,
            )
        except Exception as exc:  # noqa: BLE001 - report, do not abandon the run
            gateway_view, checks = None, []
            error = f"observation failed: {type(exc).__name__}: {exc}"
        return Observation(
            seed=seed,
            sequence_seed=sequence_seed,
            sequence_index=sequence_index,
            configuration=EXPLORED_CONFIGURATION.value,
            credits_per_call=str(credits_per_call),
            commands=tuple(runner.outcomes),
            states={name: state.value for name, state in runner.states.items()},
            accepted_keys={k: list(v) for k, v in runner.accepted_keys.items()},
            presented_keys={k: list(v) for k, v in runner.presented_keys.items()},
            permits={k: dict(v) for k, v in runner.permits.items()},
            attempts=tuple(attempt.as_dict() for attempt in runner.attempts),
            effects=tuple(row.as_dict() for row in target.ledger.effects()),
            crossings=tuple(row.as_dict() for row in target.injector.crossings()),
            gateway=gateway_view,
            signature_checks=tuple(checks),
            reconcile_checkpoints=tuple(runner.reconcile_checkpoints),
            error=error,
        )


def first_violation(
    observation: Observation, invariants: Sequence[Invariant]
) -> Violation | None:
    for invariant in invariants:
        violation = invariant.check(observation)
        if violation is not None:
            return violation
    return None


# --------------------------------------------------------------------------- #
# Shrinking                                                                     #
# --------------------------------------------------------------------------- #


@dataclass
class _ShrinkReport:
    sequence: tuple[Command, ...]
    attempts: int
    reproduced: bool
    observation: Observation | None


async def _shrink(
    env: LabEnvironment,
    commands: Sequence[Command],
    *,
    invariant_name: str,
    invariants: Sequence[Invariant],
    seed: int,
    sequence_seed: int,
    sequence_index: int,
    budget: int,
) -> _ShrinkReport:
    """Delta-debug toward the shortest sequence that still trips the invariant.

    Chunks first, then single commands: dropping half a sequence, when it
    works, removes far more noise per re-run than dropping one command at a
    time. Every candidate is re-run against a fresh target, and a candidate is
    only kept when it trips the *same* invariant -- a shorter sequence that
    trips a different one is a different finding, not a smaller version of
    this one.
    """
    current = tuple(commands)
    best_observation: Observation | None = None
    attempts = 0
    reproduced = False

    async def trips(candidate: tuple[Command, ...]) -> Observation | None:
        nonlocal attempts
        attempts += 1
        observation = await run_sequence(
            env,
            candidate,
            seed=seed,
            sequence_seed=sequence_seed,
            sequence_index=sequence_index,
            label=f"shrink{sequence_index}-{attempts}",
        )
        violation = first_violation(observation, invariants)
        if violation is not None and violation.invariant == invariant_name:
            return observation
        return None

    granularity = 2
    while len(current) > 1 and attempts < budget:
        chunk = max(1, len(current) // granularity)
        shrank = False
        for start in range(0, len(current), chunk):
            if attempts >= budget:
                break
            candidate = current[:start] + current[start + chunk :]
            if not candidate or len(candidate) == len(current):
                continue
            observation = await trips(candidate)
            if observation is not None:
                current = candidate
                best_observation = observation
                reproduced = True
                shrank = True
                break
        if shrank:
            granularity = 2
            continue
        if chunk == 1:
            break
        granularity = min(len(current), granularity * 2)

    # Final pass: one command at a time, until nothing more can go.
    dropping = True
    while dropping and len(current) > 1 and attempts < budget:
        dropping = False
        for index in range(len(current)):
            if attempts >= budget:
                break
            candidate = current[:index] + current[index + 1 :]
            observation = await trips(candidate)
            if observation is not None:
                current = candidate
                best_observation = observation
                reproduced = True
                dropping = True
                break

    return _ShrinkReport(
        sequence=current,
        attempts=attempts,
        reproduced=reproduced,
        observation=best_observation,
    )


# --------------------------------------------------------------------------- #
# Result                                                                        #
# --------------------------------------------------------------------------- #

LIMITATIONS = (
    "Finding no violation means this seed, at this length, found none. It is "
    "not a proof: the command distribution, the sequence length and the "
    "number of sequences all bound what could have been reached.",
    "Process death is simulated by tearing the request handler down at an "
    "instrumented durable boundary, not by killing an OS process. Committed "
    "state stays committed and uncommitted state rolls back, which is the "
    "footprint a SIGKILL leaves, but it is not a SIGKILL.",
    "Reconciliation is invoked with the reconciler's own idle window set to "
    "zero rather than waited out. Attempt rows are deliberately not "
    "backdated: ageing them writes a dispatched_at onto attempts that never "
    "dispatched, a row shape the product cannot produce and whose "
    "unreconcilability is a fact about the harness, not the gateway.",
    "A same-key retry issued while the first attempt's idempotency record is "
    "still in progress is answered by a poll-and-wait inside the gateway. "
    "The harness gives up on it after the gateway's upstream timeout plus "
    "two seconds and records a client timeout, rather than waiting out the "
    "gateway's full wait window.",
    "Concurrent bursts run without crash injection: a class-level crash hook "
    "with several calls in flight fires on whichever arrives first, and the "
    "harness could not afterwards say which.",
    "Debit, refund and receipt counts are read from the gateway's own tables. "
    "Findings that rest on them carry source 'gateway-reported'; only the "
    "effect ledger and the fault layer are independent of the system under "
    "test.",
    "Redaction of the emitted documents is by field name, by credential and "
    "wallet pattern, and by the literal value of the secrets this run minted. "
    "A secret that matches no pattern, carries no telltale field name and was "
    "never registered would survive; the documents are built from ids and "
    "hashes rather than payloads so that there is nothing of that kind in "
    "them to begin with.",
)


@dataclass(frozen=True)
class StatefulResult:
    seed: int
    configuration: str
    sequences_run: int
    commands_executed: int
    commands_skipped: int
    violations: tuple[Violation, ...]
    wall_clock_seconds: float
    shrink_attempts_spent: int
    shrink_budget: int
    max_length: int
    invariants: tuple[dict[str, str], ...]
    #: Commands that were legal and that the harness then failed to carry
    #: out. Counted apart from the inapplicable ones, because "the model
    #: proposed a command the real system had no room for" and "the harness
    #: broke" are different facts and only one of them is about the product.
    commands_failed: int = 0
    errors: tuple[str, ...] = ()
    limitations: tuple[str, ...] = LIMITATIONS

    @property
    def clean(self) -> bool:
        """No invariant contradicted, and nothing stopped the run observing.

        Errors count against cleanliness on purpose. An invariant that reads
        the gateway's tables cannot fire when the snapshot failed: with no
        receipts to compare against, ``RECEIPTS_DO_NOT_OUTRUN_EVIDENCE``
        compares zero with zero and says nothing was contradicted. That is
        absence of evidence wearing the costume of evidence of absence, and
        it is the failure mode this tool exists to avoid, so a run that lost
        an observation is not clean however few violations it reported.

        A run in which no command applied is not clean either, for the same
        reason: zero violations out of zero work is arithmetic, not evidence.
        """
        return bool(self.commands_executed) and not self.violations and not self.errors

    def as_dict(self) -> dict[str, Any]:
        return redact(
            {
                "seed": self.seed,
                "configuration": self.configuration,
                "sequences_run": self.sequences_run,
                "commands_executed": self.commands_executed,
                "commands_skipped": self.commands_skipped,
                "commands_failed": self.commands_failed,
                "clean": self.clean,
                "violations": [violation.as_dict() for violation in self.violations],
                "violation_count": len(self.violations),
                "wall_clock_seconds": round(self.wall_clock_seconds, 3),
                "shrink_attempts_spent": self.shrink_attempts_spent,
                "shrink_budget": self.shrink_budget,
                "max_length": self.max_length,
                "invariants": [dict(item) for item in self.invariants],
                "errors": list(self.errors),
                "limitations": list(self.limitations),
                "conclusion": self.conclusion(),
            }
        )

    def conclusion(self) -> str:
        """What this run is entitled to say. Never more than that.

        Four readings, in decreasing order of what they establish: violations
        found; nothing found but the run could not observe itself; nothing
        found because nothing ran; nothing found across work that did happen.
        Only the last one is evidence, and it says how far it reaches.
        """
        damage = ""
        if self.errors:
            failed = ""
            if self.commands_failed:
                failed = (
                    f", including {self.commands_failed} command(s) the harness "
                    "could not carry out"
                )
            damage = (
                f" {len(self.errors)} error(s) occurred during the run{failed}; an "
                "invariant that reads the gateway's tables cannot fire on an "
                "observation that failed, so the checks covered less than the "
                "command counts suggest."
            )
        if self.violations:
            names = sorted({violation.invariant for violation in self.violations})
            return (
                f"{len(self.violations)} violation(s) across {self.sequences_run} "
                f"sequence(s) at seed {self.seed}: {', '.join(names)}.{damage}"
            )
        if self.errors:
            return (
                f"No invariant was contradicted across {self.sequences_run} "
                f"sequence(s) at seed {self.seed}, but this run is not evidence "
                f"that none would have been.{damage}"
            )
        if self.sequences_run <= 0 or self.commands_executed <= 0:
            return (
                f"Nothing was exercised at seed {self.seed}: {self.sequences_run} "
                f"sequence(s) ran and {self.commands_executed} command(s) applied. "
                "No invariant could have been contradicted, and none was. This "
                "result says nothing about the gateway."
            )
        return (
            f"No invariant was contradicted by {self.sequences_run} sequence(s) "
            f"({self.commands_executed} commands executed, {self.commands_skipped} "
            f"skipped as inapplicable) at seed {self.seed}. This is evidence about "
            "this seed at this length, not a proof of correctness."
        )

    def summary(self) -> dict[str, Any]:
        return redact(
            {
                "seed": self.seed,
                "configuration": self.configuration,
                "sequences_run": self.sequences_run,
                "commands_executed": self.commands_executed,
                "commands_skipped": self.commands_skipped,
                "commands_failed": self.commands_failed,
                "clean": self.clean,
                "violation_count": len(self.violations),
                "violations": [
                    {
                        "invariant": violation.invariant,
                        "detail": violation.detail,
                        "source": violation.source,
                        "sequence_seed": violation.sequence_seed,
                        "sequence_length": len(violation.sequence),
                        "minimized_length": len(violation.minimized_sequence),
                        "minimized": violation.minimized,
                        "shrink_attempts": violation.shrink_attempts,
                        "minimized_sequence": [
                            Command.from_dict(c).render()
                            for c in violation.minimized_sequence
                        ],
                    }
                    for violation in self.violations
                ],
                "wall_clock_seconds": round(self.wall_clock_seconds, 3),
                "shrink_attempts_spent": self.shrink_attempts_spent,
                "errors": list(self.errors),
                "conclusion": self.conclusion(),
            }
        )


# --------------------------------------------------------------------------- #
# Exploration                                                                   #
# --------------------------------------------------------------------------- #


async def explore(
    env: LabEnvironment,
    *,
    seed: int,
    sequences: int = DEFAULT_SEQUENCES,
    max_length: int = DEFAULT_MAX_LENGTH,
    shrink_budget: int = DEFAULT_SHRINK_BUDGET,
    invariants: Sequence[Invariant] | None = None,
) -> StatefulResult:
    """Generate, run, check, and minimize. The seed is the whole contract.

    ``sequences`` sequences are drawn from ``random.Random(seed)``, each run
    against its own freshly-built target. The first invariant a sequence
    contradicts is minimized and reported; later invariants on the same
    sequence are not pursued, because a shrunken reproduction of the first is
    worth more than a list of correlated symptoms.
    """
    checks = tuple(invariants) if invariants is not None else default_invariants()
    master = random.Random(seed)
    started = time.perf_counter()
    violations: list[Violation] = []
    errors: list[str] = []
    executed = 0
    skipped = 0
    failed = 0
    shrink_spent = 0

    for index in range(sequences):
        sequence_seed = master.getrandbits(48)
        commands = generate_sequence(random.Random(sequence_seed), max_length)
        observation = await run_sequence(
            env,
            commands,
            seed=seed,
            sequence_seed=sequence_seed,
            sequence_index=index,
            label=f"seq{index}",
        )
        executed += sum(1 for item in observation.commands if item.applied)
        # A command the model proposed and the system had no room for is
        # inapplicable. A command that raised is a harness failure wearing
        # the same `applied: false`. Counting them together would let a run
        # in which nothing worked report itself as a run in which nothing
        # was applicable.
        skipped += sum(
            1
            for item in observation.commands
            if not item.applied and "error" not in item.detail
        )
        failed += sum(
            1
            for item in observation.commands
            if not item.applied and "error" in item.detail
        )
        if observation.error:
            errors.append(f"sequence {index}: {observation.error}")
        errors.extend(
            f"sequence {index}, command {item.index} "
            f"({item.command['kind']}): {item.detail['error']}"
            for item in observation.commands
            if "error" in item.detail
        )
        errors.extend(
            f"sequence {index}: {message}"
            for message in observation.signature_check_errors()
        )
        violation = first_violation(observation, checks)
        if violation is None:
            continue
        report = await _shrink(
            env,
            commands,
            invariant_name=violation.invariant,
            invariants=checks,
            seed=seed,
            sequence_seed=sequence_seed,
            sequence_index=index,
            budget=shrink_budget,
        )
        shrink_spent += report.attempts
        final = report.observation if report.observation is not None else observation
        violations.append(
            replace(
                violation,
                seed=seed,
                sequence_seed=sequence_seed,
                sequence_index=index,
                sequence=tuple(command.as_dict() for command in commands),
                minimized_sequence=tuple(
                    command.as_dict() for command in report.sequence
                ),
                minimized=report.reproduced,
                shrink_attempts=report.attempts,
                observation_of=(
                    "minimized" if report.observation is not None else "original"
                ),
                observation=redact(final.as_dict()),
            )
        )

    return StatefulResult(
        seed=seed,
        configuration=EXPLORED_CONFIGURATION.value,
        sequences_run=sequences,
        commands_executed=executed,
        commands_skipped=skipped,
        violations=tuple(violations),
        wall_clock_seconds=time.perf_counter() - started,
        shrink_attempts_spent=shrink_spent,
        shrink_budget=shrink_budget,
        max_length=max_length,
        invariants=tuple(invariant.describe() for invariant in checks),
        commands_failed=failed,
        errors=tuple(errors),
    )


# --------------------------------------------------------------------------- #
# Standalone entry point                                                        #
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m failure_lab.stateful",
        description=(
            "Property-based, stateful exploration of the governed-call "
            "lifecycle. Reproducible from --seed alone."
        ),
        epilog=(
            "Exit 0 means the run completed and no invariant was "
            "contradicted. Exit 1 means a violation was found, or the run "
            "hit an error and so cannot claim to have checked anything. "
            "Exit 2 is a bad invocation."
        ),
    )
    parser.add_argument("--seed", type=int, default=0, help="Master seed (default: 0).")
    parser.add_argument(
        "--sequences",
        type=int,
        default=DEFAULT_SEQUENCES,
        help=f"Command sequences to run, at least 1 (default: {DEFAULT_SEQUENCES}).",
    )
    parser.add_argument(
        "--max-length",
        type=int,
        default=DEFAULT_MAX_LENGTH,
        help=f"Longest generated sequence (default: {DEFAULT_MAX_LENGTH}).",
    )
    parser.add_argument(
        "--shrink-budget",
        type=int,
        default=DEFAULT_SHRINK_BUDGET,
        help=f"Re-runs one violation may spend shrinking (default: {DEFAULT_SHRINK_BUDGET}).",
    )
    parser.add_argument(
        "--call-timeout",
        type=float,
        default=1.0,
        help="Gateway upstream call timeout in seconds (default: 1.0).",
    )
    parser.add_argument(
        "--full", action="store_true", help="Print the whole result document."
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Keep the run directory instead of deleting it.",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Leave application logging switched on."
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace, run_dir: Path) -> StatefulResult:
    from failure_lab.gateway import boot_standalone_environment

    admin_key = boot_standalone_environment(run_dir)
    register_secret(admin_key)

    from app.db.database import close_db, init_db
    from app.main import app

    # init_db is inside the try: a half-built engine still has to be closed,
    # and a boot that fails part-way is exactly when cleanup matters.
    try:
        await init_db()
        env = LabEnvironment(
            run_dir=run_dir,
            app=app,
            admin_api_key=admin_key,
            call_timeout_seconds=args.call_timeout,
        )
        return await explore(
            env,
            seed=args.seed,
            sequences=args.sequences,
            max_length=args.max_length,
            shrink_budget=args.shrink_budget,
        )
    finally:
        await close_db()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.sequences < 1:
        # A zero-sequence run would otherwise print a fully-formed clean
        # document and exit 0 without touching the gateway.
        print("--sequences must be at least 1", file=sys.stderr)
        return 2
    if not args.verbose:
        # The gateway, the MCP SDK and httpx all narrate at INFO. The result
        # document is the output; the narration would bury it.
        logging.disable(logging.CRITICAL - 1)
    run_dir = Path(tempfile.mkdtemp(prefix=f"failure-lab-stateful-{args.seed}-"))
    try:
        # A few startup lines are written straight to stdout rather than
        # through logging. Stdout is the result document; narration goes to
        # stderr so the document stays machine-readable.
        with contextlib.redirect_stdout(sys.stderr):
            result = asyncio.run(_run(args, run_dir))
    finally:
        if args.keep:
            print(f"run directory kept at {run_dir}", file=sys.stderr)
        else:
            shutil.rmtree(run_dir, ignore_errors=True)
    document = result.as_dict() if args.full else result.summary()
    print(json.dumps(document, indent=2, sort_keys=True, default=str))
    # Exit 0 means the run completed and contradicted nothing. A run that
    # lost an observation reports no violations for the same reason a
    # switched-off smoke detector reports no smoke, so it does not get to
    # exit 0 either.
    return 0 if result.clean else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_MAX_LENGTH",
    "DEFAULT_SEQUENCES",
    "DEFAULT_SHRINK_BUDGET",
    "EXPLORED_CONFIGURATION",
    "LIMITATIONS",
    "AtMostOneDispatchPerKey",
    "BudgetNotExceeded",
    "Command",
    "CommandKind",
    "CommandOutcome",
    "EveryDebitAccountedFor",
    "ExecutionsBoundedByAcceptedKeys",
    "Invariant",
    "Observation",
    "OperationState",
    "RejectionHasNoEffect",
    "ReceiptsDoNotOutrunEvidence",
    "SignedReceiptsResistMutation",
    "StatefulResult",
    "Violation",
    "default_invariants",
    "explore",
    "first_violation",
    "generate_sequence",
    "main",
    "redact",
    "register_secret",
    "run_sequence",
]
