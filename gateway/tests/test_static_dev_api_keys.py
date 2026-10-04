"""Static development/training API keys (STATIC_DEV_API_KEYS).

These keys exist so local testing, demos, and training material can hold a
credential that never rotates. That is only safe because the key class is
worthless outside local: the auth path ignores it in production-like
environments, and the startup guardrail refuses to boot production with the
variable set at all. Both layers are covered here, plus the prefix rule
that keeps a live bootstrap key from ever authenticating through the
static-dev path.
"""

import importlib.util
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.core.auth import AuthContext, STATIC_DEV_KEY_PREFIX, get_auth_context
from app.core.config import Settings, get_settings
from app.core.scopes import require_scope
from app.core.trust_mode import (
    TrustModeGuardrailError,
    validate_trust_mode_config,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

DEV_KEY = f"{STATIC_DEV_KEY_PREFIX}static-training-key-for-tests"


@pytest.fixture()
def auth_settings():
    """Snapshot and restore the cached settings fields these tests mutate."""
    settings = get_settings()
    saved = {
        name: getattr(settings, name)
        for name in ("ENVIRONMENT", "DEBUG", "VALID_API_KEYS", "STATIC_DEV_API_KEYS")
    }
    try:
        yield settings
    finally:
        for name, value in saved.items():
            setattr(settings, name, value)


async def test_static_dev_key_is_bootstrap_admin_locally(auth_settings):
    auth_settings.ENVIRONMENT = "local"
    auth_settings.STATIC_DEV_API_KEYS = f" {DEV_KEY} , {STATIC_DEV_KEY_PREFIX}other "

    context = await get_auth_context(api_key=DEV_KEY)

    assert context.is_bootstrap_admin is True
    assert context.source == "static-dev"
    context.require_bootstrap_admin()  # must not raise


async def test_static_dev_key_ignored_in_production_like_environment(auth_settings):
    """Defense-in-depth: even past the boot guardrail, prod never honors one."""
    auth_settings.ENVIRONMENT = "production"
    auth_settings.STATIC_DEV_API_KEYS = DEV_KEY

    with pytest.raises(HTTPException) as exc:
        await get_auth_context(api_key=DEV_KEY)
    assert exc.value.status_code == 403


async def test_unknown_key_fails_closed_when_only_dev_keys_configured(auth_settings):
    """Configured dev keys close DEBUG open mode, same as VALID_API_KEYS."""
    auth_settings.ENVIRONMENT = "local"
    auth_settings.DEBUG = True
    auth_settings.VALID_API_KEYS = ""
    auth_settings.STATIC_DEV_API_KEYS = DEV_KEY

    with pytest.raises(HTTPException) as exc:
        await get_auth_context(api_key="not-the-configured-dev-key")
    assert exc.value.status_code == 403


async def test_entry_without_dev_prefix_never_authenticates(auth_settings):
    """A live key pasted into STATIC_DEV_API_KEYS must not authenticate."""
    auth_settings.ENVIRONMENT = "local"
    auth_settings.STATIC_DEV_API_KEYS = "amw_live_accidentally-pasted-live-key"

    with pytest.raises(HTTPException) as exc:
        await get_auth_context(api_key="amw_live_accidentally-pasted-live-key")
    assert exc.value.status_code == 403


async def test_require_scope_grants_static_dev_full_access():
    @require_scope("billing:charge")
    async def protected(auth: AuthContext):
        return "ok"

    context = AuthContext(source="static-dev", raw_key=DEV_KEY, is_bootstrap_admin=True)
    assert await protected(auth=context) == "ok"


def test_guardrail_rejects_static_dev_keys_in_production():
    with pytest.raises(TrustModeGuardrailError) as exc_info:
        validate_trust_mode_config(
            environment="production",
            trust_mode_enabled=True,
            signing_private_key_b64="",
            allow_legacy_unpermitted_mcp=False,
            enable_proof_surfaces=False,
            static_dev_api_keys=DEV_KEY,
        )
    assert "STATIC_DEV_API_KEYS" in str(exc_info.value)


def test_guardrail_allows_static_dev_keys_locally():
    validate_trust_mode_config(
        environment="local",
        trust_mode_enabled=True,
        signing_private_key_b64="",
        allow_legacy_unpermitted_mcp=True,
        static_dev_api_keys=DEV_KEY,
    )


def test_shipped_default_is_empty():
    assert Settings.model_fields["STATIC_DEV_API_KEYS"].default == ""


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "generate_static_dev_keys",
        REPO_ROOT / "scripts" / "generate_static_dev_keys.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generator_prints_distinct_prefixed_keys(capsys):
    generator = _load_generator()
    assert generator.KEY_PREFIX == STATIC_DEV_KEY_PREFIX
    assert generator.generate(3) == 0
    keys = capsys.readouterr().out.strip().splitlines()
    assert len(keys) == 3
    assert len(set(keys)) == 3
    for key in keys:
        assert key.startswith(STATIC_DEV_KEY_PREFIX)
        # token_urlsafe(32) yields 43 chars; anything shorter means the
        # entropy constant was accidentally lowered.
        assert len(key) >= len(STATIC_DEV_KEY_PREFIX) + 43
