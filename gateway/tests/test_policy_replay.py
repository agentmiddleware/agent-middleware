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
from app.services.signing_keys import get_signing_key_service
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
        else:
            assert claims["change"] == "unchanged", source
            assert claims["new_decision"] == claims["old_decision"]


def test_only_targeted_calls_flip_untargeted_rules_change_nothing(tmp_path):
    calls = _calls()
    for document in (
        # The constraints these calls were recorded under.
        {"allowed_tools": ["golden-path-echo"], "max_cost_per_action": "2.5"},
        # A rule scoped to a different wallet never touches these calls.
        {"wallet_id": "agt-other", "allowed_tools": ["something-else"]},
    ):
        result = replay_calls(calls, load_policy_bundle(_bundle(tmp_path, document)))
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
    assert summary["allow_to_deny"] == changes.count("allow_to_deny") == 3
    assert summary["deny_to_allow"] == changes.count("deny_to_allow") == 0
    assert summary["unchanged"] == changes.count("unchanged") == 3
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
