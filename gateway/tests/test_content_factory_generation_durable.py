"""Contract: Content Factory text generation (LLM + durable row)."""

import uuid

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.config import get_settings
from app.db.database import get_session_factory
from app.db.models import ContentFactoryGenerationModel
from app.main import app
from tests.test_trust_helpers import provision_agent_wallet

HEADERS = {"X-API-Key": "test-key"}


@pytest.fixture(autouse=True)
def _restore_content_factory_sim():
    settings = get_settings()
    saved_sim = settings.SIMULATION_MODE_CONTENT_FACTORY
    saved_key = settings.LLM_API_KEY
    saved_base_url = settings.LLM_BASE_URL
    yield
    settings.SIMULATION_MODE_CONTENT_FACTORY = saved_sim
    settings.LLM_API_KEY = saved_key
    settings.LLM_BASE_URL = saved_base_url


@pytest.mark.anyio
async def test_real_mode_persists_and_get_returns_row(monkeypatch):
    settings = get_settings()
    settings.SIMULATION_MODE_CONTENT_FACTORY = False
    settings.LLM_API_KEY = "sk-test"

    async def fake_llm(prompt: str, model: str | None = None):
        _ = prompt, model
        return "Generated output text", "gpt-test", "req-abc"

    monkeypatch.setattr(
        "app.services.content_factory_generation.openai_compatible_chat_completion",
        fake_llm,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/v1/content/generate",
            json={"prompt": "Tell me a joke"},
            headers=HEADERS,
        )
    assert r.status_code == 202
    body = r.json()
    assert body["text"] == "Generated output text"
    assert body["model"] == "gpt-test"
    assert body["prompt_hash"] is not None
    assert body["output_hash"] is not None
    assert body["provenance"]["request_id"] == "req-abc"
    cid = body["content_id"]

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        g = await client.get(f"/v1/content/{cid}", headers=HEADERS)
    assert g.status_code == 200
    got = g.json()
    assert got["output_hash"] == body["output_hash"]
    assert got["text"] == "Generated output text"

    factory = get_session_factory()
    async with factory() as session:
        row = (
            await session.execute(
                select(ContentFactoryGenerationModel).where(
                    ContentFactoryGenerationModel.content_id == cid
                )
            )
        ).scalar_one_or_none()
    assert row is not None
    assert row.output_text == "Generated output text"


@pytest.mark.anyio
async def test_simulation_mode_no_db_row():
    settings = get_settings()
    settings.SIMULATION_MODE_CONTENT_FACTORY = True
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/v1/content/generate",
            json={"prompt": "short prompt"},
            headers=HEADERS,
        )
    assert r.status_code == 202
    body = r.json()
    assert body["prompt_hash"] is None
    assert body["output_hash"] is None
    assert body["provenance"] is None
    assert "[simulated]" in body["text"]
    cid = body["content_id"]

    factory = get_session_factory()
    async with factory() as session:
        row = (
            await session.execute(
                select(ContentFactoryGenerationModel).where(
                    ContentFactoryGenerationModel.content_id == cid
                )
            )
        ).scalar_one_or_none()
    assert row is None


@pytest.mark.anyio
async def test_get_unknown_returns_404():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get(
            f"/v1/content/{uuid.uuid4()}",
            headers=HEADERS,
        )
    assert r.status_code == 404


# --------------------------------------------------------------------------
# Tenant isolation, model allowlist, and provider-error handling
# --------------------------------------------------------------------------


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _enable_real_mode(monkeypatch, text: str = "Generated output text") -> list:
    """Switch to real mode with a fake provider; return the models it saw."""
    settings = get_settings()
    settings.SIMULATION_MODE_CONTENT_FACTORY = False
    settings.LLM_API_KEY = "sk-test"
    seen_models: list = []

    async def fake_llm(prompt: str, model: str | None = None):
        _ = prompt
        seen_models.append(model)
        return text, model or settings.LLM_MODEL, "req-iso"

    monkeypatch.setattr(
        "app.services.content_factory_generation.openai_compatible_chat_completion",
        fake_llm,
    )
    return seen_models


@pytest.mark.proof
@pytest.mark.anyio
async def test_get_content_not_readable_across_tenants(
    client, clean_database, monkeypatch
):
    a = await provision_agent_wallet(client)
    b = await provision_agent_wallet(client)
    secret = "Tenant A confidential generation"
    _enable_real_mode(monkeypatch, text=secret)

    created = await client.post(
        "/v1/content/generate",
        json={"prompt": "A's private prompt"},
        headers=a["agent_headers"],
    )
    assert created.status_code == 202
    body = created.json()
    cid = body["content_id"]

    # Wallet B gets the same 404 for A's content as for an id that does not
    # exist: no existence oracle, no content, no owner wallet id.
    foreign = await client.get(f"/v1/content/{cid}", headers=b["agent_headers"])
    unknown = await client.get(
        f"/v1/content/{uuid.uuid4()}", headers=b["agent_headers"]
    )
    assert foreign.status_code == 404
    assert unknown.status_code == 404
    assert foreign.json() == unknown.json()
    assert secret not in foreign.text
    assert a["agent_wallet_id"] not in foreign.text
    assert "wallet_access_denied" not in foreign.text

    # The owner still reads its own row, with the provenance it was issued
    # (the stored owner is internal and never surfaces in the response).
    owned = await client.get(f"/v1/content/{cid}", headers=a["agent_headers"])
    assert owned.status_code == 200
    assert owned.json()["text"] == secret
    assert owned.json()["provenance"] == body["provenance"]

    # Bootstrap admin keeps cross-tenant read access.
    admin = await client.get(f"/v1/content/{cid}", headers=HEADERS)
    assert admin.status_code == 200
    assert admin.json()["text"] == secret


