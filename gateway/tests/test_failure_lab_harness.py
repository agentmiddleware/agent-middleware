"""Harness-level checks for the failure lab: the measuring instruments themselves.

These pin the properties every scenario relies on: the effect ledger is
independent and duplicate-visible, the fault layer injects where it says and
counts what crosses it, and the four configurations drive the same workload
through the real gateway routers or straight at the downstream tool.
"""

from __future__ import annotations

import pytest

from failure_lab.configurations import (
    ALL_CONFIGURATIONS,
    Configuration,
    LabEnvironment,
    configured_target,
)
from failure_lab.effect_ledger import EffectLedger, NativeConflictError
from failure_lab.faults import FaultMode, FaultPlan
from failure_lab.identity import KeyPolicy, OperationIdentity, new_business_operation_id
from failure_lab.refund_tool import RefundRequest


def test_effect_ledger_is_duplicate_visible(tmp_path):
    ledger = EffectLedger(tmp_path / "effects.db")
    common = dict(
        request_id="req_1",
        idempotency_key="k",
        amount=5000,
        currency="USD",
        customer_id="cus",
        payment_id="pay",
        configuration="test",
    )
    first = ledger.execute(operation_id="refund:pay", **common)
    second = ledger.execute(operation_id="refund:pay", **common)
    assert first.record is not None and second.record is not None
    assert first.record.execution_ordinal == 1
    assert second.record.execution_ordinal == 2
    assert ledger.execution_count("refund:pay") == 2
    assert [row["execution_count"] for row in ledger.snapshot()] == [2, 2]


def test_effect_ledger_native_idempotency_is_atomic_and_conflict_aware(tmp_path):
    ledger = EffectLedger(tmp_path / "effects.db")
    common = dict(
        request_id="req_1",
        idempotency_key="k",
        amount=5000,
        currency="USD",
        customer_id="cus",
        payment_id="pay",
        configuration="test",
    )
    first = ledger.execute(
        operation_id="refund:pay", native_fingerprint="fp-a", **common
    )
    replay = ledger.execute(
        operation_id="refund:pay", native_fingerprint="fp-a", **common
    )
    assert first.replayed is False and replay.replayed is True
    assert replay.result == first.result
    assert ledger.execution_count("refund:pay") == 1
    with pytest.raises(NativeConflictError):
        ledger.execute(operation_id="refund:pay", native_fingerprint="fp-b", **common)
    assert ledger.execution_count("refund:pay") == 1


def test_identity_distinguishes_restart_policies():
    business = OperationIdentity.first_attempt(
        "refund:pay_1", key_policy=KeyPolicy.BUSINESS
    )
    attempt = OperationIdentity.first_attempt(
        "refund:pay_1", key_policy=KeyPolicy.ATTEMPT
    )
    assert business.retry().idempotency_key == business.idempotency_key
    assert business.after_restart().idempotency_key == business.idempotency_key
    assert attempt.retry().idempotency_key == attempt.idempotency_key
    restarted = attempt.after_restart()
    assert restarted.idempotency_key != attempt.idempotency_key
    assert restarted.business_operation_id == attempt.business_operation_id
    assert restarted.agent_generation == 2
    assert restarted.request_id != attempt.request_id


def test_refund_request_is_strict_about_representation():
    RefundRequest.from_mapping(
        {"operation_id": "o", "customer_id": "c", "payment_id": "p", "amount": 5000}
    )
    with pytest.raises(ValueError):
        RefundRequest.from_mapping(
            {
                "operation_id": "o",
                "customer_id": "c",
                "payment_id": "p",
                "amount": "5000",
            }
        )
    with pytest.raises(ValueError):
        RefundRequest.from_mapping(
            {
                "operation_id": "o",
                "customer_id": "c",
                "payment_id": "p",
                "amount": 5000.0,
            }
        )
    with pytest.raises(ValueError):
        RefundRequest.from_mapping(
            {
                "operation_id": "o",
                "customer_id": "c",
                "payment_id": "p",
                "amount": 50,
                "x": 1,
            }
        )


@pytest.mark.anyio
async def test_lost_response_workload_across_configurations(tmp_path, clean_database):
    """The headline workload drives every configuration through one interface."""
    from app.main import app

    env = LabEnvironment(run_dir=tmp_path, app=app, admin_api_key="test-key")
    observed: dict[Configuration, dict] = {}
    for configuration in ALL_CONFIGURATIONS:
        async with configured_target(env, configuration) as target:
            operation_id = new_business_operation_id("pay_lost")
            identity = OperationIdentity.first_attempt(operation_id)
            refund = RefundRequest(
                operation_id=operation_id,
                customer_id="cus_1",
                payment_id="pay_lost",
                amount=5000,
            )
            target.injector.arm(
                FaultPlan(
                    mode=FaultMode.RESPONSE_LOST_AFTER_EXECUTION,
                    operation_id=operation_id,
                    hold_seconds=20,
                )
            )
            first = await target.agent.submit(
                identity,
                refund,
                timeout_seconds=1.0 if not configuration.uses_gateway else 8.0,
            )
            retry = await target.agent.submit(
                identity.retry(), refund, timeout_seconds=8.0
            )
            snapshot = (
                await target.gateway.snapshot(target.tenant) if target.gateway else None
            )
            observed[configuration] = {
                "first": first,
                "retry": retry,
                "effects": target.ledger.execution_count(operation_id),
                "crossings": target.injector.dispatch_count(operation_id),
                "snapshot": snapshot,
            }

    naive = observed[Configuration.DIRECT_NAIVE]
    assert naive["first"].status == "timeout"
    assert naive["retry"].status == "succeeded"
    assert naive["effects"] == 2  # the duplicate the lab exists to show

    native = observed[Configuration.DIRECT_NATIVE]
    assert native["first"].status == "timeout"
    assert native["retry"].status == "replayed"
    assert native["effects"] == 1  # native controls handled it without a gateway

    for configuration in (Configuration.GATEWAY_NATIVE, Configuration.GATEWAY_NAIVE):
        governed = observed[configuration]
        assert governed["first"].status == "delivery_uncertain", governed["first"]
        assert governed["first"].client_visible_state == "explicit_uncertain"
        assert governed["retry"].status == "delivery_uncertain", governed["retry"]
        assert governed["retry"].receipt_id == governed["first"].receipt_id
        assert governed["effects"] == 1
        assert (
            governed["crossings"] == 1
        )  # observed at the fault layer, not read from the gateway
        snapshot = governed["snapshot"]
        assert snapshot.sent_attempt_count == 1
        assert snapshot.debit_count == 1
        assert snapshot.refund_count == 0
        assert snapshot.receipt_outcomes() == ["delivery_uncertain"]
