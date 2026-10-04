from __future__ import annotations

import base64

import pytest

from app.core.config import Settings
from app.services.signing_keys import (
    SigningKeyError,
    validate_signing_key_configuration,
)


def _valid_private_key_b64() -> str:
    return base64.b64encode(bytes(range(32))).decode()


def test_valid_32_byte_private_key_is_loaded():
    settings = Settings(
        TRUST_MODE_ENABLED=True,
        TRUST_SIGNING_PRIVATE_KEY_B64=_valid_private_key_b64(),
    )

    assert validate_signing_key_configuration(settings) == "loaded"


@pytest.mark.parametrize(
    "configured",
    [
        "not-base64",
        base64.b64encode(bytes(31)).decode(),
        base64.b64encode(bytes(64)).decode(),
        f"{_valid_private_key_b64()}!",
    ],
)
def test_malformed_or_wrong_length_private_key_is_rejected(configured: str):
    settings = Settings(
        TRUST_MODE_ENABLED=True,
        TRUST_SIGNING_PRIVATE_KEY_B64=configured,
    )

    with pytest.raises(SigningKeyError) as exc_info:
        validate_signing_key_configuration(settings)

    assert str(exc_info.value) == "invalid_trust_signing_private_key"


def test_missing_private_key_is_rejected_when_trust_mode_is_enabled():
    settings = Settings(
        TRUST_MODE_ENABLED=True,
        TRUST_SIGNING_PRIVATE_KEY_B64="",
    )

    with pytest.raises(SigningKeyError) as exc_info:
        validate_signing_key_configuration(settings)

    assert str(exc_info.value) == "trust_signing_private_key_required"


def test_missing_private_key_uses_ephemeral_mode_when_trust_mode_is_disabled():
    settings = Settings(
        TRUST_MODE_ENABLED=False,
        TRUST_SIGNING_PRIVATE_KEY_B64="",
    )

    assert validate_signing_key_configuration(settings) == "ephemeral"


@pytest.mark.anyio
async def test_lifespan_rejects_invalid_signing_key_before_starting(monkeypatch):
    import app.main as main_module

    monkeypatch.setattr(main_module.settings, "TRUST_MODE_ENABLED", True)
    monkeypatch.setattr(
        main_module.settings,
        "TRUST_SIGNING_PRIVATE_KEY_B64",
        base64.b64encode(bytes(31)).decode(),
    )

    with pytest.raises(SigningKeyError) as exc_info:
        async with main_module.lifespan(main_module.app):
            pytest.fail("lifespan must not start with an invalid signing key")

    assert str(exc_info.value) == "invalid_trust_signing_private_key"
