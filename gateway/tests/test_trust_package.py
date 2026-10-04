"""The app.trust facade must re-export the canonical implementations.

If these identities ever drift, the product-core boundary has silently forked
from the underlying services — which is exactly what the facade exists to
prevent.
"""

from __future__ import annotations

import inspect
from decimal import Decimal
from typing import get_type_hints

import app.policy.decisions as decisions
import app.services.agent_money as agent_money
import app.services.audit_chain as audit_chain
import app.services.audit_log as audit_log
import app.services.governance as governance
import app.services.idempotency as idempotency
import app.services.permits as permits
import app.services.policies as policies
import app.services.receipts as receipts
import app.services.signing_keys as signing_keys
import app.trust as trust
from app.services.billing_engine import BillingEngine
from app.services.wallet_engine import WalletEngine


AGENT_MONEY_DELEGATES = {
    "create_sponsor_wallet": WalletEngine,
    "create_agent_wallet": WalletEngine,
    "create_child_wallet": WalletEngine,
    "reclaim_child_wallet": WalletEngine,
    "transfer": WalletEngine,
    "get_swarm_budget": WalletEngine,
    "get_wallet": WalletEngine,
    "get_daily_spend": WalletEngine,
    "list_wallets": WalletEngine,
    "charge": BillingEngine,
    "refund_charge": BillingEngine,
    "top_up": BillingEngine,
    "get_arbitrage_report": BillingEngine,
    "get_pricing_table": BillingEngine,
    "get_alerts": BillingEngine,
    "get_ledger": BillingEngine,
    "register_service": BillingEngine,
    "list_services": BillingEngine,
}


def _normalized_signature(method):
    signature = inspect.signature(method)
    hints = get_type_hints(method)
    parameters = tuple(
        (
            parameter.name,
            parameter.kind,
            parameter.default,
            hints.get(parameter.name, parameter.annotation),
        )
        for parameter in signature.parameters.values()
    )
    return parameters, hints.get("return", signature.return_annotation)


def test_permits_facade_matches_service():
    assert trust.PermitService is permits.PermitService
    assert trust.get_permit_service is permits.get_permit_service
    assert trust.PermitError is permits.PermitError
    assert trust.PermitValidation is permits.PermitValidation
    assert trust.permit_constraints_snapshot is permits.permit_constraints_snapshot


def test_receipts_facade_matches_service():
    assert trust.ReceiptService is receipts.ReceiptService
    assert trust.get_receipt_service is receipts.get_receipt_service
    assert trust.ReceiptError is receipts.ReceiptError


def test_idempotency_facade_matches_service():
    assert trust.IdempotencyService is idempotency.IdempotencyService
    assert trust.get_idempotency_service is idempotency.get_idempotency_service
    assert trust.IdempotencyConflictError is idempotency.IdempotencyConflictError
    assert trust.IdempotencyInProgressError is idempotency.IdempotencyInProgressError
    assert trust.IdempotencyReplay is idempotency.IdempotencyReplay


def test_audit_facade_matches_service():
    assert trust.verify_audit_chain is audit_chain.verify_audit_chain
    assert trust.sign_audit_model is audit_chain.sign_audit_model
    assert trust.audit_payload is audit_chain.audit_payload
    assert trust.AuditChainVerification is audit_chain.AuditChainVerification
    assert trust.record_audit_event is audit_log.record_audit_event


def test_policy_facade_matches_service():
    assert trust.evaluate_tool_invocation is decisions.evaluate_tool_invocation
    assert trust.evaluate_governed_action is decisions.evaluate_governed_action
    assert trust.PolicyDecision is decisions.PolicyDecision
    assert trust.evaluate_wallet_policy is policies.evaluate_wallet_policy
    assert trust.PolicyEvaluation is policies.PolicyEvaluation
    assert trust.record_governed_action is governance.record_governed_action


def test_metering_facade_matches_service():
    assert trust.AgentMoney is agent_money.AgentMoney
    assert trust.get_agent_money is agent_money.get_agent_money
    assert trust.InsufficientFundsError is agent_money.InsufficientFundsError
    assert trust.WalletNotFoundError is agent_money.WalletNotFoundError
    assert (
        trust.KYCVerificationRequiredError is agent_money.KYCVerificationRequiredError
    )


def test_agent_money_delegates_preserve_public_method_contracts():
    """The compatibility facade owns stable FQNs and typed signatures."""
    assert str(inspect.signature(agent_money.AgentMoney)) == "()"
    getter_signature = inspect.signature(agent_money.get_agent_money)
    assert not getter_signature.parameters
    assert getter_signature.return_annotation is agent_money.AgentMoney

    for method_name, engine_type in AGENT_MONEY_DELEGATES.items():
        facade_method = agent_money.AgentMoney.__dict__[method_name]
        engine_method = getattr(engine_type, method_name)

        assert facade_method.__module__ == "app.services.agent_money"
        assert inspect.iscoroutinefunction(
            facade_method
        ) is inspect.iscoroutinefunction(engine_method)
        assert _normalized_signature(facade_method) == _normalized_signature(
            engine_method
        )
        assert all(
            not isinstance(annotation, str)
            for annotation in facade_method.__annotations__.values()
        )


def test_agent_money_singleton_and_exception_contracts():
    assert agent_money.get_agent_money() is agent_money.get_agent_money()

    insufficient = agent_money.InsufficientFundsError(
        "wallet-1",
        Decimal("2"),
        Decimal("5"),
    )
    assert insufficient.wallet_id == "wallet-1"
    assert insufficient.current_balance == Decimal("2")
    assert insufficient.required_amount == Decimal("5")
    assert insufficient.shortfall == Decimal("3")
    assert str(insufficient) == "Insufficient funds: 2 < 5"

    missing = agent_money.WalletNotFoundError("wallet-2")
    assert missing.wallet_id == "wallet-2"
    assert str(missing) == "Wallet not found: wallet-2"

    kyc = agent_money.KYCVerificationRequiredError("wallet-3", "pending")
    assert kyc.wallet_id == "wallet-3"
    assert kyc.kyc_status == "pending"
    assert str(kyc) == (
        "KYC verification required for wallet wallet-3. Current status: pending. "
        "Complete verification at /v1/kyc/sessions"
    )


def test_signing_facade_matches_service():
    assert trust.SigningKeyService is signing_keys.SigningKeyService
    assert trust.get_signing_key_service is signing_keys.get_signing_key_service
    assert trust.SigningKeyError is signing_keys.SigningKeyError
    assert trust.canonical_json is signing_keys.canonical_json
    assert trust.sha256_hex is signing_keys.sha256_hex


def test_adapter_seam_is_exposed():
    from app.trust.adapters import GovernedInvocationAdapter, McpGovernedAdapter

    assert trust.GovernedInvocationAdapter is GovernedInvocationAdapter
    assert trust.McpGovernedAdapter is McpGovernedAdapter
    assert issubclass(McpGovernedAdapter, GovernedInvocationAdapter)
    assert McpGovernedAdapter.protocol == "mcp"
