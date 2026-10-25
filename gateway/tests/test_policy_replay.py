"""``amw-replay``: offline policy replay over recorded gateway receipts.

Input corpus: the gateway's existing receipt signing goldens
(``tests/fixtures/receipt_signing_golden``), each a receipt the gateway's
ReceiptService signs, verified here against the published test key in
``spec/test-vectors/keys.json`` before replay. No new call logs are added.

Acceptance criteria (R&D spec V1):
1. nothing forwarded            -> test_replay_forwards_nothing_*
2. one verifying receipt/call   -> test_one_signed_receipt_per_call_*
3. only targeted calls flip     -> test_only_targeted_calls_flip_*
4. summary matches receipts     -> test_summary_counts_match_receipts
5. real recorded calls only     -> test_recorded_calls_only_*
"""

from __future__ import annotations

import json
from pathlib import Path
import socket
import sys

import httpx
import pytest

from app.replay.cli import main as replay_main
from app.replay.policy_replay import (
    NetworkSealedError,
    ReplayError,
    load_policy_bundle,
    load_recorded_calls,
    network_sealed,
    replay_calls,
)
from app.services.signing_keys import (
    SigningKeyError,
    canonical_json,
    get_signing_key_service,
)
from b2a_sdk.receipt_verifier import key_set_from_document, verify_bundle

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "receipt_signing_golden"
SPEC_KEYS = Path(__file__).resolve().parents[2] / "spec" / "test-vectors" / "keys.json"

#: Recorded receipts with no approval on record: the targets of the
#: human-approval rule below.
NO_APPROVAL_SUCCESSES = {
    "success_action_v1.json",
    "success_legacy.json",
    "success_linked.json",
}


def _recorded_keys():
    return key_set_from_document(json.loads(SPEC_KEYS.read_text()))


def _calls():
    return load_recorded_calls(GOLDEN_DIR, recorded_keys=_recorded_keys())


def _bundle(tmp_path: Path, document: dict, name: str = "bundle.json"):
    path = tmp_path / name
    path.write_text(json.dumps(document))
    return path


def _approval_rule(tmp_path: Path) -> Path:
    return _bundle(
        tmp_path,
        {
            "policies": [
                {"policy_id": "pol-require-approval", "human_approval_required": True}
            ]
        },
    )


def _claims(receipt: dict) -> dict:
    return json.loads(receipt["signing_input"])


def _record_payload(**overrides):
    payload = {
        "receipt_id": "rcpt_replay_0001",
        "tool": "golden-path-echo",
        "wallet_id": "agt-golden-0001",
        "outcome": "success",
        "credits_authorized": "2.5",
    }
    payload.update(overrides)
    return payload


