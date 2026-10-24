"""Replay recorded gateway calls against a new policy bundle, offline.

``amw-replay --bundle <bundle.json> --calls <recorded receipts>`` re-runs the
gateway's own policy decision core (:func:`evaluate_policy_models`) for every
recorded call and writes one signed replay receipt per call.

Guarantees:

* Nothing is executed or forwarded. Recorded calls are read from disk, the
  decision core is pure, and the whole replay runs with outbound sockets
  sealed (:func:`network_sealed`), so a regression that reaches for the
  network fails loudly instead of silently dispatching.
* Recorded calls are the gateway's own signed receipts (portable bundles or
  the receipt signing goldens): each record must carry the exact
  ``signing_input`` the gateway signed. Hand-shaped call dicts are rejected,
  and ``recorded_keys`` verifies each recording's signature before replay.
* Replay receipts are signed with the configured trust signing key
  (``TRUST_SIGNING_PRIVATE_KEY_B64`` / ``TRUST_SIGNING_KEY_ID``) through
  :class:`SigningKeyService`, in the portable bundle shape that
  ``b2a_sdk.receipt_verifier.verify_bundle`` checks.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
import contextlib
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import socket
from typing import Any

from app.db.models import PolicyBundleModel
from app.services.policies import evaluate_policy_models
from app.services.signing_keys import (
    RECEIPT_CANONICALIZATION,
    SigningKeyService,
    canonical_json,
    get_signing_key_service,
    sha256_hex,
)

REPLAY_RECEIPT_TYPE = "amw.policy_replay/1"

ALLOW = "allow"
DENY = "deny"

#: Reason codes :func:`evaluate_policy_models` can return. A recorded denial
#: with any other reason (a permit budget, a revoked permit, ...) was not a
#: policy-bundle decision, so a new bundle cannot turn it into an allow.
POLICY_REASON_CODES = frozenset(
    {
        "policy_cost_invalid",
        "policy_constraint_corrupt",
        "human_approval_required",
        "tool_not_allowed",
        "service_category_not_allowed",
        "max_cost_per_action_exceeded",
        "daily_spend_unknown",
        "daily_spend_limit_exceeded",
        "real_effects_required",
        "risk_tier_not_allowed",
    }
)

_BUNDLE_FIELDS = (
    "policy_id",
    "wallet_id",
    "name",
    "allowed_tools",
    "allowed_service_categories",
    "max_cost_per_action",
    "daily_spend_limit",
    "require_real_effects",
    "risk_tier",
    "human_approval_required",
    "is_active",
)
_REQUIRED_RECORD_FIELDS = ("receipt_id", "tool", "wallet_id", "outcome")


class ReplayError(ValueError):
    """Input that cannot be replayed safely."""


class NetworkSealedError(RuntimeError):
    """Raised when anything tries to open a connection during a replay."""


@dataclass(frozen=True)
class RecordedCall:
    source: str
    signing_input: str
    signature: str | None
    payload: dict[str, Any]

    @property
    def call_hash(self) -> str:
        return hashlib.sha256(self.signing_input.encode()).hexdigest()

    @property
    def old_decision(self) -> str:
        return DENY if self.payload["outcome"] == "denied" else ALLOW

    @property
    def old_reason(self) -> str | None:
        return self.payload.get("reason_code")


@dataclass(frozen=True)
class PolicyBundle:
    document: dict[str, Any]
    models: tuple[PolicyBundleModel, ...]

    @property
    def bundle_hash(self) -> str:
        return sha256_hex(self.document)


@dataclass
class ReplayResult:
    receipts: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    public_key: dict[str, Any] = field(default_factory=dict)


@contextlib.contextmanager
def network_sealed() -> Iterator[None]:
    """Refuse every outbound connection and DNS lookup for the block."""

    def _refuse(*_: Any, **__: Any) -> Any:
        raise NetworkSealedError("amw-replay never opens network connections")

    saved = (
        socket.socket.connect,
        socket.socket.connect_ex,
        socket.create_connection,
        socket.getaddrinfo,
    )
    socket.socket.connect = _refuse  # type: ignore[method-assign]
    socket.socket.connect_ex = _refuse  # type: ignore[method-assign]
    socket.create_connection = _refuse  # type: ignore[assignment]
    socket.getaddrinfo = _refuse  # type: ignore[assignment]
    try:
        yield
    finally:
        (
            socket.socket.connect,  # type: ignore[method-assign]
            socket.socket.connect_ex,  # type: ignore[method-assign]
            socket.create_connection,
            socket.getaddrinfo,
        ) = saved


def _iter_records(path: Path) -> Iterator[tuple[str, Any]]:
    if path.is_dir():
        for child in sorted(path.glob("*.json")):
            yield from _iter_records(child)
        return
    text = path.read_text(encoding="utf-8")
    if path.suffix in {".jsonl", ".ndjson"}:
        for lineno, line in enumerate(text.splitlines(), start=1):
            if line.strip():
                yield f"{path.name}:{lineno}", json.loads(line)
        return
    document = json.loads(text)
    if isinstance(document, dict) and isinstance(document.get("receipts"), list):
        document = document["receipts"]
    if isinstance(document, list):
        for index, item in enumerate(document):
            yield f"{path.name}[{index}]", item
        return
    yield path.name, document


def _record_to_call(source: str, record: Any) -> RecordedCall:
    if not isinstance(record, dict):
        raise ReplayError(f"{source}: recorded call must be a JSON object")
    signing_input = record.get("signing_input")
    if not isinstance(signing_input, str) or not signing_input:
        raise ReplayError(
            f"{source}: not a gateway receipt (no signing_input); replay only "
            "accepts calls the gateway recorded and signed"
        )
    try:
        payload = json.loads(signing_input)
    except json.JSONDecodeError as exc:
        raise ReplayError(f"{source}: signing_input is not JSON") from exc
    if not isinstance(payload, dict):
        raise ReplayError(f"{source}: signing_input must be a JSON object")
    missing = [name for name in _REQUIRED_RECORD_FIELDS if not payload.get(name)]
    if missing:
        raise ReplayError(f"{source}: recorded receipt lacks {', '.join(missing)}")
    if canonical_json(payload) != signing_input:
        raise ReplayError(f"{source}: signing_input is not canonical gateway JSON")
    signature = record.get("signature") or record.get("signature_b64_over_test_seed")
    return RecordedCall(source, signing_input, signature, payload)


def _verify_recorded(call: RecordedCall, recorded_keys: dict[str, Any]) -> None:
    from b2a_sdk.receipt_verifier import verify_bundle

    if not call.signature:
        raise ReplayError(f"{call.source}: recorded receipt has no signature")
    result = verify_bundle(
        {
            "receipt_id": call.payload["receipt_id"],
            "kid": call.payload.get("kid"),
            "alg": call.payload.get("alg", "Ed25519"),
            "canonicalization": RECEIPT_CANONICALIZATION,
            "signing_input": call.signing_input,
            "signature": call.signature,
        },
        recorded_keys,
    )
    if not result.ok:
        raise ReplayError(
            f"{call.source}: recorded receipt signature {result.status.value}"
        )


def load_recorded_calls(
    path: str | Path,
    *,
    recorded_keys: dict[str, Any] | None = None,
) -> list[RecordedCall]:
    """Load gateway-recorded calls; verify their signatures when keys given."""
    calls = [_record_to_call(src, rec) for src, rec in _iter_records(Path(path))]
    if not calls:
        raise ReplayError(f"{path}: no recorded calls found")
    if recorded_keys is not None:
        for call in calls:
            _verify_recorded(call, recorded_keys)
    return calls


def _decimal(value: Any, *, field_name: str) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ReplayError(f"bundle {field_name} is not a number") from exc


def load_policy_bundle(path: str | Path) -> PolicyBundle:
    """Load a policy bundle document: one policy or ``{"policies": [...]}``.

    Each policy uses the ``/v1/policies`` field names. ``wallet_id`` is
    optional; when set, the policy only applies to calls from that wallet,
    the same scoping :func:`evaluate_wallet_policy` applies from the DB.
    """
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ReplayError("bundle must be a JSON object")
    policies = document.get("policies", [document])
    if not isinstance(policies, list) or not policies:
        raise ReplayError("bundle has no policies")
    models: list[PolicyBundleModel] = []
    for index, policy in enumerate(policies):
        if not isinstance(policy, dict):
            raise ReplayError(f"bundle policy {index} must be an object")
        unknown = sorted(set(policy) - set(_BUNDLE_FIELDS))
        if unknown:
            raise ReplayError(f"bundle policy {index} has unknown fields {unknown}")
        for list_field in ("allowed_tools", "allowed_service_categories"):
            value = policy.get(list_field)
            if value is not None and not (
                isinstance(value, list) and all(isinstance(v, str) for v in value)
            ):
                raise ReplayError(f"bundle {list_field} must be a list of strings")
        model = PolicyBundleModel(
            policy_id=policy.get("policy_id") or f"replay-policy-{index}",
            wallet_id=policy.get("wallet_id") or "",
            name=policy.get("name") or f"replay policy {index}",
            allowed_tools_json=(
                json.dumps(policy["allowed_tools"])
                if policy.get("allowed_tools") is not None
                else None
            ),
            allowed_service_categories_json=(
                json.dumps(policy["allowed_service_categories"])
                if policy.get("allowed_service_categories") is not None
                else None
            ),
            max_cost_per_action=_decimal(
                policy.get("max_cost_per_action"), field_name="max_cost_per_action"
            ),
            daily_spend_limit=_decimal(
                policy.get("daily_spend_limit"), field_name="daily_spend_limit"
            ),
            require_real_effects=bool(policy.get("require_real_effects", False)),
            risk_tier=policy.get("risk_tier", "medium"),
            human_approval_required=bool(policy.get("human_approval_required", False)),
            is_active=bool(policy.get("is_active", True)),
        )
        models.append(model)
    return PolicyBundle(document=document, models=tuple(models))


def _decide(call: RecordedCall, bundle: PolicyBundle) -> tuple[str, str, str | None]:
    if call.old_decision == DENY and call.old_reason not in POLICY_REASON_CODES:
        # Denied before or outside policy evaluation; a bundle cannot undo it.
        return DENY, f"recorded_denial:{call.old_reason or 'unknown'}", None
    wallet_id = call.payload["wallet_id"]
    models = [
        m
        for m in bundle.models
        if m.is_active and (not m.wallet_id or m.wallet_id == wallet_id)
    ]
    evaluation = evaluate_policy_models(
        models,
        tool_name=call.payload["tool"],
        service_category=call.payload.get("service_category"),
        estimated_cost=_decimal(
            call.payload.get("credits_authorized"), field_name="credits_authorized"
        ),
        approval_gate_active=bool(call.payload.get("approval_id")),
    )
    return (
        ALLOW if evaluation.allowed else DENY,
        evaluation.reason,
        evaluation.policy_id,
    )


def _change(old: str, new: str) -> str:
    if old == new:
        return "unchanged"
    return "allow_to_deny" if old == ALLOW else "deny_to_allow"


def summarize(receipts: Sequence[dict[str, Any]], bundle_hash: str) -> dict[str, Any]:
    changes = [json.loads(r["signing_input"])["change"] for r in receipts]
    return {
        "receipt_type": REPLAY_RECEIPT_TYPE,
        "bundle_hash": bundle_hash,
        "total": len(receipts),
        "allow_to_deny": changes.count("allow_to_deny"),
        "deny_to_allow": changes.count("deny_to_allow"),
        "unchanged": changes.count("unchanged"),
    }


def replay_calls(
    calls: Sequence[RecordedCall],
    bundle: PolicyBundle,
    *,
    signer: SigningKeyService | None = None,
) -> ReplayResult:
    """Re-decide each call under ``bundle``; one signed receipt per call."""
    signer = signer or get_signing_key_service()
    key_id, public_key_b64 = signer.offline_key_identity()
    bundle_hash = bundle.bundle_hash
    result = ReplayResult(
        public_key={
            "kid": key_id,
            "alg": "Ed25519",
            "status": "active",
            "public_key_b64": public_key_b64,
        }
    )
    with network_sealed():
        for index, call in enumerate(calls):
            new_decision, new_reason, policy_id = _decide(call, bundle)
            replay_id = (
                f"rpl_{sha256_hex(f'{bundle_hash}:{call.call_hash}:{index}')[:24]}"
            )
            payload: dict[str, Any] = {
                "receipt_type": REPLAY_RECEIPT_TYPE,
                "receipt_id": replay_id,
                "call_index": index,
                "call_hash": call.call_hash,
                "recorded_receipt_id": call.payload["receipt_id"],
                "tool": call.payload["tool"],
                "wallet_id": call.payload["wallet_id"],
                "old_decision": call.old_decision,
                "old_reason": call.old_reason,
                "new_decision": new_decision,
                "new_reason": new_reason,
                "new_policy_id": policy_id,
                "change": _change(call.old_decision, new_decision),
                "bundle_hash": bundle_hash,
                "alg": "Ed25519",
                "kid": key_id,
            }
            payload["payload_hash"] = sha256_hex(payload)
            signature, kid, _ = signer.sign_payload_with_key_id(payload, key_id)
            result.receipts.append(
                {
                    "receipt_id": replay_id,
                    "kid": kid,
                    "alg": "Ed25519",
                    "canonicalization": RECEIPT_CANONICALIZATION,
                    "signing_input": canonical_json(payload),
                    "signature": signature,
                }
            )
    result.summary = summarize(result.receipts, bundle_hash)
    result.summary["receipts_sha256"] = sha256_hex(
        "\n".join(r["signing_input"] for r in result.receipts)
    )
    return result
