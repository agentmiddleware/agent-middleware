"""Tests for the local-only approvals router.

Every value comes from the real G1 gate recording
(integrations/goose-agent-middleware/tests/fixtures/g1_gate_calls.jsonl) or is
made at runtime (secrets.token_hex / uuid.uuid4). Nothing here is hand-invented.
"""

import json
import secrets
import sys
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for _path in (
    REPO_ROOT / "integrations" / "goose-agent-middleware" / "src",
    REPO_ROOT / "sdk" / "python" / "src",
):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from typing import Any  # noqa: E402

import pytest  # noqa: E402
from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from goose_amw import canonical as goose_canonical  # noqa: E402
from sqlalchemy import delete  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import app.routers.approvals as approvals_module  # noqa: E402
from app.db.approval_gate_models import ApprovalGateModel  # noqa: E402
from app.db.database import get_session_factory  # noqa: E402
from app.routers.approvals import (  # noqa: E402
    get_approval_store,
    require_loopback,
    router,
    set_approval_store_for_tests,
)
from app.services.approval_gate import (  # noqa: E402
    ApprovalRecord,
    DbApprovalStore,
    InMemoryApprovalStore,
)

FIXTURE = (
    REPO_ROOT
    / "integrations"
    / "goose-agent-middleware"
    / "tests"
    / "fixtures"
    / "g1_gate_calls.jsonl"
)
RECORDS = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
W = next(record for record in RECORDS if record["tool"] == "write_file")
TOOL = W["tool"]
SESSION = W["meta"]["agent-session-id"]
ARGS = {"path": W["path"]}  # the only argument field the recording holds


def _fresh_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def app() -> FastAPI:
    set_approval_store_for_tests(InMemoryApprovalStore())
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[require_loopback] = lambda: None
    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture
async def purge_approval_gate_table() -> Any:
    factory = get_session_factory()
    async with factory() as session:
        await session.execute(delete(ApprovalGateModel))
        await session.commit()
    yield
    async with factory() as session:
        await session.execute(delete(ApprovalGateModel))
        await session.commit()


def _seed() -> ApprovalRecord:
    now = time.time()
    record = ApprovalRecord(
        nonce=secrets.token_urlsafe(16),
        idempotency_key=f"amw-fb-{secrets.token_hex(32)}",
        args_hash=goose_canonical.args_hash(ARGS),
        principal=_fresh_id(),
        tool=TOOL,
        session_id=SESSION,
        created_at=now,
        deadline=now + 300,
    )
    get_approval_store().save(record)
    return record


def test_pending_lists_seeded_record(client: TestClient) -> None:
    record = _seed()
    response = client.get("/v1/approvals/pending")
    assert response.status_code == 200
    approvals = response.json()["approvals"]
    assert [item["nonce"] for item in approvals] == [record.nonce]
    assert approvals[0]["tool"] == TOOL
    assert approvals[0]["principal"] == record.principal
    assert approvals[0]["args_hash"] == goose_canonical.args_hash(ARGS)


def test_approve_flips_pending_to_approved(client: TestClient) -> None:
    record = _seed()
    response = client.post(f"/v1/approvals/{record.nonce}/approve")
    assert response.status_code == 200
    assert response.json() == {"nonce": record.nonce, "status": "approved"}
    assert client.get("/v1/approvals/pending").json() == {"approvals": []}


def test_deny_flips_pending_to_denied(client: TestClient) -> None:
    record = _seed()
    response = client.post(f"/v1/approvals/{record.nonce}/deny")
    assert response.status_code == 200
    assert response.json() == {"nonce": record.nonce, "status": "denied"}


def test_second_decision_is_404(client: TestClient) -> None:
    record = _seed()
    assert client.post(f"/v1/approvals/{record.nonce}/approve").status_code == 200
    assert client.post(f"/v1/approvals/{record.nonce}/deny").status_code == 404


def test_unknown_nonce_is_404(client: TestClient) -> None:
    unknown = _fresh_id()
    assert client.post(f"/v1/approvals/{unknown}/approve").status_code == 404
    assert client.post(f"/v1/approvals/{unknown}/deny").status_code == 404


def test_non_loopback_client_gets_403() -> None:
    set_approval_store_for_tests(InMemoryApprovalStore())
    application = FastAPI()
    application.include_router(router)
    client = TestClient(application)
    unknown = _fresh_id()
    assert client.get("/v1/approvals/pending").status_code == 403
    assert client.post(f"/v1/approvals/{unknown}/approve").status_code == 403
    assert client.post(f"/v1/approvals/{unknown}/deny").status_code == 403


def test_default_store_is_db_backed() -> None:
    import importlib

    try:
        importlib.reload(approvals_module)
        assert isinstance(approvals_module.get_approval_store(), DbApprovalStore)
    finally:
        approvals_module.set_approval_store_for_tests(InMemoryApprovalStore())


async def test_approve_through_http_decides_db_row(
    client: TestClient, clean_database: None, purge_approval_gate_table: None
) -> None:
    set_approval_store_for_tests(DbApprovalStore())
    record = _seed()
    response = client.post(f"/v1/approvals/{record.nonce}/approve")
    assert response.status_code == 200
    assert response.json() == {"nonce": record.nonce, "status": "approved"}
    assert client.get("/v1/approvals/pending").json() == {"approvals": []}
    stored = DbApprovalStore().get(record.nonce)
    assert stored is not None
    assert stored.status == "approved"


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_require_loopback_allows_loopback(host: str) -> None:
    request = Request({"type": "http", "client": (host, 1234), "headers": []})
    assert require_loopback(request) is None


@pytest.mark.parametrize(
    "host", ["10.0.0.1", "192.168.1.2", "example.com", "testclient", ""]
)
def test_require_loopback_rejects_remote(host: str) -> None:
    scope = {"type": "http", "headers": []}
    if host:
        scope["client"] = (host, 1234)
    else:
        scope["client"] = None
    with pytest.raises(HTTPException) as exc_info:
        require_loopback(Request(scope))
    assert exc_info.value.status_code == 403
