"""Focused tests for app.services.webauthn_provider.

Covers the main public behavior of WebAuthnProvider: challenge creation,
mock verification, verification status and expiry, invalidation, credential
registration, and cleanup. py_webauthn is not installed in this environment,
so tests opt into the mock verification path via WEBAUTHN_ALLOW_MOCK, the
same approach used in test_awi_phase9.py. No network access.
"""

from datetime import timedelta

import pytest

from app.services import webauthn_provider
from app.services.webauthn_provider import WebAuthnProvider


@pytest.fixture(autouse=True)
def _allow_mock_webauthn(monkeypatch):
    monkeypatch.setenv("WEBAUTHN_ALLOW_MOCK", "true")


@pytest.fixture
def provider():
    return WebAuthnProvider(
        rp_id="test.example.com",
        rp_name="Test App",
        challenge_expiry_seconds=60,
        verification_validity_seconds=60,
    )


def _credential(credential_id="test-credential-id"):
    return {
        "id": credential_id,
        "raw_id": "test-raw-id",
        "type": "public-key",
        "response": {
            "authenticator_data": "dGVzdA==",
            "client_data_json": (
                '{"type":"webauthn.get","challenge":"test",'
                '"origin":"https://test.example.com"}'
            ),
            "signature": "dGVzdA==",
        },
    }


async def _verified_challenge(provider, session_id="session1", action="checkout"):
    challenge = await provider.create_challenge(session_id=session_id, action=action)
    result = await provider.verify_response(
        challenge_id=challenge["challenge_id"],
        credential=_credential(),
    )
    return challenge, result


class TestRequiresPasskey:
    async def test_high_risk_action_requires_passkey(self, provider):
        assert await provider.requires_passkey("session1", "checkout") is True

    async def test_matching_is_case_insensitive(self, provider):
        assert await provider.requires_passkey("session1", "Checkout") is True

    async def test_low_risk_action_does_not_require_passkey(self, provider):
        assert await provider.requires_passkey("session1", "navigate_to") is False


class TestCreateChallenge:
    async def test_returns_client_options(self, provider):
        challenge = await provider.create_challenge(
            session_id="session1", action="checkout"
        )

        assert challenge["challenge_id"]
        assert challenge["rp_id"] == "test.example.com"
        assert challenge["rp_name"] == "Test App"
        assert challenge["timeout"] == 60000
        assert challenge["user_verification"] == "required"
        assert "challenge" in challenge

    async def test_rejects_low_risk_action(self, provider):
        with pytest.raises(ValueError, match="does not require passkey"):
            await provider.create_challenge(session_id="session1", action="navigate_to")

    async def test_allow_credentials_filtered_by_user(self, provider):
        await provider.register_credential("alice", "cred-alice", b"pk-alice")
        await provider.register_credential("bob", "cred-bob", b"pk-bob")

        challenge = await provider.create_challenge(
            session_id="session1", action="checkout", user_id="alice"
        )

        ids = [c["id"] for c in challenge["allow_credentials"]]
        assert ids == ["cred-alice"]


class TestVerifyResponse:
    async def test_success_marks_action_verified(self, provider):
        challenge, result = await _verified_challenge(provider)

        assert result["verified"] is True
        assert result["challenge_id"] == challenge["challenge_id"]
        assert result["session_id"] == "session1"
        assert result["action"] == "checkout"
        assert await provider.is_action_verified("session1", "checkout") is True

    async def test_unknown_challenge_id(self, provider):
        with pytest.raises(ValueError, match="Challenge not found"):
            await provider.verify_response(
                challenge_id="no-such-challenge", credential=_credential()
            )

    async def test_missing_credential_id(self, provider):
        challenge = await provider.create_challenge(
            session_id="session1", action="checkout"
        )

        with pytest.raises(ValueError, match="Missing credential ID"):
            await provider.verify_response(
                challenge_id=challenge["challenge_id"], credential={}
            )

    async def test_expired_challenge(self, provider):
        provider._challenge_expiry = -1
        challenge = await provider.create_challenge(
            session_id="session1", action="checkout"
        )

        with pytest.raises(ValueError, match="[Ee]xpired"):
            await provider.verify_response(
                challenge_id=challenge["challenge_id"],
                credential=_credential(),
            )

    async def test_challenge_cannot_be_verified_twice(self, provider):
        challenge, _ = await _verified_challenge(provider)

        with pytest.raises(ValueError, match="already verified"):
            await provider.verify_response(
                challenge_id=challenge["challenge_id"],
                credential=_credential(),
            )

    async def test_fails_closed_without_mock_opt_in(self, provider, monkeypatch):
        monkeypatch.delenv("WEBAUTHN_ALLOW_MOCK", raising=False)
        monkeypatch.setattr(webauthn_provider, "PY_WEBAUTHN_AVAILABLE", False)
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "WEBAUTHN_ALLOW_MOCK", False)
        challenge = await provider.create_challenge(
            session_id="session1", action="checkout"
        )

        with pytest.raises(ValueError, match="Credential verification failed"):
            await provider.verify_response(
                challenge_id=challenge["challenge_id"],
                credential=_credential(),
            )


