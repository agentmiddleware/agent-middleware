"""Focused tests for app.main (application assembly).

Covers the parts of gateway/app/main.py that had no dedicated coverage:
the non-finite-float safe validation-error handler, its ``_json_safe_numbers``
helper, the signing-key remediation map, the assembled app metadata, and the
root content-negotiation edge where ``text/html`` is present alongside
``application/json``. Router mounting, CORS branches, and the health endpoints
already have their own files (test_proof_surface_gate, test_cors_security,
test_health_*, test_readiness_fail_closed) and are not repeated here.
"""

from __future__ import annotations

import json

import pytest
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from app.core.config import get_settings
from app.main import (
    _SIGNING_KEY_REMEDIATION,
    _SIGNING_KEY_REMEDIATION_DEFAULT,
    _json_safe_numbers,
    app,
    public_contact,
    request_validation_error_handler,
)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _request() -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/permits",
        "headers": [(b"content-type", b"application/json")],
    }
    return Request(scope)


def _nan_error() -> RequestValidationError:
    return RequestValidationError(
        [
            {
                "loc": ("body", "amount"),
                "msg": "Input should be a finite number",
                "type": "finite_number",
                "input": float("nan"),
            }
        ]
    )


# --- _json_safe_numbers ----------------------------------------------------


def test_json_safe_numbers_spells_non_finite_floats():
    assert _json_safe_numbers(float("nan")) == "NaN"
    assert _json_safe_numbers(float("inf")) == "Infinity"
    assert _json_safe_numbers(float("-inf")) == "-Infinity"


def test_json_safe_numbers_passes_finite_values_through():
    assert _json_safe_numbers(3.5) == 3.5
    assert _json_safe_numbers(0) == 0
    assert _json_safe_numbers("NaN") == "NaN"
    assert _json_safe_numbers(None) is None


def test_json_safe_numbers_walks_nested_structures():
    value = {
        "amount": float("nan"),
        "limits": [1.0, float("inf"), {"cap": float("-inf")}],
    }
    assert _json_safe_numbers(value) == {
        "amount": "NaN",
        "limits": [1.0, "Infinity", {"cap": "-Infinity"}],
    }


# --- request_validation_error_handler --------------------------------------


async def test_validation_handler_answers_422_for_nan_input():
    """Edge case behind the custom handler: Starlette accepts bare NaN/Infinity
    literals, echoes the refused input in the 422 detail, and serializes error
    payloads with allow_nan=False, so the default handler answers 500."""
    response = await request_validation_error_handler(_request(), _nan_error())

    assert response.status_code == 422
    body = json.loads(response.body)
    assert body["detail"][0]["input"] == "NaN"
    assert body["detail"][0]["loc"] == ["body", "amount"]


async def test_validation_handler_keeps_finite_input_untouched():
    exc = RequestValidationError(
        [
            {
                "loc": ("body", "amount"),
                "msg": "Field required",
                "type": "missing",
                "input": {"currency": "USD"},
            }
        ]
    )
    response = await request_validation_error_handler(_request(), exc)

    assert response.status_code == 422
    body = json.loads(response.body)
    assert body["detail"][0]["input"] == {"currency": "USD"}


def test_raw_nan_error_body_is_not_strict_json_serializable():
    """Pins why the handler must convert first: the unconverted payload cannot
    be rendered with strict JSON semantics."""
    from fastapi.encoders import jsonable_encoder

    raw = {"detail": jsonable_encoder(_nan_error().errors())}
    with pytest.raises(ValueError):
        json.dumps(raw, allow_nan=False)


# --- signing-key remediation map -------------------------------------------


def test_signing_key_remediation_covers_both_known_codes():
    assert set(_SIGNING_KEY_REMEDIATION) == {
        "trust_signing_private_key_required",
        "invalid_trust_signing_private_key",
    }
    for code, text in _SIGNING_KEY_REMEDIATION.items():
        assert isinstance(text, str) and text.strip(), code
        assert "TRUST_SIGNING_PRIVATE_KEY_B64" in text, code


def test_signing_key_remediation_default_names_both_settings():
    assert "TRUST_SIGNING_PRIVATE_KEY_B64" in _SIGNING_KEY_REMEDIATION_DEFAULT
    assert "TRUST_SIGNING_KEY_ID" in _SIGNING_KEY_REMEDIATION_DEFAULT


# --- assembled app metadata -------------------------------------------------


def test_app_metadata_matches_settings():
    settings = get_settings()
    assert app.title == settings.APP_NAME
    assert app.version == settings.APP_VERSION
    assert app.docs_url == "/docs"
    assert app.redoc_url == "/redoc"
    assert app.openapi_url == "/openapi.json"


def test_app_openapi_contact_matches_public_contact():
    info = app.openapi()["info"]
    if public_contact is None:
        assert "contact" not in info
    else:
        assert info["contact"] == public_contact


def test_app_servers_advertise_public_url():
    settings = get_settings()
    urls = [server["url"] for server in app.servers]
    assert (settings.PUBLIC_URL or "https://api.thisisatest.tech") in urls


def test_app_description_states_no_exactly_once_claim():
    assert "not exactly-once" in (app.description or "")


# --- root content negotiation edge ------------------------------------------


@pytest.mark.anyio
async def test_root_prefers_json_when_both_media_types_accepted(client):
    """Accept carrying text/html *and* application/json is a machine caller:
    it must get the JSON service index, not the dashboard page."""
    resp = await client.get("/", headers={"Accept": "text/html, application/json"})

    assert resp.status_code == 200
    assert "application/json" in resp.headers["content-type"]
    data = resp.json()
    assert data["name"] == get_settings().APP_NAME
    assert "services" in data


@pytest.mark.anyio
async def test_root_json_index_carries_rate_limits_and_auth(client):
    resp = await client.get("/", headers={"Accept": "application/json"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["auth"]["header"] == "X-API-Key"
    assert isinstance(data["rate_limits"], dict)
    assert data["docs"]["openapi"] == "/openapi.json"


def test_root_openapi_response_schema_covers_html_and_json():
    spec = app.openapi()
    content = spec["paths"]["/"]["get"]["responses"]["200"]["content"]
    assert "text/html" in content
    assert content["text/html"]["schema"] == {"type": "string"}


async def test_validation_handler_spells_signed_infinities():
    """Both infinities take the same string-spelling path as NaN."""
    for raw, spelled in ((float("inf"), "Infinity"), (float("-inf"), "-Infinity")):
        exc = RequestValidationError(
            [
                {
                    "loc": ("body", "amount"),
                    "msg": "Input should be a finite number",
                    "type": "finite_number",
                    "input": {"ceiling": raw},
                }
            ]
        )
        response = await request_validation_error_handler(_request(), exc)

        assert response.status_code == 422
        assert json.loads(response.body)["detail"][0]["input"] == {"ceiling": spelled}
