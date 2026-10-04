"""Public MCP endpoint (POST /mcp/public): unauthenticated read-only surface.

The SDK owns the protocol (initialize, framing, errors); these tests pin the
public contract layered on top — off-by-default gating, genuinely
credential-free access, origin validation, the hard read-only tool boundary
(no governed tool is reachable), and the verification verdict taxonomy:
"invalid" is a verdict, "unknown_key"/"malformed"/"keys_unavailable" are the
verifier's situation and must never be reported as tampering.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import get_settings
from app.db.database import get_session_factory
from app.db.models import SigningKeyModel, WalletModel
from app.main import app
from app.routers import mcp_public
from app.routers.mcp_public import PUBLIC_SERVER_NAME
from app.schemas.billing import ServiceCategory
from app.services.service_registry import get_service_registry
from app.services.signing_keys import (
    RECEIPT_CANONICALIZATION,
    canonical_json,
    get_signing_key_service,
)
from tests.conftest import iter_routes

# The SDK's streamable HTTP transport requires an explicit Accept header.
# Deliberately no auth headers anywhere in this file: credential-free access
# is the surface's contract.
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}

PUBLIC_PATH = "/mcp/public"

_PUBLIC_STATE_TABLES = (
    "wallets",
    "api_keys",
    "key_rotation_logs",
    "permits",
    "permit_requests",
    "quotes",
    "ledger_entries",
    "receipts",
    "mcp_dispatch_attempts",
    "idempotency_records",
    "signing_keys",
    "control_plane_audit_events",
    "audit_chain_heads",
    "service_registry",
    "policy_bundles",
    "human_approvals",
    "billing_alerts",
    "daily_balance_snapshots",
    "kyc_verifications",
    "refresh_tokens",
)


@pytest.fixture
async def client(clean_database):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def public_mcp_enabled(monkeypatch):
    monkeypatch.setenv("ENABLE_PUBLIC_MCP_ENDPOINT", "true")
    get_settings.cache_clear()
    yield
    monkeypatch.setenv("ENABLE_PUBLIC_MCP_ENDPOINT", "false")
    get_settings.cache_clear()


def _rpc(method: str, request_id: int | None = 1, params: dict | None = None) -> dict:
    body: dict = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if request_id is not None:
        body["id"] = request_id
    return body


def _initialize(protocol_version: str = "2025-06-18") -> dict:
    return _rpc(
        "initialize",
        params={
            "protocolVersion": protocol_version,
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "0.0.0"},
        },
    )


def _call(tool: str, arguments: dict | None = None) -> dict:
    return _rpc("tools/call", params={"name": tool, "arguments": arguments or {}})


def _registry_snapshot() -> tuple:
    registry = get_service_registry()
    records = tuple(
        sorted(
            (
                service_id,
                id(record),
                repr({key: value for key, value in record.items() if key != "func"}),
            )
            for service_id, record in registry._local_registry.items()
        )
    )
    functions = tuple(
        sorted(
            (service_id, id(func))
            for service_id, func in registry._func_registry.items()
        )
    )
    executors = tuple(
        sorted(
            (service_id, id(executor))
            for service_id, executor in registry._executor_registry.items()
        )
    )
    return records, functions, executors


async def _security_state_snapshot() -> dict:
    factory = get_session_factory()
    async with factory() as session:
        rows: dict[
            str,
            tuple[tuple[tuple[str, str], ...], ...],
        ] = {}
        for table in _PUBLIC_STATE_TABLES:
            columns = [
                row[1]
                for row in (
                    await session.execute(text(f"PRAGMA table_info({table})"))
                ).all()
            ]
            order = ", ".join(f'"{column}"' for column in columns)
            table_rows = (
                await session.execute(text(f'SELECT * FROM "{table}" ORDER BY {order}'))
            ).all()
            rows[table] = tuple(
                tuple(
                    ("null", "")
                    if value is None
                    else (type(value).__name__, repr(value))
                    for value in row
                )
                for row in table_rows
            )
        digest = hashlib.sha256(repr(sorted(rows.items())).encode("utf-8")).hexdigest()
    return {
        "database_digest": digest,
        "database_rows": rows,
        "registry": _registry_snapshot(),
    }


async def _seed_isolation_wallet() -> None:
    factory = get_session_factory()
    async with factory() as session:
        session.add(
            WalletModel(
                wallet_id="public-verifier-isolation-wallet",
                wallet_type="agent",
                balance=Decimal("37.5"),
            )
        )
        await session.commit()


def _unverifiable_bundle() -> dict:
    return {
        "receipt_id": "rcpt-no-key",
        "kid": "no-published-key",
        "alg": "Ed25519",
        "canonicalization": RECEIPT_CANONICALIZATION,
        "signing_input": json.dumps(
            {"receipt_id": "rcpt-no-key", "kid": "no-published-key"}
        ),
        "signature": base64.b64encode(b"\x00" * 64).decode(),
    }


async def _signed_bundle() -> dict:
    """Build a portable bundle signed by this plane's real signing service.

    Mirrors ``sign_payload_with_key_id``: the signed bytes are the canonical
    JSON of the payload plus ``alg``/``kid``/``payload_hash``, which is what
    ``signing_input`` carries in a real portable receipt.
    """
    payload = {
        "receipt_id": "rcpt-public-mcp-1",
        "tool": "portable-echo",
        "status": "success",
        "credits_charged": "1",
    }
    signature, kid, payload_hash = await get_signing_key_service().sign_payload(
        dict(payload)
    )
    signed = {**payload, "alg": "Ed25519", "kid": kid, "payload_hash": payload_hash}
    return {
        "schema_version": "1.0",
        "receipt_id": payload["receipt_id"],
        "issuer": "",
        "alg": "Ed25519",
        "kid": kid,
        "canonicalization": RECEIPT_CANONICALIZATION,
        "signing_input": canonical_json(signed),
        "signature": signature,
    }


@pytest.mark.anyio
async def test_endpoint_disabled_by_default(client):
    resp = await client.post(PUBLIC_PATH, json=_initialize(), headers=MCP_HEADERS)
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_initialize_needs_no_credentials(client, public_mcp_enabled):
    resp = await client.post(PUBLIC_PATH, json=_initialize(), headers=MCP_HEADERS)
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["serverInfo"]["name"] == PUBLIC_SERVER_NAME
    assert "read-only" in result["instructions"].lower()


@pytest.mark.anyio
async def test_get_and_delete_are_rejected(client, public_mcp_enabled):
    assert (await client.get(PUBLIC_PATH, headers=MCP_HEADERS)).status_code == 405
    assert (await client.delete(PUBLIC_PATH, headers=MCP_HEADERS)).status_code == 405


def test_public_mcp_has_no_generated_sibling_routes():
    routes = [
        route
        for route in iter_routes(app.routes)
        if getattr(route, "path", "") == PUBLIC_PATH
        or getattr(route, "path", "").startswith(PUBLIC_PATH + "/")
    ]

    assert {route.path for route in routes} == {PUBLIC_PATH}
    assert len(routes) == 2
    assert {
        (frozenset(route.methods or set()), route.include_in_schema) for route in routes
    } == {
        (frozenset({"POST"}), True),
        (frozenset({"GET", "DELETE"}), False),
    }


def test_app_imports_without_pytest_sdk_pythonpath():
    repo_root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(repo_root)

    completed = subprocess.run(
        [sys.executable, "-c", "from app.main import app"],
        cwd=repo_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


@pytest.mark.anyio
async def test_cross_origin_browser_call_is_refused(client, public_mcp_enabled):
    resp = await client.post(
        PUBLIC_PATH,
        json=_initialize(),
        headers={**MCP_HEADERS, "Origin": "https://evil.example"},
    )
    assert resp.status_code == 403


@pytest.mark.anyio
@pytest.mark.parametrize(
    "origin",
    ["https://test", "http://test:444"],
    ids=["different_scheme", "different_port"],
)
async def test_same_hostname_with_different_origin_is_refused(
    client, public_mcp_enabled, origin
):
    resp = await client.post(
        PUBLIC_PATH,
        json=_initialize(),
        headers={**MCP_HEADERS, "Origin": origin},
    )

    assert resp.status_code == 403


@pytest.mark.anyio
async def test_public_url_rejects_rebound_host(client, public_mcp_enabled, monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://api.example.com")
    get_settings.cache_clear()
    try:
        resp = await client.post(
            PUBLIC_PATH,
            json=_initialize(),
            headers={
                **MCP_HEADERS,
                "Host": "rebind.evil",
                "Origin": "https://rebind.evil",
            },
        )
    finally:
        monkeypatch.setenv("PUBLIC_URL", "")
        get_settings.cache_clear()

    assert resp.status_code == 421


@pytest.mark.anyio
async def test_public_url_rejects_proxy_observed_http_origin(
    client, public_mcp_enabled, monkeypatch
):
    monkeypatch.setenv("PUBLIC_URL", "https://api.example.com")
    get_settings.cache_clear()
    try:
        insecure = await client.post(
            PUBLIC_PATH,
            json=_initialize(),
            headers={
                **MCP_HEADERS,
                "Host": "api.example.com",
                "Origin": "http://api.example.com",
            },
        )
        canonical = await client.post(
            PUBLIC_PATH,
            json=_initialize(),
            headers={
                **MCP_HEADERS,
                "Host": "api.example.com",
                "Origin": "https://api.example.com",
            },
        )
    finally:
        monkeypatch.setenv("PUBLIC_URL", "")
        get_settings.cache_clear()

    assert insecure.status_code == 403
    assert canonical.status_code == 200


@pytest.mark.anyio
async def test_tools_list_is_exactly_the_read_only_surface(client, public_mcp_enabled):
    resp = await client.post(PUBLIC_PATH, json=_rpc("tools/list"), headers=MCP_HEADERS)
    tools = resp.json()["result"]["tools"]
    assert {t["name"] for t in tools} == {
        "verify_receipt",
        "get_trust_plane_info",
        "list_governed_tools",
    }
    for tool in tools:
        annotations = tool["annotations"]
        assert annotations["readOnlyHint"] is True
        assert annotations["destructiveHint"] is False
        assert annotations["openWorldHint"] is False
        assert tool["inputSchema"]["type"] == "object"
        assert tool["outputSchema"]["type"] == "object"
    info_schema = next(
        tool["outputSchema"] for tool in tools if tool["name"] == "get_trust_plane_info"
    )
    assert "positioning" in info_schema["properties"]
    assert "positioning" in info_schema["required"]
    assert "scope" in info_schema["properties"]["positioning"]["properties"]


@pytest.mark.anyio
@pytest.mark.parametrize("parallel", [False, True], ids=["sequential", "parallel"])
async def test_every_public_tool_preserves_security_and_economic_state(
    client, public_mcp_enabled, parallel
):
    """Public reads cannot provision keys, dispatch tools, or alter trust state."""
    await _seed_isolation_wallet()
    dispatch_count = 0

    async def sentinel_tool(message: str = "") -> dict[str, str]:
        nonlocal dispatch_count
        dispatch_count += 1
        return {"message": message}

    registry = get_service_registry()
    service_id = "public-isolation-sentinel"
    registry.unregister_local(service_id)
    registry.register_local(
        service_id=service_id,
        name="Public isolation sentinel",
        description="Must never be dispatched by the public verifier.",
        category=ServiceCategory.AGENT_COMMS,
        func=sentinel_tool,
        require_permit=True,
    )
    try:
        before = await _security_state_snapshot()
        requests = [
            _initialize(),
            _rpc("ping"),
            _rpc("tools/list"),
            _rpc("notifications/initialized", request_id=None),
            _call("verify_receipt", {"bundle": _unverifiable_bundle()}),
            _call("get_trust_plane_info"),
            _call("list_governed_tools"),
        ]
        if parallel:
            responses = await asyncio.gather(
                *(
                    client.post(PUBLIC_PATH, json=body, headers=MCP_HEADERS)
                    for body in requests * 8
                )
            )
        else:
            responses = []
            for body in requests:
                responses.append(
                    await client.post(PUBLIC_PATH, json=body, headers=MCP_HEADERS)
                )
                assert await _security_state_snapshot() == before

        structured = [
            result["structuredContent"]
            for response in responses
            if response.content
            for result in [response.json().get("result")]
            if isinstance(result, dict) and "structuredContent" in result
        ]
        assert any(item.get("status") == "keys_unavailable" for item in structured)
        assert any(item.get("keys_available") is False for item in structured)
        assert any(
            service_id in {tool["name"] for tool in item.get("tools", [])}
            for item in structured
        )
        assert dispatch_count == 0
        assert await _security_state_snapshot() == before
    finally:
        registry.unregister_local(service_id)


@pytest.mark.anyio
async def test_public_key_reads_do_not_reactivate_a_retired_key(
    client, public_mcp_enabled
):
    service = get_signing_key_service()
    key = await service.ensure_active_key()
    await service.retire_key_metadata(key.key_id)
    before = await _security_state_snapshot()

    verify = await client.post(
        PUBLIC_PATH,
        json=_call("verify_receipt", {"bundle": _unverifiable_bundle()}),
        headers=MCP_HEADERS,
    )
    info = await client.post(
        PUBLIC_PATH,
        json=_call("get_trust_plane_info"),
        headers=MCP_HEADERS,
    )

    assert verify.status_code == 200
    assert info.status_code == 200
    persisted = await service.get_public_key(key.key_id)
    assert persisted is not None
    assert persisted.status == "retired"
    assert await _security_state_snapshot() == before


@pytest.mark.anyio
async def test_parallel_valid_verification_preserves_state(
    client, public_mcp_enabled, monkeypatch
):
    bundle = await _signed_bundle()
    service = get_signing_key_service()
    published_key = await service.get_public_key(bundle["kid"])
    assert published_key is not None

    async def fixed_key_snapshot():
        return [published_key]

    # The mixed parallel isolation test above exercises real DB reads. Keep
    # this stress case focused on 32 simultaneous Ed25519 verifications
    # without coupling it to pytest's cross-event-loop SQLite pool.
    monkeypatch.setattr(service, "list_public_keys", fixed_key_snapshot)
    before = await _security_state_snapshot()

    responses = await asyncio.gather(
        *(
            client.post(
                PUBLIC_PATH,
                json=_call("verify_receipt", {"bundle": bundle}),
                headers=MCP_HEADERS,
            )
            for _ in range(32)
        )
    )

    assert {
        response.json()["result"]["structuredContent"]["status"]
        for response in responses
    } == {"verified"}
    assert await _security_state_snapshot() == before


@pytest.mark.anyio
async def test_every_governed_tool_name_is_rejected_without_dispatch(
    client, public_mcp_enabled
):
    dispatch_count = 0

    async def sentinel_tool() -> dict[str, bool]:
        nonlocal dispatch_count
        dispatch_count += 1
        return {"called": True}

    registry = get_service_registry()
    service_id = "public-governed-call-sentinel"
    registry.unregister_local(service_id)
    registry.register_local(
        service_id=service_id,
        name="Governed call sentinel",
        description="Must remain unreachable from public MCP.",
        category=ServiceCategory.AGENT_COMMS,
        func=sentinel_tool,
        require_permit=True,
    )
    try:
        tool_names = sorted(registry._local_registry)
        assert service_id in tool_names
        assert set(tool_names).isdisjoint(mcp_public._TOOL_HANDLERS)
        for tool_name in tool_names:
            resp = await client.post(
                PUBLIC_PATH,
                json=_call(tool_name),
                headers=MCP_HEADERS,
            )
            assert resp.json()["error"]["code"] == -32602
        assert dispatch_count == 0
    finally:
        registry.unregister_local(service_id)


@pytest.mark.anyio
async def test_verify_receipt_verifies_a_genuine_bundle(client, public_mcp_enabled):
    bundle = await _signed_bundle()
    resp = await client.post(
        PUBLIC_PATH,
        json=_call("verify_receipt", {"bundle": bundle}),
        headers=MCP_HEADERS,
    )
    result = resp.json()["result"]
    assert result.get("isError") is not True
    structured = result["structuredContent"]
    assert structured["ok"] is True
    assert structured["status"] == "verified"
    assert structured["receipt_id"] == "rcpt-public-mcp-1"
    assert structured["kid"] == bundle["kid"]
    assert structured["claims"]["tool"] == "portable-echo"
    assert result["content"][0]["text"].startswith("VERIFIED")


@pytest.mark.anyio
async def test_expected_issuer_accepts_a_matching_issuer(client, public_mcp_enabled):
    bundle = await _signed_bundle()
    resp = await client.post(
        PUBLIC_PATH,
        json=_call(
            "verify_receipt",
            {"bundle": bundle, "expected_issuer": bundle["issuer"]},
        ),
        headers=MCP_HEADERS,
    )

    assert resp.json()["result"]["structuredContent"]["status"] == "verified"


@pytest.mark.anyio
async def test_expected_issuer_mismatch_is_not_a_signature_verdict(
    client, public_mcp_enabled
):
    bundle = await _signed_bundle()
    resp = await client.post(
        PUBLIC_PATH,
        json=_call(
            "verify_receipt",
            {"bundle": bundle, "expected_issuer": "https://other.example"},
        ),
        headers=MCP_HEADERS,
    )

    structured = resp.json()["result"]["structuredContent"]
    assert structured["status"] == "mismatch"
    assert structured["ok"] is False
    assert (
        "does not mean the signature failed"
        in resp.json()["result"]["content"][0]["text"]
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("bundle", "expected_status"),
    [
        ("not-json", "malformed"),
        # The unsupported cases must be declared inside the SIGNED bytes.
        # Reading them from the envelope is what let an edited byte downgrade a
        # genuine receipt to "this verifier is too old", so the envelope is no
        # longer trusted for that decision -- which also means an envelope-only
        # bad alg is no longer cheaply rejectable, by design.
        (
            {
                **_unverifiable_bundle(),
                "signing_input": json.dumps(
                    {
                        "receipt_id": "rcpt-no-key",
                        "kid": "no-published-key",
                        "alg": "RSA",
                    }
                ),
            },
            "unsupported",
        ),
        (
            {
                **_unverifiable_bundle(),
                "signing_input": json.dumps(
                    {
                        "receipt_id": "rcpt-no-key",
                        "kid": "no-published-key",
                        "canonicalization": "unknown/1",
                    }
                ),
            },
            "unsupported",
        ),
    ],
)
async def test_preflight_failures_do_not_touch_the_key_store(
    client, public_mcp_enabled, monkeypatch, bundle, expected_status
):
    async def fail_if_called():
        raise AssertionError("preflight failure queried the signing-key store")

    monkeypatch.setattr(
        get_signing_key_service(),
        "list_public_keys",
        fail_if_called,
    )
    resp = await client.post(
        PUBLIC_PATH,
        json=_call("verify_receipt", {"bundle": bundle}),
        headers=MCP_HEADERS,
    )

    assert resp.json()["result"]["structuredContent"]["status"] == expected_status


@pytest.mark.anyio
async def test_key_store_failure_is_retryable_not_tampering(
    client, public_mcp_enabled, monkeypatch
):
    async def unavailable():
        raise SQLAlchemyError("transient signing-key store failure")

    monkeypatch.setattr(
        get_signing_key_service(),
        "list_public_keys",
        unavailable,
    )
    resp = await client.post(
        PUBLIC_PATH,
        json=_call("verify_receipt", {"bundle": _unverifiable_bundle()}),
        headers=MCP_HEADERS,
    )

    structured = resp.json()["result"]["structuredContent"]
    assert structured["status"] == "keys_unavailable"
    assert structured["ok"] is False
    assert "not a verdict" in resp.json()["result"]["content"][0]["text"].lower()


@pytest.mark.anyio
async def test_disabled_key_is_not_available_to_public_verifier(
    client, public_mcp_enabled
):
    bundle = await _signed_bundle()
    factory = get_session_factory()
    async with factory() as session:
        stored = await session.get(SigningKeyModel, bundle["kid"])
        assert stored is not None
        stored.status = "disabled"
        session.add(stored)
        await session.commit()

    resp = await client.post(
        PUBLIC_PATH,
        json=_call("verify_receipt", {"bundle": bundle}),
        headers=MCP_HEADERS,
    )

    structured = resp.json()["result"]["structuredContent"]
    assert structured["status"] == "keys_unavailable"
    assert structured["ok"] is False


@pytest.mark.anyio
async def test_verify_receipt_rejects_a_tampered_bundle(client, public_mcp_enabled):
    bundle = await _signed_bundle()
    await get_signing_key_service().ensure_active_key()
    payload = json.loads(bundle["signing_input"])
    payload["credits_charged"] = "999999"
    forged = {**bundle, "signing_input": canonical_json(payload)}
    resp = await client.post(
        PUBLIC_PATH,
        json=_call("verify_receipt", {"bundle": forged}),
        headers=MCP_HEADERS,
    )
    structured = resp.json()["result"]["structuredContent"]
    assert structured["ok"] is False
    assert structured["status"] == "invalid"
    assert structured["claims"] == {}


@pytest.mark.anyio
async def test_unknown_key_is_not_reported_as_tampering(client, public_mcp_enabled):
    await get_signing_key_service().ensure_active_key()
    # A key this plane genuinely does not publish, named inside the SIGNED
    # bytes, is the real "unknown key" case.
    unknown = await _signed_bundle()
    payload = json.loads(unknown["signing_input"])
    payload["kid"] = "kid-that-does-not-exist"
    unknown["signing_input"] = json.dumps(payload)
    unknown["kid"] = "kid-that-does-not-exist"
    resp = await client.post(
        PUBLIC_PATH,
        json=_call("verify_receipt", {"bundle": unknown}),
        headers=MCP_HEADERS,
    )
    structured = resp.json()["result"]["structuredContent"]
    assert structured["ok"] is False
    assert structured["status"] == "unknown_key"
    text = resp.json()["result"]["content"][0]["text"]
    assert "not evidence of tampering" in text.lower()


@pytest.mark.anyio
async def test_relabelled_envelope_kid_is_a_mismatch_not_an_unknown_key(
    client, public_mcp_enabled
):
    """Editing only the envelope must not look like a key-distribution problem.

    The signed payload still names the real key, so this plane holds it and the
    receipt is genuine. Reporting UNKNOWN_KEY here would let anyone in the
    transport path turn a valid receipt into "refresh your key set" with one
    edited field -- an availability story for what is actually an altered
    bundle.
    """
    bundle = await _signed_bundle()
    await get_signing_key_service().ensure_active_key()
    relabeled = {**bundle, "kid": "kid-that-does-not-exist"}
    resp = await client.post(
        PUBLIC_PATH,
        json=_call("verify_receipt", {"bundle": relabeled}),
        headers=MCP_HEADERS,
    )
    structured = resp.json()["result"]["structuredContent"]
    assert structured["ok"] is False
    assert structured["status"] == "mismatch"


@pytest.mark.anyio
async def test_verify_receipt_without_bundle_is_invalid_params(
    client, public_mcp_enabled
):
    resp = await client.post(
        PUBLIC_PATH, json=_call("verify_receipt", {}), headers=MCP_HEADERS
    )
    assert resp.json()["error"]["code"] == -32602


@pytest.mark.anyio
@pytest.mark.parametrize("field", ["signing_input", "garbage"])
async def test_oversized_http_body_is_rejected_before_mcp_parsing(
    client, public_mcp_enabled, field
):
    bundle = await _signed_bundle()
    bloated = {**bundle, field: "x" * (256 * 1024 + 1)}
    resp = await client.post(
        PUBLIC_PATH,
        json=_call("verify_receipt", {"bundle": bloated}),
        headers=MCP_HEADERS,
    )
    assert resp.status_code == 413


@pytest.mark.anyio
async def test_chunked_oversized_body_is_rejected(client, public_mcp_enabled):
    body = json.dumps(
        _call("verify_receipt", {"bundle": {"garbage": "x" * (256 * 1024 + 1)}})
    ).encode()

    async def chunks():
        for offset in range(0, len(body), 8192):
            yield body[offset : offset + 8192]

    resp = await client.post(
        PUBLIC_PATH,
        content=chunks(),
        headers=MCP_HEADERS,
    )
    assert resp.status_code == 413


@pytest.mark.anyio
async def test_extreme_json_nesting_is_rejected_without_poisoning_transport(
    client, public_mcp_enabled
):
    depth = 1100
    body = (
        '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":'
        '{"name":"verify_receipt","arguments":{"bundle":{"signing_input":'
        + "[" * depth
        + "0"
        + "]" * depth
        + "}}}}"
    ).encode()

    nested = await client.post(PUBLIC_PATH, content=body, headers=MCP_HEADERS)
    ping = await client.post(PUBLIC_PATH, json=_rpc("ping"), headers=MCP_HEADERS)

    assert nested.status_code == 200
    assert nested.json()["error"]["code"] == -32602
    assert ping.status_code == 200
    assert ping.json()["result"] == {}


@pytest.mark.anyio
async def test_governed_tools_are_not_callable_here(client, public_mcp_enabled):
    resp = await client.post(
        PUBLIC_PATH,
        json=_call("partner.notes.write", {"note": "hi"}),
        headers=MCP_HEADERS,
    )
    error = resp.json()["error"]
    assert error["code"] == -32602
    assert "read-only" in error["message"]


@pytest.mark.anyio
async def test_get_trust_plane_info_reports_published_keys(client, public_mcp_enabled):
    await get_signing_key_service().ensure_active_key()
    listed = await client.post(
        PUBLIC_PATH, json=_rpc("tools/list"), headers=MCP_HEADERS
    )
    info_schema = next(
        tool["outputSchema"]
        for tool in listed.json()["result"]["tools"]
        if tool["name"] == "get_trust_plane_info"
    )
    resp = await client.post(
        PUBLIC_PATH, json=_call("get_trust_plane_info"), headers=MCP_HEADERS
    )
    structured = resp.json()["result"]["structuredContent"]
    assert set(info_schema["required"]) <= structured.keys()
    assert structured["alg"] == "Ed25519"
    assert structured["keys_available"] is True
    assert structured["positioning"]["schema_version"] == "1.0"
    assert structured["positioning"]["id"] == (
        "transaction_integrity_for_consequential_autonomous_actions"
    )
    assert structured["positioning"]["scope"]["transaction_state_machine"] == (
        "configured_upstream_mcp_tool_only"
    )
    assert "delivery_uncertain" in structured["description"]
    assert structured["keys"], "a freshly deployed plane must publish a key"
    for key in structured["keys"]:
        assert key["kid"]
        assert key["status"] != "disabled"
    assert structured["endpoints"]["trust_keys"] == "/.well-known/trust-keys.json"


@pytest.mark.anyio
async def test_list_governed_tools_paginates_without_schemas(
    client, public_mcp_enabled
):
    resp = await client.post(
        PUBLIC_PATH,
        json=_call("list_governed_tools", {"limit": 1}),
        headers=MCP_HEADERS,
    )
    structured = resp.json()["result"]["structuredContent"]
    assert structured["count"] <= 1
    assert structured["total"] >= structured["count"]
    assert structured["has_more"] == (structured["total"] > structured["count"])
    for entry in structured["tools"]:
        assert set(entry) == {
            "name",
            "description",
            "category",
            "credits_per_call",
            "unit_name",
            "requires_permit",
        }


@pytest.mark.anyio
async def test_list_governed_tools_rejects_unknown_category(client, public_mcp_enabled):
    resp = await client.post(
        PUBLIC_PATH,
        json=_call("list_governed_tools", {"category": "not-a-category"}),
        headers=MCP_HEADERS,
    )
    error = resp.json()["error"]
    assert error["code"] == -32602
    assert "Valid categories" in error["message"]


@pytest.mark.anyio
async def test_ping_and_notifications_need_no_credentials(client, public_mcp_enabled):
    ping = await client.post(PUBLIC_PATH, json=_rpc("ping"), headers=MCP_HEADERS)
    assert ping.json()["result"] == {}
    note = await client.post(
        PUBLIC_PATH,
        json=_rpc("notifications/initialized", request_id=None),
        headers=MCP_HEADERS,
    )
    assert note.status_code == 202