class TestVerificationStatus:
    async def test_unverified_status_shape(self, provider):
        status = await provider.get_verification_status("session1", "checkout")

        assert status == {
            "session_id": "session1",
            "action": "checkout",
            "is_verified": False,
            "verified_at": None,
            "expires_in_seconds": None,
        }

    async def test_verified_status_reports_remaining_time(self, provider):
        await _verified_challenge(provider)

        status = await provider.get_verification_status("session1", "checkout")

        assert status["is_verified"] is True
        assert status["verified_at"] is not None
        assert 0 < status["expires_in_seconds"] <= 60

    async def test_verification_does_not_leak_across_actions(self, provider):
        await _verified_challenge(provider, action="checkout")

        assert await provider.is_action_verified("session1", "payment") is False

    async def test_expired_verification_reads_as_unverified(self, provider):
        await _verified_challenge(provider)
        key = provider._make_verification_key("session1", "checkout")
        provider._verifications[key].expires_at -= timedelta(seconds=3600)

        assert await provider.is_action_verified("session1", "checkout") is False
        status = await provider.get_verification_status("session1", "checkout")
        assert status["is_verified"] is False


class TestInvalidateVerification:
    async def test_invalidate_single_action(self, provider):
        await _verified_challenge(provider, action="checkout")
        await _verified_challenge(provider, action="payment")

        assert await provider.invalidate_verification("session1", "checkout") == 1
        assert await provider.is_action_verified("session1", "checkout") is False
        assert await provider.is_action_verified("session1", "payment") is True

    async def test_invalidate_all_actions_for_session(self, provider):
        await _verified_challenge(provider, action="checkout")
        await _verified_challenge(provider, action="payment")

        assert await provider.invalidate_verification("session1") == 2
        assert await provider.is_action_verified("session1", "checkout") is False
        assert await provider.is_action_verified("session1", "payment") is False

    async def test_invalidate_missing_returns_zero(self, provider):
        assert await provider.invalidate_verification("session1", "checkout") == 0


class TestCredentialsAndCleanup:
    async def test_register_and_list_credentials_filtered_by_user(self, provider):
        await provider.register_credential("alice", "cred-1", b"pk-1", name="key")
        await provider.register_credential("bob", "cred-2", b"pk-2")

        creds = provider.get_registered_credentials(user_id="alice")

        assert [c["credential_id"] for c in creds] == ["cred-1"]
        assert creds[0]["name"] == "key"
        assert len(provider.get_registered_credentials()) == 2

    def test_get_high_risk_actions_sorted(self, provider):
        actions = provider.get_high_risk_actions()

        assert actions == sorted(actions)
        assert "checkout" in actions
        assert "payment" in actions

    async def test_get_challenge_returns_stored_challenge(self, provider):
        challenge = await provider.create_challenge("session1", "checkout")
        stored = provider.get_challenge(challenge["challenge_id"])

        assert stored is not None
        assert stored.session_id == "session1"
        assert provider.get_challenge("missing") is None

    async def test_cleanup_expired_removes_stale_entries(self, provider):
        challenge = await provider.create_challenge(
            session_id="session1", action="checkout"
        )
        provider._challenges[challenge["challenge_id"]].expires_at -= timedelta(
            seconds=3600
        )

        removed = provider.cleanup_expired()

        assert removed["challenges_removed"] == 1
        assert provider.get_challenge(challenge["challenge_id"]) is None
