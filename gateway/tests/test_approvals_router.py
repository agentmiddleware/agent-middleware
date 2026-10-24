"""Data-free tests for the local-only approvals router."""

import time

import pytest
from fastapi import FastAPI, HTTPException, Request
from starlette.testclient import TestClient

from app.routers.approvals import (
    get_approval_store,
    require_loopback,
    router,
    set_approval_store_for_tests,
)
from app.services.approval_gate import ApprovalRecord, InMemoryApprovalStore


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


def _seed(nonce: str = "nonce-1") -> ApprovalRecord:
    record = ApprovalRecord(
        nonce=nonce,
        idempotency_key="key-1",
        args_hash="sha256:abc",
        principal="caller-a",
        tool="tool-x",
        session_id="session-1",
        created_at=time.time(),
        deadline=time.time() + 300,
    )
    get_approval_store().save(record)
    return record


def test_pending_lists_seeded_record(client: TestClient) -> None:
    _seed()
    response = client.get("/v1/approvals/pending")
    assert response.status_code == 200
    approvals = response.json()["approvals"]
    assert [item["nonce"] for item in approvals] == ["nonce-1"]
    assert approvals[0]["tool"] == "tool-x"


def test_approve_flips_pending_to_approved(client: TestClient) -> None:
    _seed()
    response = client.post("/v1/approvals/nonce-1/approve")
    assert response.status_code == 200
    assert response.json() == {"nonce": "nonce-1", "status": "approved"}
    assert client.get("/v1/approvals/pending").json() == {"approvals": []}


def test_deny_flips_pending_to_denied(client: TestClient) -> None:
    _seed()
    response = client.post("/v1/approvals/nonce-1/deny")
    assert response.status_code == 200
    assert response.json() == {"nonce": "nonce-1", "status": "denied"}


def test_second_decision_is_404(client: TestClient) -> None:
    _seed()
    assert client.post("/v1/approvals/nonce-1/approve").status_code == 200
    assert client.post("/v1/approvals/nonce-1/deny").status_code == 404


def test_unknown_nonce_is_404(client: TestClient) -> None:
    assert client.post("/v1/approvals/nope/approve").status_code == 404
    assert client.post("/v1/approvals/nope/deny").status_code == 404


def test_non_loopback_client_gets_403() -> None:
    set_approval_store_for_tests(InMemoryApprovalStore())
    application = FastAPI()
    application.include_router(router)
    client = TestClient(application)
    assert client.get("/v1/approvals/pending").status_code == 403
    assert client.post("/v1/approvals/nonce-1/approve").status_code == 403
    assert client.post("/v1/approvals/nonce-1/deny").status_code == 403


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
