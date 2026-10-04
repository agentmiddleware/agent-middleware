"""The live stress script must fail its run when a sub-test observes a failure.

Each case drives one sub-test of ``scripts/stress_test_live.py`` against an
in-process fake of the trust plane (``httpx.MockTransport``) that answers the
way the real server does, then injects one fault. A healthy answer must keep
the run green; a faulty one must make ``main()`` return non-zero instead of
printing a failure marker under an "ALL STRESS TESTS PASSED" banner.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import subprocess
import sys
from decimal import Decimal, InvalidOperation

import httpx
import pytest

from scripts import stress_test_live as stress

SUB_TESTS = (
    "test_budget_exhaustion",
    "test_expired_permit",
    "test_concurrent_permit_creation",
    "test_concurrent_governed_invokes",
    "test_unicode_payload",
    "test_tampered_permit",
    "test_cross_wallet_access",
    "test_timezone_extremes",
    "test_decimal_precision",
    "test_rapid_fire_idempotency",
    "test_permit_reuse_after_replay",
    "test_health_under_load",
)
AGENT_BALANCE = Decimal("1000")
NOTE_COST = Decimal("2")
MAX_NOTE_CHARS = 2000


class FakeTrustPlane:
    """Answers permit and tools/call requests the way the live server does."""

    def __init__(self, fault: str | None = None) -> None:
        self.fault = fault
        self.permits: dict[str, dict] = {}
        self.receipts_by_key: dict[str, str] = {}
        self._ids = itertools.count(1)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/permits":
            return self._create_permit(json.loads(request.content))
        if request.method == "GET" and request.url.path.startswith("/v1/permits/"):
            permit = self.permits[request.url.path.rsplit("/", 1)[-1]]
            spent = permit["spent"]
            if self.fault == "reuse-spend-drift":
                spent -= NOTE_COST
            return httpx.Response(
                200, json={"permit_id": permit["id"], "spent_credits": f"{spent:.8f}"}
            )
        if request.method == "POST" and request.url.path == "/mcp/messages":
            return self._call_tool(
                json.loads(request.content), request.headers["Idempotency-Key"]
            )
        return httpx.Response(404, json={"detail": "not found"})

    def _create_permit(self, body: dict) -> httpx.Response:
        expires_at = body["expires_at"]
        if self.fault == "timezone-rejected" and expires_at.endswith("+14:00"):
            return httpx.Response(422, json={"detail": "bad offset"})
        try:
            max_credits = Decimal(body["max_credits"])
        except (InvalidOperation, TypeError):
            return httpx.Response(422, json={"detail": [{"type": "decimal_parsing"}]})
        if max_credits <= 0 and self.fault != "nonpositive-permit-minted":
            return httpx.Response(400, json={"detail": "max_credits_must_be_positive"})
        if max_credits > AGENT_BALANCE:
            return httpx.Response(
                400, json={"detail": "permit_budget_exceeds_wallet_balance"}
            )
        permit_id = f"permit-{next(self._ids)}"
        self.permits[permit_id] = {
            "id": permit_id,
            "max": max_credits,
            "spent": Decimal("0"),
        }
        return httpx.Response(
            201,
            json={
                "permit_id": permit_id,
                "max_credits": str(max_credits),
                "expires_at": expires_at,
            },
        )

    def _receipt(self, permit_id: str, charged: Decimal) -> dict:
        return {
            "receipt_id": f"rcpt-{next(self._ids)}",
            "permit_id": permit_id,
            "credits_charged": str(charged),
        }

    def _call_tool(self, body: dict, idempotency_key: str) -> httpx.Response:
        params = body["params"]
        permit = self.permits[params["mcpContext"]["permit_id"]]
        text = params["arguments"]["text"]
        rpc = {"jsonrpc": "2.0", "id": body["id"]}

        cached = self.receipts_by_key.get(idempotency_key)
        if cached and self.fault != "replay-new-receipt":
            return httpx.Response(
                200, json={**rpc, "result": {"receipt": {"receipt_id": cached}}}
            )
        if self.fault == "emoji-rejected" and "🔥" in text:
            return httpx.Response(
                200, json={**rpc, "error": {"code": -32602, "message": "bad text"}}
            )
        if len(text) > MAX_NOTE_CHARS:
            charged = NOTE_COST if self.fault == "oversized-charged" else Decimal(0)
            permit["spent"] += charged
            return httpx.Response(
                200,
                json={
                    **rpc,
                    "error": {
                        "code": -32603,
                        "message": "text exceeds max length of 2000 characters",
                        "data": {"receipt": self._receipt(permit["id"], charged)},
                    },
                },
            )
        if permit["spent"] + NOTE_COST > permit["max"]:
            return httpx.Response(
                200,
                json={
                    **rpc,
                    "error": {
                        "code": -32003,
                        "message": "permit_budget_exceeded",
                        "data": {"receipt": self._receipt(permit["id"], Decimal(0))},
                    },
                },
            )
        permit["spent"] += NOTE_COST
        receipt = self._receipt(permit["id"], NOTE_COST)
        self.receipts_by_key[idempotency_key] = receipt["receipt_id"]
        return httpx.Response(200, json={**rpc, "result": {"receipt": receipt}})


def _run_stress(
    monkeypatch: pytest.MonkeyPatch,
    *,
    keep: tuple[str, ...],
    fault: str | None = None,
) -> int:
    fake = FakeTrustPlane(fault)
    real_async_client = httpx.AsyncClient

    def mock_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return real_async_client(
            *args, transport=httpx.MockTransport(fake.handle), **kwargs
        )

    async def fake_setup_wallets() -> tuple[str, str]:
        return "spn-stress", "agt-stress"

    async def no_op(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setenv("AGENT_MIDDLEWARE_API_KEY", "stress-test-key")
    monkeypatch.setattr(stress.httpx, "AsyncClient", mock_client)
    # A fresh semaphore per run: asyncio primitives bind to the first loop
    # that waits on them, and each run gets its own loop.
    monkeypatch.setattr(stress, "SEM", asyncio.Semaphore(10))
    monkeypatch.setattr(stress, "setup_wallets", fake_setup_wallets)
    for name in SUB_TESTS:
        if name not in keep:
            monkeypatch.setattr(stress, name, no_op)

    return asyncio.run(stress.main(["--api-url", "http://127.0.0.1:8000"]))


VERDICT_SUB_TESTS = (
    "test_concurrent_governed_invokes",
    "test_unicode_payload",
    "test_timezone_extremes",
    "test_decimal_precision",
    "test_rapid_fire_idempotency",
    "test_permit_reuse_after_replay",
)


@pytest.mark.parametrize("sub_test", VERDICT_SUB_TESTS)
def test_healthy_server_keeps_each_sub_test_green(
    sub_test: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run_stress(monkeypatch, keep=(sub_test,)) == 0
    out = capsys.readouterr().out
    assert "❌" not in out
    assert "ALL STRESS TESTS PASSED" in out


def test_healthy_server_passes_all_verdict_sub_tests_together(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run_stress(monkeypatch, keep=VERDICT_SUB_TESTS) == 0
    assert "ALL STRESS TESTS PASSED" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("sub_test", "fault"),
    [
        pytest.param("test_unicode_payload", "emoji-rejected", id="unicode-error"),
        pytest.param("test_unicode_payload", "oversized-charged", id="refusal-charged"),
        pytest.param(
            "test_timezone_extremes", "timezone-rejected", id="timezone-rejected"
        ),
        pytest.param(
            "test_decimal_precision",
            "nonpositive-permit-minted",
            id="negative-max-credits-minted",
        ),
        pytest.param(
            "test_rapid_fire_idempotency",
            "replay-new-receipt",
            id="replay-receipts-vary",
        ),
        pytest.param(
            "test_permit_reuse_after_replay",
            "reuse-spend-drift",
            id="spent-credits-drift",
        ),
    ],
)
def test_observed_failure_fails_the_run(
    sub_test: str,
    fault: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run_stress(monkeypatch, keep=(sub_test,), fault=fault) != 0
    out = capsys.readouterr().out
    assert "ALL STRESS TESTS PASSED" not in out
    assert "STRESS TEST FAILED" in out


@pytest.mark.parametrize(
    "sub_test", ["test_concurrent_governed_invokes", "test_rapid_fire_idempotency"]
)
@pytest.mark.parametrize(
    "status,body",
    [
        (500, {"detail": "synthetic failure"}),
        (409, {"detail": "in progress"}),
        (200, []),
        (200, {"result": {}}),
        (200, {"jsonrpc": "2.0", "id": 0, "result": None}),
        (200, {"jsonrpc": "2.0", "id": 0, "error": {"message": "denied"}}),
        (200, {"jsonrpc": "2.0", "id": 0, "result": {"receipt": {"receipt_id": ""}}}),
        (200, {"jsonrpc": "2.0", "id": 0, "result": {"receipt": {"receipt_id": 12}}}),
        (200, {"jsonrpc": "2.0", "id": 0, "result": {"receipt": {"receipt_id": " "}}}),
    ],
)
@pytest.mark.parametrize("partial", [False, True])
def test_invokes_reject_missing_success_evidence(
    monkeypatch, sub_test, status, body, partial
):
    calls = 0

    async def request(method, path, **kwargs):
        nonlocal calls
        if path == "/v1/permits":
            return httpx.Response(201, json={"permit_id": "synthetic-permit"})
        calls += 1
        if partial and calls > 1:
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 0,
                    "result": {"receipt": {"receipt_id": "synthetic-receipt"}},
                },
            )
        return httpx.Response(status, json=body)

    monkeypatch.setattr(stress, "req", request)
    with pytest.raises(AssertionError):
        asyncio.run(getattr(stress, sub_test)("sponsor", "agent"))


def test_rapid_replays_send_identical_payloads(monkeypatch):
    payloads = []

    async def request(method, path, **kwargs):
        if path == "/v1/permits":
            return httpx.Response(201, json={"permit_id": "synthetic-permit"})
        payloads.append(kwargs["json"])
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 0,
                "result": {"receipt": {"receipt_id": "synthetic-receipt"}},
            },
        )

    monkeypatch.setattr(stress, "req", request)
    asyncio.run(stress.test_rapid_fire_idempotency("sponsor", "agent"))
    assert len(payloads) == 50
    assert all(payload == payloads[0] for payload in payloads)


@pytest.mark.parametrize(
    "content", [b"invalid JSON", b'{"result":{"receipt":{"receipt_id":"x"}}}']
)
def test_success_evidence_rejects_invalid_rpc(content):
    with pytest.raises(AssertionError):
        stress.successful_receipt_id(httpx.Response(200, content=content))


def test_invoke_checks_still_fail_under_optimized_python():
    result = subprocess.run(
        [
            sys.executable,
            "-O",
            "-c",
            """
import asyncio
import httpx
from scripts import stress_test_live as stress
async def request(method, path, **kwargs):
    if path == '/v1/permits':
        return httpx.Response(201, json={'permit_id': 'synthetic'})
    return httpx.Response(500, json={'detail': 'failure'})
stress.req = request
for check in (stress.test_concurrent_governed_invokes, stress.test_rapid_fire_idempotency):
    try:
        asyncio.run(check('sponsor', 'agent'))
    except AssertionError:
        continue
    raise SystemExit('check accepted HTTP failure under -O')
""",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
