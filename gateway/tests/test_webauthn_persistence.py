"""
Tests for WebAuthn credential persistence.

Registered passkey credentials (public keys) and authenticator replay
counters used to live only in memory: a process restart forgot every
credential and reset every counter, which weakens cloned-credential
detection. With a credential store path configured, both must survive a
restart, and re-registering a known credential must never roll its replay
counter backwards.
"""

import pytest

from app.services import webauthn_provider as webauthn_module
from app.services.webauthn_provider import WebAuthnProvider


CREDENTIAL_ID = "Y3JlZGVudGlhbC1pZA"


def _make_provider(store_path=None):
    kwargs = {
        "rp_id": "test.example.com",
        "rp_name": "Test App",
        "challenge_expiry_seconds": 60,
        "verification_validity_seconds": 60,
    }
    if store_path is not None:
        kwargs["credential_store_path"] = str(store_path)
    return WebAuthnProvider(**kwargs)


class TestCredentialPersistence:
    @pytest.mark.asyncio
    async def test_credentials_and_counters_survive_restart(self, tmp_path):
        """Registering then rebuilding the provider keeps key and counter."""
        store = tmp_path / "webauthn-store.json"
        provider = _make_provider(store)
        await provider.register_credential(
            user_id="user-1",
            credential_id=CREDENTIAL_ID,
            public_key=b"public-key-bytes",
            sign_count=7,
            name="laptop",
        )

        restarted = _make_provider(store)

        creds = restarted.get_registered_credentials(user_id="user-1")
        assert len(creds) == 1
        assert creds[0]["credential_id"] == CREDENTIAL_ID
        assert creds[0]["name"] == "laptop"
        assert restarted._credentials[CREDENTIAL_ID].public_key == b"public-key-bytes"
        assert restarted._credentials[CREDENTIAL_ID].sign_count == 7

    @pytest.mark.asyncio
    async def test_sign_count_update_survives_restart(self, tmp_path, monkeypatch):
        """A counter advanced by a verification is what a restart reloads."""
        from types import SimpleNamespace

        store = tmp_path / "webauthn-store.json"
        monkeypatch.setenv("WEBAUTHN_ALLOW_MOCK", "false")
        monkeypatch.setattr(webauthn_module, "PY_WEBAUTHN_AVAILABLE", True)
        monkeypatch.setattr(
            webauthn_module,
            "verify_authentication_response",
            lambda **kwargs: SimpleNamespace(new_sign_count=41),
            raising=False,
        )
        monkeypatch.setattr(
            webauthn_module, "parse_credential_id", lambda value: value, raising=False
        )
        monkeypatch.setattr(
            webauthn_module,
            "generate_challenge",
            lambda: b"\x01" * 64,
            raising=False,
        )
        monkeypatch.setattr(
            webauthn_module,
            "base64url_to_bytes",
            lambda value: value.encode("ascii"),
            raising=False,
        )

        provider = _make_provider(store)
        await provider.register_credential(
            user_id="user-1",
            credential_id=CREDENTIAL_ID,
            public_key=b"public-key-bytes",
        )
        challenge = await provider.create_challenge(
            session_id="session1", action="payment", user_id="user-1"
        )
        result = await provider.verify_response(
            challenge_id=challenge["challenge_id"],
            credential={
                "id": CREDENTIAL_ID,
                "raw_id": CREDENTIAL_ID,
                "type": "public-key",
                "response": {
                    "authenticator_data": "dGVzdA==",
                    "client_data_json": "e30",
                    "signature": "dGVzdA==",
                },
            },
        )
        assert result["verified"] is True

        restarted = _make_provider(store)
        assert restarted._credentials[CREDENTIAL_ID].sign_count == 41

    @pytest.mark.asyncio
    async def test_reregister_never_rolls_back_counter(self, tmp_path):
        """Re-registering a known credential keeps the highest sign count."""
        provider = _make_provider(tmp_path / "webauthn-store.json")
        await provider.register_credential(
            user_id="user-1",
            credential_id=CREDENTIAL_ID,
            public_key=b"public-key-bytes",
            sign_count=10,
        )
        await provider.register_credential(
            user_id="user-1",
            credential_id=CREDENTIAL_ID,
            public_key=b"public-key-bytes",
            sign_count=0,
        )

        assert provider._credentials[CREDENTIAL_ID].sign_count == 10
        restarted = _make_provider(tmp_path / "webauthn-store.json")
        assert restarted._credentials[CREDENTIAL_ID].sign_count == 10

    def test_corrupt_store_fails_closed(self, tmp_path):
        """A damaged store file yields no credentials instead of crashing."""
        store = tmp_path / "webauthn-store.json"
        store.write_text("{not valid json", encoding="utf-8")

        provider = _make_provider(store)

        assert provider.get_registered_credentials() == []

    def test_singleton_reads_store_path_from_env(self, tmp_path, monkeypatch):
        """The process-wide provider picks up WEBAUTHN_CREDENTIAL_STORE."""
        store = tmp_path / "webauthn-store.json"
        monkeypatch.setenv("WEBAUTHN_CREDENTIAL_STORE", str(store))
        monkeypatch.setattr(webauthn_module, "_webauthn_provider", None)
        try:
            provider = webauthn_module.get_webauthn_provider()
            assert provider._credential_store_path == str(store)
        finally:
            monkeypatch.setattr(webauthn_module, "_webauthn_provider", None)