def _record_file(tmp_path: Path, payload: dict, name: str = "call.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps({"signing_input": canonical_json(payload)}))
    return path


# --- 1. nothing is forwarded -------------------------------------------------


class _Tripwire:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def hit(self, name: str):
        def _record(*_args, **_kwargs):
            self.calls.append(name)
            raise AssertionError(f"replay reached {name}")

        return _record


@pytest.fixture
def tripwire(monkeypatch):
    wire = _Tripwire()
    from app.routers import mcp as mcp_router
    from app.services import upstream_mcp
    from app.trust import adapters

    for owner, attr in (
        (httpx.Client, "send"),
        (httpx.AsyncClient, "send"),
        (httpx.HTTPTransport, "handle_request"),
        (httpx.AsyncHTTPTransport, "handle_async_request"),
        (upstream_mcp.UpstreamMcpAdapter, "call_tool"),
        (adapters.McpGovernedAdapter, "invoke"),
        (mcp_router, "invoke_tool"),
    ):
        monkeypatch.setattr(owner, attr, wire.hit(f"{owner.__name__}.{attr}"))

    events: list[str] = []
    armed = {"on": True}

    def _audit(event: str, _args) -> None:
        if armed["on"] and event in {"socket.connect", "socket.getaddrinfo"}:
            events.append(event)

    sys.addaudithook(_audit)
    wire.socket_events = events  # type: ignore[attr-defined]
    yield wire
    armed["on"] = False


def test_replay_forwards_nothing_library(tmp_path, tripwire):
    result = replay_calls(_calls(), load_policy_bundle(_approval_rule(tmp_path)))
    assert len(result.receipts) == 6
    assert tripwire.calls == []
    assert tripwire.socket_events == []


def test_replay_forwards_nothing_cli(tmp_path, tripwire, capsys):
    code = replay_main(
        [
            "--bundle",
            str(_approval_rule(tmp_path)),
            "--calls",
            str(GOLDEN_DIR),
            "--recorded-keys",
            str(SPEC_KEYS),
            "--out",
            str(tmp_path / "out"),
        ]
    )
    assert code == 0, capsys.readouterr().err
    assert tripwire.calls == []
    assert tripwire.socket_events == []
    lines = (tmp_path / "out" / "receipts.jsonl").read_text().splitlines()
    assert len(lines) == 6


def test_network_is_sealed_during_replay():
    with network_sealed():
        with pytest.raises(NetworkSealedError):
            socket.create_connection(("127.0.0.1", 9))
        with pytest.raises(NetworkSealedError):
            socket.getaddrinfo("example.com", 443)
    # Restored afterwards.
    assert socket.getaddrinfo.__name__ != "_refuse"


# --- 2. exactly one receipt per call, signatures verify ----------------------


def test_one_signed_receipt_per_call_verifies(tmp_path):
    calls = _calls()
    result = replay_calls(calls, load_policy_bundle(_approval_rule(tmp_path)))
    assert len(result.receipts) == len(calls) == 6
    assert sorted(_claims(r)["call_hash"] for r in result.receipts) == sorted(
        c.call_hash for c in calls
    )
    assert len({r["receipt_id"] for r in result.receipts}) == len(calls)

    key_set = key_set_from_document({"keys": [result.public_key]})
    for receipt in result.receipts:
        verdict = verify_bundle(receipt, key_set)
        assert verdict.ok, verdict.reason
        claims = _claims(receipt)
        assert (
            claims["bundle_hash"]
            == load_policy_bundle(_approval_rule(tmp_path)).bundle_hash
        )
        assert claims["old_decision"] in {"allow", "deny"}
        assert claims["new_decision"] in {"allow", "deny"}

    tampered = dict(result.receipts[0])
    claims = _claims(tampered)
    claims["new_decision"] = "allow" if claims["new_decision"] == "deny" else "deny"
    tampered["signing_input"] = json.dumps(
        claims, sort_keys=True, separators=(",", ":")
    )
    assert not verify_bundle(tampered, key_set).ok


@pytest.mark.anyio
async def test_one_signed_receipt_per_call_gateway_verifier(tmp_path, clean_database):
    """The gateway's own SigningKeyService verifier accepts every receipt."""
    signer = get_signing_key_service()
    await signer.ensure_active_key()
    result = replay_calls(
        _calls(), load_policy_bundle(_approval_rule(tmp_path)), signer=signer
    )
    for receipt in result.receipts:
        assert await signer.verify_payload(
            _claims(receipt), signature=receipt["signature"], key_id=receipt["kid"]
        )


# --- 3. only the targeted calls flip -----------------------------------------


def test_only_targeted_calls_flip_allow_to_deny(tmp_path):
    calls = _calls()
    result = replay_calls(calls, load_policy_bundle(_approval_rule(tmp_path)))
    by_source = {
        c.source: _claims(r) for c, r in zip(calls, result.receipts, strict=True)
    }
    for source, claims in by_source.items():
        if source in NO_APPROVAL_SUCCESSES:
            assert claims["old_decision"] == "allow"
            assert claims["new_decision"] == "deny"
            assert claims["new_reason"] == "human_approval_required"
            assert claims["change"] == "allow_to_deny"
        elif claims["old_decision"] == "deny":
            assert claims["change"] == "unchanged", source
            assert claims["new_decision"] == "deny"
        else:
            # Approval-holding successes satisfy the new approval rule, but the
            # goldens record no tier and the bundle's default medium ceiling
            # still applies, so replay fails closed on the unknown tier.
            assert claims["old_decision"] == "allow"
            assert claims["new_decision"] == "deny"
            assert claims["new_reason"] == "replay_input_unknown:risk_tier"
            assert claims["unevaluated_constraints"] == ["risk_tier"]
            assert claims["change"] == "allow_to_deny"


def test_only_targeted_calls_flip_untargeted_rules_change_nothing(tmp_path):
    calls = _calls()
    # A rule scoped to a different wallet never touches these calls: with no
    # applicable policy there is no ceiling to fail closed on either.
    result = replay_calls(
        calls,
        load_policy_bundle(
            _bundle(
                tmp_path,
                {"wallet_id": "agt-other", "allowed_tools": ["something-else"]},
            )
        ),
    )
    assert {_claims(r)["change"] for r in result.receipts} == {"unchanged"}


def test_only_targeted_calls_flip_recorded_non_policy_denial_stays(tmp_path):
    result = replay_calls(_calls(), load_policy_bundle(_bundle(tmp_path, {})))
    denial = [
        _claims(r) for r in result.receipts if _claims(r)["old_decision"] == "deny"
    ]
    assert len(denial) == 1
    assert denial[0]["new_decision"] == "deny"
    assert denial[0]["new_reason"] == "recorded_denial:permit_budget_exceeded"


# --- 4. summary counts match the receipts ------------------------------------


def test_summary_counts_match_receipts(tmp_path):
    out = tmp_path / "out"
    assert (
        replay_main(
            [
                "--bundle",
                str(_approval_rule(tmp_path)),
                "--calls",
                str(GOLDEN_DIR),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    summary = json.loads((out / "summary.json").read_text())
    receipts = [
        json.loads(line) for line in (out / "receipts.jsonl").read_text().splitlines()
    ]
    changes = [_claims(r)["change"] for r in receipts]
    assert summary["total"] == len(receipts) == 6
    assert summary["allow_to_deny"] == changes.count("allow_to_deny") == 5
    assert summary["deny_to_allow"] == changes.count("deny_to_allow") == 0
    assert summary["unchanged"] == changes.count("unchanged") == 1
    assert (
        summary["allow_to_deny"] + summary["deny_to_allow"] + summary["unchanged"]
        == summary["total"]
    )
    assert {_claims(r)["bundle_hash"] for r in receipts} == {summary["bundle_hash"]}
    keys = key_set_from_document(json.loads((out / "keys.json").read_text()))
    assert all(verify_bundle(r, keys).ok for r in receipts)


# --- 5. real recorded calls only ---------------------------------------------


def test_recorded_calls_only_corpus_is_existing_signed_receipts():
    calls = _calls()  # raises unless every recording verifies
    assert sorted(c.source for c in calls) == sorted(
        p.name for p in GOLDEN_DIR.glob("*.json")
    )
    assert {c.payload["kid"] for c in calls} == {"sk_golden_0001"}


def test_recorded_calls_only_rejects_hand_shaped_calls(tmp_path):
    fake = tmp_path / "calls.jsonl"
    fake.write_text(
        json.dumps({"tool": "golden-path-echo", "outcome": "success"}) + "\n"
    )
    with pytest.raises(ReplayError, match="not a gateway receipt"):
        load_recorded_calls(fake)


def test_recorded_calls_only_rejects_tampered_recording(tmp_path):
    record = json.loads((GOLDEN_DIR / "success_legacy.json").read_text())
    record["signing_input"] = record["signing_input"].replace(
        '"outcome":"success"', '"outcome":"denied"'
    )
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(record))
    with pytest.raises(ReplayError, match="signature"):
        load_recorded_calls(path, recorded_keys=_recorded_keys())


# --- 6. directories load every recording shape -------------------------------


def test_load_recorded_calls_directory_loads_json_and_jsonl(tmp_path):
    record = json.loads((GOLDEN_DIR / "success_legacy.json").read_text())
    calls_dir = tmp_path / "calls"
    calls_dir.mkdir()
    (calls_dir / "a.json").write_text(json.dumps(record))
    (calls_dir / "b.jsonl").write_text(json.dumps(record) + "\n")
    calls = load_recorded_calls(calls_dir)
    assert [c.source for c in calls] == ["a.json", "b.jsonl:1"]


def test_load_recorded_calls_jsonl_only_directory(tmp_path):
    record = json.loads((GOLDEN_DIR / "success_legacy.json").read_text())
    calls_dir = tmp_path / "calls"
    calls_dir.mkdir()
    (calls_dir / "only.jsonl").write_text(json.dumps(record) + "\n")
    assert len(load_recorded_calls(calls_dir)) == 1


def test_load_recorded_calls_directory_loads_ndjson(tmp_path):
    record = json.loads((GOLDEN_DIR / "success_legacy.json").read_text())
    calls_dir = tmp_path / "calls"
    calls_dir.mkdir()
    (calls_dir / "only.ndjson").write_text(json.dumps(record) + "\n")
    assert len(load_recorded_calls(calls_dir)) == 1


# --- 7. strict bundle parsing ------------------------------------------------


def test_load_policy_bundle_bools_match_pydantic(tmp_path):
    bundle = load_policy_bundle(_bundle(tmp_path, {"require_real_effects": "false"}))
    assert bundle.models[0].require_real_effects is False
    bundle = load_policy_bundle(
        _bundle(tmp_path, {"human_approval_required": "true"}, name="b2.json")
    )
    assert bundle.models[0].human_approval_required is True
    bundle = load_policy_bundle(
        _bundle(tmp_path, {"is_active": "false"}, name="b3.json")
    )
    assert bundle.models[0].is_active is False


def test_load_policy_bundle_rejects_garbage_bools(tmp_path):
    for field, value in (
        ("require_real_effects", "maybe"),
        ("human_approval_required", "sometimes"),
        ("is_active", "active-ish"),
    ):
        with pytest.raises(ReplayError, match="must be a boolean"):
            load_policy_bundle(_bundle(tmp_path, {field: value}))


def test_load_policy_bundle_rejects_unknown_risk_tier(tmp_path):
    with pytest.raises(ReplayError, match="unknown risk_tier"):
        load_policy_bundle(_bundle(tmp_path, {"risk_tier": "extreme"}))
    with pytest.raises(ReplayError, match="unknown risk_tier"):
        load_policy_bundle(_bundle(tmp_path, {"risk_tier": 5}))


# --- 8. unknown replay inputs fail closed -------------------------------------


def test_replay_unknown_simulation_denies(tmp_path):
    calls = load_recorded_calls(
        _record_file(tmp_path, _record_payload(risk_tier="low"))
    )
    result = replay_calls(
        calls,
        load_policy_bundle(_bundle(tmp_path, {"require_real_effects": True})),
    )
    claims = _claims(result.receipts[0])
    assert claims["new_decision"] == "deny"
    assert claims["new_reason"] == "replay_input_unknown:simulation"
    assert claims["unevaluated_constraints"] == ["simulation"]


def test_replay_recorded_simulation_reaches_policy(tmp_path):
    bundle_path = _bundle(tmp_path, {"require_real_effects": True})
    calls = load_recorded_calls(
        _record_file(
            tmp_path,
            _record_payload(simulation=True, risk_tier="low"),
            name="t.json",
        )
    )
    result = replay_calls(calls, load_policy_bundle(bundle_path))
    assert _claims(result.receipts[0])["new_reason"] == "real_effects_required"
    calls = load_recorded_calls(
        _record_file(
            tmp_path,
            _record_payload(simulation=False, risk_tier="low"),
            name="f.json",
        )
    )
    result = replay_calls(calls, load_policy_bundle(bundle_path))
    claims = _claims(result.receipts[0])
    assert claims["new_decision"] == "allow"
    assert claims["unevaluated_constraints"] == []


def test_replay_unknown_risk_tier_denies(tmp_path):
    calls = load_recorded_calls(_record_file(tmp_path, _record_payload()))
    result = replay_calls(
        calls, load_policy_bundle(_bundle(tmp_path, {"risk_tier": "low"}))
    )
    claims = _claims(result.receipts[0])
    assert claims["new_decision"] == "deny"
    assert claims["new_reason"] == "replay_input_unknown:risk_tier"
    assert claims["unevaluated_constraints"] == ["risk_tier"]


def test_replay_recorded_risk_tier_reaches_policy(tmp_path):
    bundle_path = _bundle(tmp_path, {"risk_tier": "low"})
    calls = load_recorded_calls(
        _record_file(tmp_path, _record_payload(risk_tier="low"), name="low.json")
    )
    result = replay_calls(calls, load_policy_bundle(bundle_path))
    claims = _claims(result.receipts[0])
    assert claims["new_decision"] == "allow"
    assert claims["unevaluated_constraints"] == []
    calls = load_recorded_calls(
        _record_file(tmp_path, _record_payload(risk_tier="high"), name="high.json")
    )
    result = replay_calls(calls, load_policy_bundle(bundle_path))
    assert _claims(result.receipts[0])["new_reason"] == "risk_tier_not_allowed"


def test_replay_default_risk_ceiling_denies_unknown_tier(tmp_path):
    # A bundle that omits risk_tier still carries the default medium ceiling,
    # so a recorded call with no tier fails closed like an explicit ceiling.
    for document in ({}, {"allowed_tools": ["golden-path-echo"]}):
        calls = load_recorded_calls(_record_file(tmp_path, _record_payload()))
        result = replay_calls(calls, load_policy_bundle(_bundle(tmp_path, document)))
        claims = _claims(result.receipts[0])
        assert claims["new_decision"] == "deny"
        assert claims["new_reason"] == "replay_input_unknown:risk_tier"
        assert claims["unevaluated_constraints"] == ["risk_tier"]
    bundle_path = _bundle(tmp_path, {})
    calls = load_recorded_calls(
        _record_file(tmp_path, _record_payload(risk_tier="medium"), name="m.json")
    )
    result = replay_calls(calls, load_policy_bundle(bundle_path))
    assert _claims(result.receipts[0])["new_decision"] == "allow"
    calls = load_recorded_calls(
        _record_file(tmp_path, _record_payload(risk_tier="high"), name="h.json")
    )
    result = replay_calls(calls, load_policy_bundle(bundle_path))
    assert _claims(result.receipts[0])["new_reason"] == "risk_tier_not_allowed"


def test_replay_unknown_credits_denies(tmp_path):
    payload = _record_payload(risk_tier="low")
    del payload["credits_authorized"]
    calls = load_recorded_calls(_record_file(tmp_path, payload))
    result = replay_calls(
        calls, load_policy_bundle(_bundle(tmp_path, {"max_cost_per_action": "1.0"}))
    )
    claims = _claims(result.receipts[0])
    assert claims["new_decision"] == "deny"
    assert claims["new_reason"] == "replay_input_unknown:credits_authorized"
    assert claims["unevaluated_constraints"] == ["credits_authorized"]


# --- 9. non-success recorded outcomes stay denied -----------------------------


def test_replay_recorded_insufficient_funds_never_flips(tmp_path):
    calls = load_recorded_calls(
        _record_file(tmp_path, _record_payload(outcome="insufficient_funds"))
    )
    assert calls[0].old_decision == "deny"
    result = replay_calls(calls, load_policy_bundle(_bundle(tmp_path, {})))
    claims = _claims(result.receipts[0])
    assert claims["old_decision"] == "deny"
    assert claims["new_decision"] == "deny"
    assert claims["new_reason"] == "recorded_denial:insufficient_funds"


def test_replay_insufficient_funds_with_policy_reason_stays_denied(tmp_path):
    # A funds refusal is never a policy decision, even when its reason_code
    # names one: a permissive bundle cannot flip it.
    payload = _record_payload(
        outcome="insufficient_funds",
        reason_code="tool_not_allowed",
        risk_tier="low",
    )
    calls = load_recorded_calls(_record_file(tmp_path, payload))
    assert calls[0].old_decision == "deny"
    result = replay_calls(calls, load_policy_bundle(_bundle(tmp_path, {})))
    claims = _claims(result.receipts[0])
    assert claims["old_decision"] == "deny"
    assert claims["new_decision"] == "deny"
    assert claims["new_reason"] == "recorded_denial:tool_not_allowed"
    assert claims["change"] == "unchanged"


def test_load_recorded_calls_rejects_unknown_outcome(tmp_path):
    payload = _record_payload(
        outcome="cancelled", reason_code="tool_not_allowed", risk_tier="low"
    )
    with pytest.raises(ReplayError, match="unknown outcome"):
        load_recorded_calls(_record_file(tmp_path, payload))


def test_replay_post_authorization_outcomes_reevaluated(tmp_path):
    # Post-authorization outcomes ran past the policy gate when recorded, so
    # they replay as allows; a stricter bundle flips them to deny with the
    # new policy's reason, not a recorded-denial short-circuit.
    bundle_path = _bundle(tmp_path, {"human_approval_required": True})
    for outcome in (
        "response_rejected",
        "delivery_uncertain",
        "failed_refunded",
        "failed_unrefunded",
    ):
        calls = load_recorded_calls(
            _record_file(
                tmp_path,
                _record_payload(outcome=outcome, risk_tier="low"),
                name=f"{outcome}.json",
            )
        )
        assert calls[0].old_decision == "allow", outcome
        result = replay_calls(calls, load_policy_bundle(bundle_path))
        claims = _claims(result.receipts[0])
        assert claims["old_decision"] == "allow", outcome
        assert claims["new_decision"] == "deny", outcome
        assert claims["new_reason"] == "human_approval_required", outcome
        assert claims["change"] == "allow_to_deny", outcome


# --- 10. CLI errors stay on protocol ------------------------------------------


def test_cli_signing_key_error_returns_2(tmp_path, capsys, monkeypatch):
    import app.replay.policy_replay as policy_replay

    def _boom():
        raise SigningKeyError("trust_signing_private_key_required")

    monkeypatch.setattr(policy_replay, "get_signing_key_service", _boom)
    code = replay_main(
        [
            "--bundle",
            str(_approval_rule(tmp_path)),
            "--calls",
            str(GOLDEN_DIR),
            "--out",
            str(tmp_path / "out"),
        ]
    )
    assert code == 2
    assert "amw-replay: trust_signing_private_key_required" in capsys.readouterr().err


def test_cli_output_write_failure_returns_2(tmp_path, capsys):
    out_file = tmp_path / "out"
    out_file.write_text("not a directory")
    code = replay_main(
        [
            "--bundle",
            str(_approval_rule(tmp_path)),
            "--calls",
            str(GOLDEN_DIR),
            "--out",
            str(out_file),
        ]
    )
    assert code == 2
    assert "amw-replay: " in capsys.readouterr().err


def test_cli_malformed_recorded_keys_returns_2(tmp_path, capsys):
    for name, document, message in (
        (
            "no-keys.json",
            {"keys": "not-a-list"},
            "key set document has no 'keys' list",
        ),
        (
            "not-object.json",
            ["not", "an", "object"],
            "key set document must be a JSON object",
        ),
    ):
        keys_path = tmp_path / name
        keys_path.write_text(json.dumps(document))
        code = replay_main(
            [
                "--bundle",
                str(_approval_rule(tmp_path)),
                "--calls",
                str(GOLDEN_DIR),
                "--recorded-keys",
                str(keys_path),
                "--out",
                str(tmp_path / "out"),
            ]
        )
        assert code == 2, name
        assert f"amw-replay: {message}" in capsys.readouterr().err, name


# --- 11. the seal survives nesting --------------------------------------------


def test_network_sealed_nested_and_sequential_restores():
    def _fns():
        return (
            socket.socket.connect,
            socket.socket.connect_ex,
            socket.create_connection,
            socket.getaddrinfo,
        )

    before = _fns()
    with network_sealed():
        with network_sealed():
            with pytest.raises(NetworkSealedError):
                socket.create_connection(("127.0.0.1", 9))
        with pytest.raises(NetworkSealedError):
            socket.create_connection(("127.0.0.1", 9))
    assert _fns() == before
    with network_sealed():
        pass
    assert _fns() == before


# --- 12. recorded signature provenance ----------------------------------------


def test_replay_receipt_marks_verified_recordings(tmp_path):
    calls = load_recorded_calls(GOLDEN_DIR, recorded_keys=_recorded_keys())
    assert all(c.signature_verified for c in calls)
    result = replay_calls(calls, load_policy_bundle(_approval_rule(tmp_path)))
    assert [_claims(r)["recorded_signature_verified"] for r in result.receipts] == [
        True
    ] * 6


def test_replay_receipt_marks_unverified_recordings(tmp_path):
    calls = load_recorded_calls(GOLDEN_DIR)
    assert not any(c.signature_verified for c in calls)
    result = replay_calls(calls, load_policy_bundle(_approval_rule(tmp_path)))
    assert [_claims(r)["recorded_signature_verified"] for r in result.receipts] == [
        False
    ] * 6


def test_recorded_envelope_mismatch_raises(tmp_path):
    base = json.loads((GOLDEN_DIR / "success_legacy.json").read_text())
    signed = json.loads(base["signing_input"])
    # An envelope that repeats the signed claims still verifies.
    matching = dict(base)
    matching["receipt_id"] = signed["receipt_id"]
    matching["kid"] = signed["kid"]
    match_path = tmp_path / "match.json"
    match_path.write_text(json.dumps(matching))
    assert (
        load_recorded_calls(match_path, recorded_keys=_recorded_keys())[
            0
        ].signature_verified
        is True
    )
    # An envelope that mislabels them fails closed instead of degrading.
    for name, outer in (
        ("bad-id.json", {"receipt_id": "rcpt_not_the_signed_one"}),
        ("bad-kid.json", {"kid": "sk_not_the_signer"}),
    ):
        record = dict(base)
        record.update(outer)
        path = tmp_path / name
        path.write_text(json.dumps(record))
        with pytest.raises(ReplayError, match="mismatch"):
            load_recorded_calls(path, recorded_keys=_recorded_keys())


# --- 13. limits must be finite ------------------------------------------------


def test_load_policy_bundle_rejects_non_finite_limits(tmp_path):
    for field in ("max_cost_per_action", "daily_spend_limit"):
        for value in ("NaN", "sNaN", "Infinity", "-Infinity"):
            with pytest.raises(ReplayError, match="must be finite"):
                load_policy_bundle(_bundle(tmp_path, {field: value}))
    raw = tmp_path / "raw.json"
    raw.write_text('{"max_cost_per_action": NaN}')
    with pytest.raises(ReplayError, match="must be finite"):
        load_policy_bundle(raw)


# --- 14. unknown daily spend fails closed -------------------------------------


def test_replay_daily_spend_limit_without_usage_denies(tmp_path):
    result = replay_calls(
        _calls(), load_policy_bundle(_bundle(tmp_path, {"daily_spend_limit": "100.00"}))
    )
    assert len(result.receipts) == 6
    for receipt in result.receipts:
        claims = _claims(receipt)
        if claims["old_decision"] == "deny":
            continue
        assert claims["new_decision"] == "deny"
        assert claims["new_reason"] == "daily_spend_unknown"


# --- 15. bundle shape negatives -----------------------------------------------


def test_load_policy_bundle_rejects_bad_shapes(tmp_path):
    with pytest.raises(ReplayError, match="unknown fields"):
        load_policy_bundle(_bundle(tmp_path, {"bogus_field": True}))
    with pytest.raises(ReplayError, match="list of strings"):
        load_policy_bundle(_bundle(tmp_path, {"allowed_tools": "golden-path-echo"}))
    with pytest.raises(ReplayError, match="list of strings"):
        load_policy_bundle(
            _bundle(tmp_path, {"allowed_service_categories": ["ok", 7]}, name="l2.json")
        )
    with pytest.raises(ReplayError, match="not a number"):
        load_policy_bundle(
            _bundle(tmp_path, {"max_cost_per_action": "lots"}, name="c.json")
        )
    with pytest.raises(ReplayError, match="no policies"):
        load_policy_bundle(_bundle(tmp_path, {"policies": []}, name="e.json"))
    bad = tmp_path / "arr.json"
    bad.write_text(json.dumps(["not", "an", "object"]))
    with pytest.raises(ReplayError, match="must be a JSON object"):
        load_policy_bundle(bad)


# --- 16. recorded money must be finite ---------------------------------------


def test_replay_non_finite_recorded_money_denies(tmp_path):
    # Non-finite recorded money denies with the live gateway's own reason
    # (the shared decision core fails the comparison closed); both money
    # inputs behave the same.
    bundle = load_policy_bundle(_bundle(tmp_path, {}))
    cases = 0
    for field in ("credits_authorized", "daily_spend_used"):
        for value in ("NaN", "Infinity", "-Infinity"):
            payload = _record_payload(risk_tier="low")
            payload[field] = value
            calls = load_recorded_calls(
                _record_file(tmp_path, payload, name=f"{field}-{cases}.json")
            )
            result = replay_calls(calls, bundle)
            claims = _claims(result.receipts[0])
            assert claims["new_decision"] == "deny", (field, value)
            assert claims["new_reason"] == "policy_cost_invalid", (field, value)
            cases += 1
    assert cases == 6


# --- 17. non-UTF-8 CLI inputs stay on protocol -------------------------------


def test_cli_non_utf8_inputs_return_2(tmp_path, capsys):
    bad = tmp_path / "bad.bin"
    bad.write_bytes(b"\xff\xfe\xfd\xfc")
    bundle_path = _approval_rule(tmp_path)
    cases = [
        ["--bundle", str(bad), "--calls", str(GOLDEN_DIR)],
        ["--bundle", str(bundle_path), "--calls", str(bad)],
        [
            "--bundle",
            str(bundle_path),
            "--calls",
            str(GOLDEN_DIR),
            "--recorded-keys",
            str(bad),
        ],
    ]
    for argv in cases:
        code = replay_main([*argv, "--out", str(tmp_path / "out")])
        assert code == 2, argv
        assert "amw-replay: " in capsys.readouterr().err, argv


# --- 18. recorded string fields are validated ---------------------------------


def test_load_recorded_calls_rejects_non_string_fields(tmp_path):
    for field in ("receipt_id", "tool", "wallet_id", "outcome"):
        payload = _record_payload()
        payload[field] = 12345
        with pytest.raises(ReplayError, match="must be a string"):
            load_recorded_calls(
                _record_file(tmp_path, payload, name=f"bad-{field}.json")
            )
    payload = _record_payload()
    payload["reason_code"] = 12345
    with pytest.raises(ReplayError, match="reason_code.*must be a string"):
        load_recorded_calls(_record_file(tmp_path, payload, name="bad-reason.json"))


def test_load_recorded_calls_rejects_non_bool_simulation(tmp_path):
    for i, value in enumerate(("true", "false", 1, 0, 1.0, [], {})):
        payload = _record_payload(simulation=value, risk_tier="low")
        with pytest.raises(ReplayError, match="simulation.*must be a boolean"):
            load_recorded_calls(
                _record_file(tmp_path, payload, name=f"bad-sim-{i}.json")
            )


# --- 19. bundle string fields are validated -----------------------------------


def test_load_policy_bundle_rejects_non_string_fields(tmp_path):
    for field in ("policy_id", "wallet_id", "name"):
        for i, value in enumerate((12345, 0, False)):
            with pytest.raises(ReplayError, match="must be a string"):
                load_policy_bundle(
                    _bundle(tmp_path, {field: value}, name=f"bad-{field}-{i}.json")
                )
    bundle = load_policy_bundle(
        _bundle(
            tmp_path,
            {"policy_id": None, "wallet_id": None, "name": None},
            name="none.json",
        )
    )
    assert bundle.models[0].policy_id == "replay-policy-0"
    assert bundle.models[0].wallet_id == ""
    assert bundle.models[0].name == "replay policy 0"
    bundle = load_policy_bundle(
        _bundle(
            tmp_path, {"policy_id": "", "wallet_id": "", "name": ""}, name="empty.json"
        )
    )
    assert bundle.models[0].policy_id == "replay-policy-0"
    assert bundle.models[0].wallet_id == ""
    assert bundle.models[0].name == "replay policy 0"


# --- 20. daily spend limit needs the authorized cost --------------------------


def test_replay_daily_spend_limit_without_credits_denies(tmp_path):
    payload = _record_payload(risk_tier="low", daily_spend_used="10.00")
    del payload["credits_authorized"]
    calls = load_recorded_calls(_record_file(tmp_path, payload))
    result = replay_calls(
        calls, load_policy_bundle(_bundle(tmp_path, {"daily_spend_limit": "100.00"}))
    )
    claims = _claims(result.receipts[0])
    assert claims["new_decision"] == "deny"
    assert claims["new_reason"] == "replay_input_unknown:credits_authorized"
    assert claims["unevaluated_constraints"] == ["credits_authorized"]


# --- 21. recorded policy denials can flip to allow ----------------------------


def test_replay_recorded_tool_denial_flips_to_allow(tmp_path):
    payload = _record_payload(
        outcome="denied",
        reason_code="tool_not_allowed",
        risk_tier="low",
    )
    calls = load_recorded_calls(_record_file(tmp_path, payload))
    assert calls[0].old_decision == "deny"
    result = replay_calls(calls, load_policy_bundle(_bundle(tmp_path, {})))
    claims = _claims(result.receipts[0])
    assert claims["old_decision"] == "deny"
    assert claims["new_decision"] == "allow"
    assert claims["change"] == "deny_to_allow"
    key_set = key_set_from_document({"keys": [result.public_key]})
    assert verify_bundle(result.receipts[0], key_set).ok


# --- 22. approval_id is validated ---------------------------------------------


def test_load_recorded_calls_rejects_bad_approval_id(tmp_path):
    for i, value in enumerate((1, True, "", ["appr-1"])):
        payload = _record_payload(approval_id=value)
        with pytest.raises(ReplayError, match="approval_id"):
            load_recorded_calls(
                _record_file(tmp_path, payload, name=f"bad-approval-{i}.json")
            )


def test_load_recorded_calls_bad_approval_id_names_record(tmp_path):
    payload = _record_payload(approval_id="")
    with pytest.raises(ReplayError, match=r"bad-approval\.json.*approval_id"):
        load_recorded_calls(_record_file(tmp_path, payload, name="bad-approval.json"))
    good = {"signing_input": canonical_json(_record_payload())}
    bad = {"signing_input": canonical_json(_record_payload(approval_id=1))}
    path = tmp_path / "calls.jsonl"
    path.write_text(json.dumps(good) + "\n" + json.dumps(bad) + "\n")
    with pytest.raises(ReplayError, match=r"calls\.jsonl:2.*approval_id"):
        load_recorded_calls(path)


def test_load_recorded_calls_rejects_whitespace_approval_id(tmp_path):
    for i, value in enumerate((" ", "\t\n", "  \t  \n ")):
        payload = _record_payload(approval_id=value)
        with pytest.raises(ReplayError, match="approval_id"):
            load_recorded_calls(
                _record_file(tmp_path, payload, name=f"ws-approval-{i}.json")
            )


def test_load_recorded_calls_accepts_missing_or_valid_approval_id(tmp_path):
    assert (
        load_recorded_calls(_record_file(tmp_path, _record_payload()))[0].payload.get(
            "approval_id"
        )
        is None
    )
    payload = _record_payload(approval_id=None)
    assert (
        load_recorded_calls(_record_file(tmp_path, payload, name="none.json"))[
            0
        ].payload.get("approval_id")
        is None
    )
    payload = _record_payload(approval_id="appr-0001", risk_tier="low")
    calls = load_recorded_calls(_record_file(tmp_path, payload, name="ok.json"))
    result = replay_calls(calls, load_policy_bundle(_approval_rule(tmp_path)))
    assert _claims(result.receipts[0])["new_decision"] == "allow"


# --- 23. --require-verified ---------------------------------------------------


def test_cli_require_verified_without_keys_returns_2(tmp_path, capsys):
    code = replay_main(
        [
            "--bundle",
            str(_approval_rule(tmp_path)),
            "--calls",
            str(GOLDEN_DIR),
            "--out",
            str(tmp_path / "out"),
            "--require-verified",
        ]
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "--require-verified" in err
    assert "--recorded-keys" in err


def test_cli_require_verified_rejects_unverified_calls(tmp_path, capsys, monkeypatch):
    import app.replay.cli as replay_cli

    from dataclasses import replace

    real_load = replay_cli.load_recorded_calls

    def _strip_verification(path, **kwargs):
        return [replace(c, signature_verified=False) for c in real_load(path, **kwargs)]

    monkeypatch.setattr(replay_cli, "load_recorded_calls", _strip_verification)
    out = tmp_path / "out"
    code = replay_main(
        [
            "--bundle",
            str(_approval_rule(tmp_path)),
            "--calls",
            str(GOLDEN_DIR),
            "--recorded-keys",
            str(SPEC_KEYS),
            "--out",
            str(out),
            "--require-verified",
        ]
    )
    assert code == 2
    captured = capsys.readouterr()
    assert "6 unverified" in captured.err
    assert captured.out == ""
    assert not (out / "summary.json").exists()
    assert not (out / "receipts.jsonl").exists()


def test_cli_require_verified_passes_when_all_verified(tmp_path, capsys):
    out = tmp_path / "out"
    code = replay_main(
        [
            "--bundle",
            str(_approval_rule(tmp_path)),
            "--calls",
            str(GOLDEN_DIR),
            "--recorded-keys",
            str(SPEC_KEYS),
            "--out",
            str(out),
            "--require-verified",
        ]
    )
    assert code == 0, capsys.readouterr().err
    summary = json.loads((out / "summary.json").read_text())
    assert summary["total"] == 6


# --- 24. CLI outputs are written atomically -----------------------------------


def _replay_out_argv(tmp_path: Path) -> tuple[list[str], Path]:
    out = tmp_path / "out"
    return (
        [
            "--bundle",
            str(_approval_rule(tmp_path)),
            "--calls",
            str(GOLDEN_DIR),
            "--out",
            str(out),
        ],
        out,
    )


def test_cli_success_writes_only_outputs(tmp_path, capsys):
    argv, out = _replay_out_argv(tmp_path)
    assert replay_main(argv) == 0, capsys.readouterr().err
    assert sorted(p.name for p in out.iterdir()) == [
        "keys.json",
        "receipts.jsonl",
        "summary.json",
    ]


def test_cli_outputs_rename_into_place(tmp_path, capsys, monkeypatch):
    import os

    real_replace = os.replace
    renamed: list[tuple[Path, Path]] = []

    def _spy(src, dst):
        renamed.append((Path(src), Path(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _spy)
    argv, out = _replay_out_argv(tmp_path)
    assert replay_main(argv) == 0, capsys.readouterr().err
    ours = [(src, dst) for src, dst in renamed if dst.parent == out]
    assert sorted(dst.name for _, dst in ours) == [
        "keys.json",
        "receipts.jsonl",
        "summary.json",
    ]
    assert all(src.parent == out for src, _ in ours)


def test_cli_first_output_failure_leaves_no_files(tmp_path, capsys, monkeypatch):
    import tempfile

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(tempfile, "mkstemp", _boom)
    argv, out = _replay_out_argv(tmp_path)
    assert replay_main(argv) == 2
    assert "amw-replay: disk full" in capsys.readouterr().err
    assert list(out.iterdir()) == []


def test_cli_second_output_failure_removes_first_temp(tmp_path, capsys, monkeypatch):
    import tempfile

    real_mkstemp = tempfile.mkstemp
    attempts = {"count": 0}

    def _fail_second(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 2:
            raise OSError("disk full")
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(tempfile, "mkstemp", _fail_second)
    argv, out = _replay_out_argv(tmp_path)
    assert replay_main(argv) == 2
    assert "amw-replay: disk full" in capsys.readouterr().err
    assert attempts["count"] == 2
    assert list(out.iterdir()) == []


def test_cli_rename_failure_restores_previous_outputs(tmp_path, capsys, monkeypatch):
    import os

    argv, out = _replay_out_argv(tmp_path)
    out.mkdir(parents=True, exist_ok=True)
    old = {
        "receipts.jsonl": "old receipts\n",
        "summary.json": "old summary\n",
        "keys.json": "old keys\n",
    }
    for name, content in old.items():
        (out / name).write_bytes(content.encode("utf-8"))
    old_bytes = {name: (out / name).read_bytes() for name in old}

    real_replace = os.replace
    renames = {"count": 0}

    def _fail_second_temp_rename(src, dst):
        dst_path = Path(dst)
        src_str = str(src)
        if (
            dst_path.parent == out
            and dst_path.name in old
            and src_str.endswith(".tmp")
            and not src_str.endswith(".bak.tmp")
        ):
            renames["count"] += 1
            if renames["count"] == 2:
                raise OSError("rename failed")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _fail_second_temp_rename)
    assert replay_main(argv) == 2
    assert "amw-replay: rename failed" in capsys.readouterr().err
    assert renames["count"] == 2
    for name, data in old_bytes.items():
        assert (out / name).read_bytes() == data
    assert sorted(p.name for p in out.iterdir()) == sorted(old)


def test_cli_overwrite_prepopulated_out_writes_new_contents(tmp_path, capsys):
    argv, out = _replay_out_argv(tmp_path)
    out.mkdir(parents=True, exist_ok=True)
    old = {
        "receipts.jsonl": "old receipts\n",
        "summary.json": "old summary\n",
        "keys.json": "old keys\n",
    }
    for name, content in old.items():
        (out / name).write_text(content)
    assert replay_main(argv) == 0, capsys.readouterr().err
    assert sorted(p.name for p in out.iterdir()) == [
        "keys.json",
        "receipts.jsonl",
        "summary.json",
    ]
    for name, content in old.items():
        assert (out / name).read_text() != content
    summary = json.loads((out / "summary.json").read_text())
    assert summary["total"] == 6
    receipts = (out / "receipts.jsonl").read_text().splitlines()
    assert len(receipts) == 6


def test_cli_backup_link_failure_falls_back_to_copy(tmp_path, capsys, monkeypatch):
    import os

    argv, out = _replay_out_argv(tmp_path)
    out.mkdir(parents=True, exist_ok=True)
    old = {
        "receipts.jsonl": "old receipts\n",
        "summary.json": "old summary\n",
        "keys.json": "old keys\n",
    }
    for name, content in old.items():
        (out / name).write_text(content)

    def _no_link(_src, _dst):
        raise OSError("hardlinks unsupported")

    monkeypatch.setattr(os, "link", _no_link)
    assert replay_main(argv) == 0, capsys.readouterr().err
    assert sorted(p.name for p in out.iterdir()) == [
        "keys.json",
        "receipts.jsonl",
        "summary.json",
    ]
    for name, content in old.items():
        assert (out / name).read_text() != content


def test_cli_restore_failure_keeps_backup_and_mentions_it(
    tmp_path, capsys, monkeypatch
):
    import os

    argv, out = _replay_out_argv(tmp_path)
    out.mkdir(parents=True, exist_ok=True)
    old = {
        "receipts.jsonl": "old receipts\n",
        "summary.json": "old summary\n",
        "keys.json": "old keys\n",
    }
    for name, content in old.items():
        (out / name).write_bytes(content.encode("utf-8"))
    old_bytes = {name: (out / name).read_bytes() for name in old}

    real_replace = os.replace
    renames = {"count": 0}

    def _fail_temp_rename_then_restore(src, dst):
        dst_path = Path(dst)
        src_str = str(src)
        if (
            dst_path.parent == out
            and dst_path.name in old
            and src_str.endswith(".tmp")
            and not src_str.endswith(".bak.tmp")
        ):
            renames["count"] += 1
            if renames["count"] == 2:
                raise OSError("rename failed")
        if src_str.endswith(".bak.tmp") and dst_path.parent == out:
            raise OSError("restore failed")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _fail_temp_rename_then_restore)
    assert replay_main(argv) == 2
    err = capsys.readouterr().err
    assert renames["count"] == 2
    backups = list(out.glob("*.bak.tmp"))
    assert len(backups) == 1
    assert str(backups[0]) in err
    assert backups[0].is_file()
    assert backups[0].read_bytes() == old_bytes["receipts.jsonl"]
    assert sorted(p.name for p in out.glob("*.tmp")) == [backups[0].name]


def test_cli_copy_fallback_with_rename_failure_restores_previous_outputs(
    tmp_path, capsys, monkeypatch
):
    import os

    argv, out = _replay_out_argv(tmp_path)
    out.mkdir(parents=True, exist_ok=True)
    old = {
        "receipts.jsonl": "old receipts\n",
        "summary.json": "old summary\n",
        "keys.json": "old keys\n",
    }
    for name, content in old.items():
        (out / name).write_bytes(content.encode("utf-8"))
    old_bytes = {name: (out / name).read_bytes() for name in old}

    def _no_link(_src, _dst):
        raise OSError("hardlinks unsupported")

    monkeypatch.setattr(os, "link", _no_link)

    real_replace = os.replace
    renames = {"count": 0}

    def _fail_second_temp_rename(src, dst):
        dst_path = Path(dst)
        src_str = str(src)
        if (
            dst_path.parent == out
            and dst_path.name in old
            and src_str.endswith(".tmp")
            and not src_str.endswith(".bak.tmp")
        ):
            renames["count"] += 1
            if renames["count"] == 2:
                raise OSError("rename failed")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _fail_second_temp_rename)
    assert replay_main(argv) == 2
    assert "amw-replay: rename failed" in capsys.readouterr().err
    assert renames["count"] == 2
    for name, data in old_bytes.items():
        assert (out / name).read_bytes() == data
    assert sorted(p.name for p in out.iterdir()) == sorted(old)
    assert list(out.glob("*.tmp")) == []


def test_cli_non_oserror_with_failed_restore_reraises_with_note(tmp_path, monkeypatch):
    import os

    from app.replay.cli import _write_outputs_atomically

    out = tmp_path / "out"
    out.mkdir(parents=True, exist_ok=True)
    old = {
        "receipts.jsonl": "old receipts\n",
        "summary.json": "old summary\n",
        "keys.json": "old keys\n",
    }
    for name, content in old.items():
        (out / name).write_bytes(content.encode("utf-8"))

    real_replace = os.replace
    renames = {"count": 0}

    def _fail_temp_rename_then_restore(src, dst):
        dst_path = Path(dst)
        src_str = str(src)
        if (
            dst_path.parent == out
            and dst_path.name in old
            and src_str.endswith(".tmp")
            and not src_str.endswith(".bak.tmp")
        ):
            renames["count"] += 1
            if renames["count"] == 2:
                raise RuntimeError("boom")
        if src_str.endswith(".bak.tmp") and dst_path.parent == out:
            raise OSError("restore failed")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _fail_temp_rename_then_restore)
    with pytest.raises(RuntimeError, match="boom") as excinfo:
        _write_outputs_atomically(
            out,
            {
                "receipts.jsonl": "new receipts\n",
                "summary.json": "new summary\n",
                "keys.json": "new keys\n",
            },
        )
    assert type(excinfo.value) is RuntimeError
    assert renames["count"] == 2
    backups = list(out.glob("*.bak.tmp"))
    assert len(backups) == 1
    notes = getattr(excinfo.value, "__notes__", [])
    assert len(notes) == 1
    assert "rollback restore failed" in notes[0]
    assert str(backups[0]) in notes[0]
    assert sorted(p.name for p in out.glob("*.tmp")) == [backups[0].name]
