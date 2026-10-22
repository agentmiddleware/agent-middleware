"""Passkey provider is dev-only: the singleton needs an explicit opt-in.

Registered credentials and authenticator replay counters live only in
process memory, so the provider is not a production login path. These tests
pin the gate: app code reaching get_webauthn_provider() without
ENABLE_PROOF_SURFACES=true gets a clear refusal, and opting in works.
"""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.services import webauthn_provider as provider_module
from app.services.webauthn_provider import WebAuthnProvider, get_webauthn_provider


@pytest.fixture
def _isolated_provider_singleton(monkeypatch):
    """Point the singleton at None and restore settings/singleton after."""
    monkeypatch.setenv("ENABLE_PROOF_SURFACES", "false")
    get_settings.cache_clear()
    previous = provider_module._webauthn_provider
    provider_module._webauthn_provider = None
    try:
        yield
    finally:
        provider_module._webauthn_provider = previous
        monkeypatch.undo()
        get_settings.cache_clear()


@pytest.mark.usefixtures("_isolated_provider_singleton")
def test_provider_refuses_without_explicit_dev_flag():
    with pytest.raises(RuntimeError, match="not a production login path") as exc_info:
        get_webauthn_provider()
    message = str(exc_info.value)
    assert "process memory" in message
    assert "ENABLE_PROOF_SURFACES=true" in message


@pytest.mark.usefixtures("_isolated_provider_singleton")
def test_provider_available_with_explicit_dev_flag(monkeypatch):
    monkeypatch.setenv("ENABLE_PROOF_SURFACES", "true")
    get_settings.cache_clear()

    provider = get_webauthn_provider()

    assert isinstance(provider, WebAuthnProvider)
    # Same opt-in boot reuses the singleton rather than forking state.
    assert get_webauthn_provider() is provider


@pytest.mark.usefixtures("_isolated_provider_singleton")
async def test_execute_action_skips_passkey_without_dev_flag():
    """Without the dev flag there is no passkey surface, so execution must
    not crash building the gated provider, even for high-risk actions."""
    from app.schemas.awi import (
        AWIExecutionRequest,
        AWISessionCreate,
        AWIStandardAction,
    )
    from app.services.awi_session import AWISessionManager

    manager = AWISessionManager()
    session = await manager.create_session(
        AWISessionCreate(target_url="https://example.com")
    )
    result = await manager.execute_action(
        AWIExecutionRequest(
            session_id=session.session_id,
            action=AWIStandardAction.CHECKOUT,
            parameters={},
        )
    )

    assert result.status != "passkey_required"