@pytest.mark.proof
@pytest.mark.anyio
async def test_admin_generated_content_not_readable_by_wallet_key(
    client, clean_database, monkeypatch
):
    b = await provision_agent_wallet(client)
    _enable_real_mode(monkeypatch, text="operator-only output")

    created = await client.post(
        "/v1/content/generate", json={"prompt": "operator"}, headers=HEADERS
    )
    assert created.status_code == 202
    cid = created.json()["content_id"]

    foreign = await client.get(f"/v1/content/{cid}", headers=b["agent_headers"])
    assert foreign.status_code == 404
    assert "operator-only output" not in foreign.text

    admin = await client.get(f"/v1/content/{cid}", headers=HEADERS)
    assert admin.status_code == 200


@pytest.mark.proof
@pytest.mark.anyio
async def test_wallet_key_cannot_choose_unlisted_model(
    client, clean_database, monkeypatch
):
    b = await provision_agent_wallet(client)
    seen_models = _enable_real_mode(monkeypatch)
    default_model = get_settings().LLM_MODEL

    refused = await client.post(
        "/v1/content/generate",
        json={"prompt": "hi", "model": "gpt-expensive"},
        headers=b["agent_headers"],
    )
    assert refused.status_code == 422
    assert refused.json()["detail"]["error"] == "model_not_allowed"
    # The provider was never called on the operator's LLM key.
    assert seen_models == []

    # The configured default (explicit or implied) is still allowed.
    for payload in ({"prompt": "hi", "model": default_model}, {"prompt": "hi"}):
        ok = await client.post(
            "/v1/content/generate", json=payload, headers=b["agent_headers"]
        )
        assert ok.status_code == 202
    assert seen_models == [default_model, None]

    # Bootstrap admin keeps the operator override.
    admin = await client.post(
        "/v1/content/generate",
        json={"prompt": "hi", "model": "gpt-expensive"},
        headers=HEADERS,
    )
    assert admin.status_code == 202
    assert seen_models[-1] == "gpt-expensive"


def _provider_status_500(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, json={"error": {"message": "upstream secret detail"}})


def _provider_status_401(request: httpx.Request) -> httpx.Response:
    return httpx.Response(401, json={"error": {"message": "upstream secret detail"}})


def _provider_unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("upstream secret detail", request=request)


def _provider_not_json(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text="<html>upstream secret detail</html>")


@pytest.mark.proof
@pytest.mark.anyio
@pytest.mark.parametrize(
    "handler",
    [
        _provider_status_500,
        _provider_status_401,
        _provider_unreachable,
        _provider_not_json,
    ],
)
async def test_provider_failure_returns_502_and_is_audited(
    clean_database, monkeypatch, handler
):
    settings = get_settings()
    settings.SIMULATION_MODE_CONTENT_FACTORY = False
    settings.LLM_API_KEY = "sk-test"
    settings.LLM_BASE_URL = "https://llm.internal.example/v1"

    real_async_client = httpx.AsyncClient

    def _mock_provider_client(*args, **kwargs):
        # Only the provider call builds a client without a transport; the
        # test's own ASGI client below passes one explicitly.
        kwargs.setdefault("transport", httpx.MockTransport(handler))
        return real_async_client(*args, **kwargs)

    audits: list[dict] = []

    def _capture_audit(event: str, **fields):
        audits.append({"event": event, **fields})

    monkeypatch.setattr(
        "app.services.content_factory_generation.httpx.AsyncClient",
        _mock_provider_client,
    )
    monkeypatch.setattr("app.routers.content_generation.record_audit", _capture_audit)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/v1/content/generate", json={"prompt": "hello"}, headers=HEADERS
        )

    assert r.status_code == 502
    assert r.json()["detail"]["error"] == "llm_unavailable"
    # Neither the provider URL nor the provider's response body leaks.
    assert "llm.internal.example" not in r.text
    assert "upstream secret detail" not in r.text
    outcomes = [
        a["outcome"] for a in audits if a["event"] == "content_factory.generate"
    ]
    assert outcomes == ["error"]

    factory = get_session_factory()
    async with factory() as session:
        rows = (
            (await session.execute(select(ContentFactoryGenerationModel)))
            .scalars()
            .all()
        )
    assert rows == []
