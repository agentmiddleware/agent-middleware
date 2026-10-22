"""Unhandled HTTP failures are safe for clients and traceable for operators."""

import importlib
import json
import logging
import re
import sys

import pytest
from starlette.requests import Request

from app.main import app as gateway_app
from app.main import unhandled_error_handler


@pytest.fixture
def partner_module(monkeypatch):
    monkeypatch.setenv(
        "PARTNER_MCP_BEARER_TOKEN", "pilot-test-token-0123456789-ABCDEFGH"
    )
    monkeypatch.setenv("PARTNER_MCP_ALLOWED_HOSTS", "partner.example")
    sys.modules.pop("app.partner_mcp", None)
    module = importlib.import_module("app.partner_mcp")
    try:
        yield module
    finally:
        sys.modules.pop("app.partner_mcp", None)


async def _assert_traced_handler(handler, logger_name, caplog):
    request = Request(
        {"type": "http", "method": "GET", "path": "/crash", "headers": []}
    )
    sensitive_detail = "private request data"
    try:
        raise RuntimeError(sensitive_detail)
    except RuntimeError as exc:
        with caplog.at_level(logging.ERROR, logger=logger_name):
            response = await handler(request, exc)

    assert response.status_code == 500
    payload = json.loads(response.body)
    assert payload["code"] == "internal_error"
    assert set(payload) == {"code", "message", "fix", "docs_url", "correlation_id"}
    correlation_id = payload["correlation_id"]
    assert re.fullmatch(r"[0-9a-f]{32}", correlation_id)
    assert sensitive_detail not in response.body.decode()

    record = next(r for r in caplog.records if correlation_id in r.getMessage())
    assert record.name == logger_name
    assert record.levelno == logging.ERROR
    assert "RuntimeError" in record.getMessage()
    assert "test_unhandled_error_logging.py" in record.getMessage()
    assert sensitive_detail not in record.getMessage()


@pytest.mark.anyio
async def test_gateway_unhandled_error_is_traced(caplog, clean_database):
    assert gateway_app.exception_handlers[Exception] is unhandled_error_handler
    await _assert_traced_handler(unhandled_error_handler, "app.main", caplog)


@pytest.mark.anyio
async def test_partner_unhandled_error_is_traced(partner_module, caplog):
    assert (
        partner_module.app.exception_handlers[Exception]
        is partner_module._unhandled_error
    )
    await _assert_traced_handler(
        partner_module._unhandled_error, "app.partner_mcp", caplog
    )
