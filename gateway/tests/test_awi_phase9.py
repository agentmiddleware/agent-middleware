"""
Tests for Phase 9 AWI Enhanced Features
======================================

Tests for:
- WebAuthn/Passkey provider
- AWI Playwright bridge
- AWI RAG engine
"""

import sys
from datetime import timedelta
from types import ModuleType
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.core import url_guard
from app.core.time import utc_now
from app.main import app
from app.routers import awi_enhanced as awi_enhanced_router
from app.services import awi_playwright_bridge as awi_playwright_bridge_module
from app.services import awi_rag_engine as awi_rag_engine_module
from app.schemas.awi_enhanced import DOMBridgeSessionRequest
from app.services.webauthn_provider import WebAuthnProvider
from app.services.awi_playwright_bridge import (
    AWIPlaywrightBridge,
    BridgeSession,
    BrowserSessionLimitExceeded,
    PlaywrightCommand,
    CommandType,
    TranslationMode,
)
from app.services.awi_rag_engine import AWIRAGEngine, SearchResult
from tests.test_trust_helpers import (
    BOOTSTRAP_HEADERS,
    create_tool_permit,
    provision_agent_wallet,
)


class TestWebAuthnProvider:
    """Tests for WebAuthn/Passkey provider."""

    @pytest.fixture(autouse=True)
    def _allow_mock_webauthn(self, monkeypatch):
        # py_webauthn is not installed in CI; production fails closed
        # without it. Tests explicitly opt into the mock verification path.
        monkeypatch.setenv("WEBAUTHN_ALLOW_MOCK", "true")

    @pytest.fixture
    def provider(self):
        return WebAuthnProvider(
            rp_id="test.example.com",
            rp_name="Test App",
            challenge_expiry_seconds=60,
            verification_validity_seconds=60,
        )

    def test_high_risk_actions_require_passkey(self, provider):
        """Test that high-risk actions require passkey verification."""
        high_risk = ["checkout", "payment", "delete_account", "transfer_funds"]

        for action in high_risk:
            assert provider.HIGH_RISK_ACTIONS is not None
            assert action.lower() in provider.HIGH_RISK_ACTIONS

    @pytest.mark.asyncio
    async def test_low_risk_actions_no_passkey(self, provider):
        """Test that low-risk actions don't require passkey."""
        low_risk = ["navigate_to", "scroll", "search_and_sort"]

        for action in low_risk:
            result = await provider.requires_passkey("session1", action)
            assert result is False

    @pytest.mark.asyncio
    async def test_requires_passkey_returns_true_for_high_risk(self, provider):
        """Test requires_passkey for high-risk actions."""
        result = await provider.requires_passkey("session1", "checkout")
        assert result is True

    @pytest.mark.asyncio
    async def test_requires_passkey_returns_false_for_low_risk(self, provider):
        """Test requires_passkey for low-risk actions."""
        result = await provider.requires_passkey("session1", "navigate_to")
        assert result is False

    @pytest.mark.asyncio
    async def test_create_challenge(self, provider):
        """Test challenge creation."""
        challenge = await provider.create_challenge(
            session_id="session1",
            action="checkout",
        )

        assert challenge["challenge_id"] is not None
        assert challenge["rp_id"] == "test.example.com"
        assert challenge["rp_name"] == "Test App"
        assert challenge["timeout"] == 60000
        assert "challenge" in challenge
        assert "public_key_cred_params" in challenge

    @pytest.mark.asyncio
    async def test_create_challenge_fails_for_low_risk_action(self, provider):
        """Test that challenge creation fails for low-risk actions."""
        with pytest.raises(ValueError) as exc_info:
            await provider.create_challenge(
                session_id="session1",
                action="navigate_to",
            )

        assert "does not require passkey" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_verify_response_invalid_challenge(self, provider):
        """Test verification with invalid challenge ID."""
        with pytest.raises(ValueError) as exc_info:
            await provider.verify_response(
                challenge_id="invalid-id",
                credential={},
            )

        assert "Challenge not found" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_verify_response_expired(self, provider):
        """Test verification with expired challenge."""
        provider._challenge_expiry = -1  # Always expired

        challenge = await provider.create_challenge(
            session_id="session1",
            action="checkout",
        )

        with pytest.raises(ValueError) as exc_info:
            await provider.verify_response(
                challenge_id=challenge["challenge_id"],
                credential={},
            )

        assert "expired" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_verify_response_success(self, provider):
        """Test successful verification."""
        challenge = await provider.create_challenge(
            session_id="session1",
            action="checkout",
        )

        # Mock credential with required fields
        credential = {
            "id": "test-credential-id",
            "raw_id": "test-raw-id",
            "type": "public-key",
            "response": {
                "authenticator_data": "dGVzdA==",
                "client_data_json": '{"type":"webauthn.get","challenge":"test","origin":"https://test.example.com"}',
                "signature": "dGVzdA==",
            },
        }

        result = await provider.verify_response(
            challenge_id=challenge["challenge_id"],
            credential=credential,
        )

        assert result["verified"] is True
        assert result["session_id"] == "session1"
        assert result["action"] == "checkout"
        assert "expires_in_seconds" in result

    @pytest.mark.asyncio
    async def test_is_action_verified_false_initially(self, provider):
        """Test that actions are not verified initially."""
        result = await provider.is_action_verified("session1", "checkout")
        assert result is False

    @pytest.mark.asyncio
    async def test_is_action_verified_after_verification(self, provider):
        """Test that actions are verified after successful verification."""
        challenge = await provider.create_challenge(
            session_id="session1",
            action="checkout",
        )

        credential = {
            "id": "test-credential-id",
            "raw_id": "test-raw-id",
            "type": "public-key",
            "response": {
                "authenticator_data": "dGVzdA==",
                "client_data_json": '{"type":"webauthn.get","challenge":"test","origin":"https://test.example.com"}',
                "signature": "dGVzdA==",
            },
        }

        await provider.verify_response(
            challenge_id=challenge["challenge_id"],
            credential=credential,
        )

        result = await provider.is_action_verified("session1", "checkout")
        assert result is True

    @pytest.mark.asyncio
    async def test_is_action_verified_false_for_different_action(self, provider):
        """Test that verification doesn't apply to different actions."""
        challenge = await provider.create_challenge(
            session_id="session1",
            action="checkout",
        )

        credential = {
            "id": "test-credential-id",
            "raw_id": "test-raw-id",
            "type": "public-key",
            "response": {
                "authenticator_data": "dGVzdA==",
                "client_data_json": '{"type":"webauthn.get","challenge":"test","origin":"https://test.example.com"}',
                "signature": "dGVzdA==",
            },
        }

        await provider.verify_response(
            challenge_id=challenge["challenge_id"],
            credential=credential,
        )

        result = await provider.is_action_verified("session1", "payment")
        assert result is False

    @pytest.mark.asyncio
    async def test_invalidate_verification(self, provider):
        """Test invalidating verification."""
        challenge = await provider.create_challenge(
            session_id="session1",
            action="checkout",
        )

        credential = {
            "id": "test-credential-id",
            "raw_id": "test-raw-id",
            "type": "public-key",
            "response": {
                "authenticator_data": "dGVzdA==",
                "client_data_json": '{"type":"webauthn.get","challenge":"test","origin":"https://test.example.com"}',
                "signature": "dGVzdA==",
            },
        }

        await provider.verify_response(
            challenge_id=challenge["challenge_id"],
            credential=credential,
        )

        invalidated = await provider.invalidate_verification("session1", "checkout")
        assert invalidated == 1

        result = await provider.is_action_verified("session1", "checkout")
        assert result is False

    def test_get_high_risk_actions(self, provider):
        """Test getting list of high-risk actions."""
        actions = provider.get_high_risk_actions()

        assert isinstance(actions, list)
        assert len(actions) > 0
        assert "checkout" in actions
        assert "payment" in actions

    def test_cleanup_expired(self, provider):
        """Test cleanup of expired challenges."""
        provider._challenge_expiry = 1

        import asyncio

        asyncio.run(provider.create_challenge("session1", "checkout"))

        import time

        time.sleep(0.1)

        result = provider.cleanup_expired()

        assert "challenges_removed" in result
        assert "verifications_removed" in result


@pytest.mark.proof
class TestWebAuthnUserVerification:
    """Passkey verification of a high-risk action must prove user verification.

    Every challenge this provider issues is for a HIGH_RISK_ACTIONS entry
    (payment, transfer_funds, delete_account, ...). A bare user-presence tap
    proves only that *someone* touched the authenticator; it is the
    UV (PIN/biometric) flag that ties the assertion to the credential owner.
    The assertion check used to pass require_user_verification=False with a
    comment claiming UV was handled elsewhere, but nothing enforced it, and the
    challenge options only asked for UV as "preferred".

    py_webauthn is an optional extra that CI does not install, so these tests
    stand in a fake verify_authentication_response with the library's
    semantics: it rejects an assertion whose UV flag is unset only when the
    caller passes require_user_verification=True.
    """

    CREDENTIAL_ID = "Y3JlZGVudGlhbC1pZA"

    @pytest.fixture
    def provider(self):
        return WebAuthnProvider(
            rp_id="test.example.com",
            rp_name="Test App",
            challenge_expiry_seconds=60,
            verification_validity_seconds=60,
        )

    @pytest.fixture
    def fake_py_webauthn(self, monkeypatch):
        """Pretend py_webauthn is installed; record each verification call."""
        from types import SimpleNamespace

        from app.services import webauthn_provider

        state = {"calls": [], "user_verified": False}

        def fake_verify_authentication_response(**kwargs):
            state["calls"].append(kwargs)
            if kwargs.get("require_user_verification") and not state["user_verified"]:
                raise ValueError(
                    "User verification is required but user was not verified "
                    "during authentication"
                )
            return SimpleNamespace(new_sign_count=1)

        monkeypatch.setattr(webauthn_provider, "PY_WEBAUTHN_AVAILABLE", True)
        monkeypatch.setattr(
            webauthn_provider,
            "verify_authentication_response",
            fake_verify_authentication_response,
            raising=False,
        )
        monkeypatch.setattr(
            webauthn_provider, "parse_credential_id", lambda value: value, raising=False
        )
        monkeypatch.setattr(
            webauthn_provider,
            "generate_challenge",
            lambda: b"\x01" * 64,
            raising=False,
        )
        monkeypatch.setattr(
            webauthn_provider,
            "base64url_to_bytes",
            lambda value: value.encode("ascii"),
            raising=False,
        )
        # Real cryptographic path only: the mock escape hatch must not be what
        # makes these tests pass or fail.
        monkeypatch.setenv("WEBAUTHN_ALLOW_MOCK", "false")
        return state

    async def _register_and_challenge(self, provider, action: str = "payment"):
        await provider.register_credential(
            user_id="user-1",
            credential_id=self.CREDENTIAL_ID,
            public_key=b"public-key-bytes",
        )
        return await provider.create_challenge(
            session_id="session1", action=action, user_id="user-1"
        )

    def _assertion(self) -> dict:
        return {
            "id": self.CREDENTIAL_ID,
            "raw_id": self.CREDENTIAL_ID,
            "type": "public-key",
            "response": {
                "authenticator_data": "dGVzdA==",
                "client_data_json": "e30",
                "signature": "dGVzdA==",
            },
        }

    @pytest.mark.asyncio
    async def test_assertion_check_requires_user_verification(
        self, provider, fake_py_webauthn
    ):
        fake_py_webauthn["user_verified"] = True
        challenge = await self._register_and_challenge(provider)

        result = await provider.verify_response(
            challenge_id=challenge["challenge_id"], credential=self._assertion()
        )

        assert result["verified"] is True
        assert len(fake_py_webauthn["calls"]) == 1
        assert fake_py_webauthn["calls"][0]["require_user_verification"] is True

    @pytest.mark.asyncio
    async def test_presence_only_assertion_cannot_unlock_high_risk_action(
        self, provider, fake_py_webauthn
    ):
        """A valid signature without the UV flag must not verify a payment."""
        fake_py_webauthn["user_verified"] = False
        challenge = await self._register_and_challenge(provider, action="payment")

        with pytest.raises(ValueError, match="Credential verification failed"):
            await provider.verify_response(
                challenge_id=challenge["challenge_id"], credential=self._assertion()
            )

        assert await provider.is_action_verified("session1", "payment") is False
        stored = provider.get_challenge(challenge["challenge_id"])
        assert stored is not None
        assert stored.status.value == "failed"

    @pytest.mark.asyncio
    async def test_challenge_options_require_user_verification(
        self, provider, fake_py_webauthn
    ):
        """The client is told UV is required, not merely preferred."""
        challenge = await self._register_and_challenge(provider)

        assert challenge["user_verification"] == "required"
        assert challenge["authenticator_selection"]["user_verification"] == "required"
        stored = provider.get_challenge(challenge["challenge_id"])
        assert stored is not None
        assert stored.user_verification == "required"

    def test_passkey_challenge_response_defaults_to_required(self):
        """The API schema never advertises a weaker requirement than enforced."""
        from app.schemas.awi_enhanced import PasskeyChallengeResponse

        body = PasskeyChallengeResponse(
            challenge_id="c1",
            challenge="abc",
            rp_id="test.example.com",
            rp_name="Test App",
            timeout=60000,
            public_key_cred_params=[],
            authenticator_selection={},
        )
        assert body.user_verification == "required"


class TestAWIPlaywrightBridge:
    """Tests for AWI Playwright Bridge."""

    @pytest.fixture(autouse=True)
    def _pin_public_documentation_dns(self, monkeypatch):
        """Keep example-domain tests independent of workstation DNS policy."""

        async def resolve(_host):
            return [(None, None, None, None, ("93.184.216.34", 0))]

        monkeypatch.setattr(url_guard, "_resolve_host", resolve)

    @pytest.fixture
    def bridge(self):
        return AWIPlaywrightBridge(mode=TranslationMode.CDP_DIRECT)

    def test_semantic_patterns_exist(self, bridge):
        """Test that semantic patterns are defined."""
        patterns = bridge.SEMANTIC_PATTERNS

        assert "search_input" in patterns
        assert "add_to_cart" in patterns
        assert "checkout" in patterns
        assert "password_input" in patterns

    def test_semantic_pattern_has_tags(self, bridge):
        """Test that semantic patterns have tags."""
        for semantic_type, pattern in bridge.SEMANTIC_PATTERNS.items():
            assert "tags" in pattern
            assert len(pattern["tags"]) > 0

    def test_sort_value_mappings_exist(self, bridge):
        """Test that sort value mappings are defined."""
        mappings = bridge.SORT_VALUE_MAPPINGS

        assert "price_low" in mappings
        assert "price_high" in mappings
        assert "relevance" in mappings

    @pytest.mark.asyncio
    async def test_create_session(self, bridge):
        """Test session creation."""
        session = await bridge.create_session("https://example.com")

        assert session.session_id is not None
        assert session.current_url == "https://example.com"
        assert session.created_at is not None

        await bridge.destroy_session(session.session_id)

    @pytest.mark.asyncio
    async def test_create_session_enforces_max_sessions(self):
        """DOM bridge refuses unbounded browser session creation."""
        bridge = AWIPlaywrightBridge(mode=TranslationMode.CDP_DIRECT, max_sessions=1)
        session = await bridge.create_session("https://example.com")

        with pytest.raises(BrowserSessionLimitExceeded):
            await bridge.create_session("https://example.org")

        await bridge.destroy_session(session.session_id)

    @pytest.mark.asyncio
    async def test_cleanup_expired_sessions_releases_capacity(self):
        """Idle DOM bridge sessions are destroyed before capacity checks."""
        bridge = AWIPlaywrightBridge(
            mode=TranslationMode.CDP_DIRECT,
            max_sessions=1,
            session_ttl_seconds=1,
        )
        session = await bridge.create_session("https://example.com")
        session.last_activity = utc_now() - timedelta(seconds=5)

        replacement = await bridge.create_session("https://example.org")

        assert replacement.session_id != session.session_id
        assert await bridge.get_session(session.session_id) is None
        assert await bridge.get_session(replacement.session_id) is not None

        await bridge.destroy_session(replacement.session_id)

    @pytest.mark.asyncio
    async def test_destroy_session(self, bridge):
        """Test session destruction."""
        session = await bridge.create_session("https://example.com")
        session_id = session.session_id

        result = await bridge.destroy_session(session_id)
        assert result is True

        result = await bridge.destroy_session("invalid-id")
        assert result is False

    @pytest.mark.asyncio
    async def test_translate_search_and_sort(self, bridge):
        """Test translating search_and_sort action."""
        session = await bridge.create_session("https://example.com")

        commands = await bridge.translate_action(
            session_id=session.session_id,
            action="search_and_sort",
            parameters={"query": "laptop", "sort_by": "price_low"},
        )

        assert isinstance(commands, list)

        await bridge.destroy_session(session.session_id)

    @pytest.mark.asyncio
    async def test_translate_add_to_cart(self, bridge):
        """Test translating add_to_cart action."""
        session = await bridge.create_session("https://example.com")

        with pytest.raises(ValueError) as exc_info:
            await bridge.translate_action(
                session_id=session.session_id,
                action="add_to_cart",
                parameters={},
            )

        assert "No add-to-cart element found" in str(exc_info.value)

        await bridge.destroy_session(session.session_id)

    @pytest.mark.asyncio
    async def test_translate_fill_form(self, bridge):
        """Test translating fill_form action."""
        session = await bridge.create_session("https://example.com")

        commands = await bridge.translate_action(
            session_id=session.session_id,
            action="fill_form",
            parameters={
                "data": {
                    "email": "test@example.com",
                    "password": "secret123",
                }
            },
        )

        assert isinstance(commands, list)

        await bridge.destroy_session(session.session_id)

    @pytest.mark.asyncio
    async def test_translate_login(self, bridge):
        """Test translating login action."""
        session = await bridge.create_session("https://example.com/login")

        commands = await bridge.translate_action(
            session_id=session.session_id,
            action="login",
            parameters={"email": "test@example.com", "password": "secret"},
        )

        assert isinstance(commands, list)

        await bridge.destroy_session(session.session_id)

    @pytest.mark.asyncio
    async def test_translate_navigate_to(self, bridge):
        """Test translating navigate_to action."""
        session = await bridge.create_session("https://example.com")

        commands = await bridge.translate_action(
            session_id=session.session_id,
            action="navigate_to",
            parameters={"url": "https://example.com/shop"},
        )

        assert len(commands) == 1
        assert commands[0].command_type == CommandType.GOTO

        await bridge.destroy_session(session.session_id)

    @pytest.mark.asyncio
    async def test_translate_scroll(self, bridge):
        """Test translating scroll action."""
        session = await bridge.create_session("https://example.com")

        commands = await bridge.translate_action(
            session_id=session.session_id,
            action="scroll",
            parameters={"direction": "down", "amount": 500},
        )

        assert len(commands) == 1
        assert commands[0].command_type == CommandType.EVALUATE

        await bridge.destroy_session(session.session_id)

    @pytest.mark.asyncio
    async def test_translate_unsupported_action(self, bridge):
        """Test translating unsupported action."""
        session = await bridge.create_session("https://example.com")

        with pytest.raises(ValueError) as exc_info:
            await bridge.translate_action(
                session_id=session.session_id,
                action="unsupported_action",
                parameters={},
            )

        assert "Unsupported AWI action" in str(exc_info.value)

        await bridge.destroy_session(session.session_id)

    @pytest.mark.asyncio
    async def test_translate_invalid_session(self, bridge):
        """Test translating action with invalid session."""
        with pytest.raises(ValueError) as exc_info:
            await bridge.translate_action(
                session_id="invalid-session",
                action="search_and_sort",
                parameters={},
            )

        assert "Session invalid-session not found" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_execute_commands(self, bridge):
        """Test command execution without opening a real browser."""

        class FakePage:
            url = "https://example.com"

            async def title(self):
                return "Example"

        session = await bridge.create_session("https://example.com")
        session._page = FakePage()

        commands = [
            PlaywrightCommand(
                command_type=CommandType.WAIT_FOR_TIMEOUT,
                target="",
                wait_for_timeout_ms=100,
            )
        ]

        result = await bridge.execute_commands(
            session_id=session.session_id,
            commands=commands,
        )

        assert result.success is True
        assert result.commands_executed == 1

        await bridge.destroy_session(session.session_id)

    @pytest.mark.asyncio
    async def test_extract_state_representation(self, bridge):
        """Test extracting state representation (skipped if Playwright not installed)."""
        # Check if Playwright is available
        try:
            from playwright.async_api import async_playwright  # noqa: F401
        except ImportError:
            pytest.skip("Playwright not installed")

        session = await bridge.create_session("https://example.com")

        representation = await bridge.extract_state_representation(
            session_id=session.session_id,
            representation_type="summary",
        )

        assert "session_id" in representation
        assert "url" in representation
        assert "page_type" in representation

        await bridge.destroy_session(session.session_id)

    def test_classify_page_type(self, bridge):
        """Test page type classification."""
        from app.services.awi_playwright_bridge import BridgeSession

        # Shopping page
        session = BridgeSession(
            session_id="test", current_url="https://shop.example.com"
        )
        page_type = bridge._classify_page_type(session)
        assert page_type == "product_listing"

        # Login page
        session = BridgeSession(
            session_id="test", current_url="https://example.com/login"
        )
        page_type = bridge._classify_page_type(session)
        assert page_type == "login"

        # Generic page
        session = BridgeSession(session_id="test", current_url="https://example.com")
        page_type = bridge._classify_page_type(session)
        assert page_type == "generic"

    def test_infer_field_type(self, bridge):
        """Test field type inference."""
        assert bridge._infer_field_type("email", "test@example.com") == "email_input"
        assert bridge._infer_field_type("password", "secret") == "password_input"
        assert bridge._infer_field_type("search_box", "") == "search_input"
        assert bridge._infer_field_type("phone_number", "123") == "text_input"

    def test_get_sort_option_value(self, bridge):
        """Test sort option value mapping."""
        assert bridge._get_sort_option_value("price_low") == "price-asc"
        assert bridge._get_sort_option_value("price_high") == "price-desc"
        assert bridge._get_sort_option_value("relevance") == "relevance"


class TestDOMBridgeViewportBounds:
    """Viewport sizes are bounded before they reach a browser context."""

    OUT_OF_RANGE = [(0, 720), (1280, 0), (-1, -1), (100000, 720), (1280, 100000)]

    @pytest.fixture(autouse=True)
    def _pin_public_documentation_dns(self, monkeypatch):
        async def resolve(_host):
            return [(None, None, None, None, ("93.184.216.34", 0))]

        monkeypatch.setattr(url_guard, "_resolve_host", resolve)

    @pytest.mark.parametrize("width,height", OUT_OF_RANGE)
    def test_schema_rejects_out_of_range_viewport(self, width, height):
        with pytest.raises(ValidationError):
            DOMBridgeSessionRequest(
                target_url="https://example.com",
                viewport_width=width,
                viewport_height=height,
            )

    @pytest.mark.parametrize("width,height", [(1280, 720), (320, 240), (3840, 2160)])
    def test_schema_accepts_supported_viewports(self, width, height):
        request = DOMBridgeSessionRequest(
            target_url="https://example.com",
            viewport_width=width,
            viewport_height=height,
        )
        assert (request.viewport_width, request.viewport_height) == (width, height)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("viewport", OUT_OF_RANGE)
    async def test_bridge_rejects_out_of_range_viewport(self, viewport):
        bridge = AWIPlaywrightBridge(mode=TranslationMode.CDP_DIRECT)

        with pytest.raises(ValueError, match="viewport"):
            await bridge.create_session("https://example.com", viewport=viewport)

        assert await bridge.list_sessions() == []

    @pytest.mark.anyio
    async def test_dom_session_endpoint_rejects_unbounded_viewport(
        self, clean_database
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            provisioned = await provision_agent_wallet(client)
            headers = provisioned["agent_headers"]
            wallet_id = provisioned["agent_wallet_id"]

            for width, height in self.OUT_OF_RANGE:
                resp = await client.post(
                    "/v1/awi/dom/session",
                    json={
                        "target_url": "https://example.com",
                        "wallet_id": wallet_id,
                        "viewport_width": width,
                        "viewport_height": height,
                    },
                    headers=headers,
                )
                assert resp.status_code == 422, resp.text

            # The owning wallet still gets a session with a supported size.
            created = await client.post(
                "/v1/awi/dom/session",
                json={
                    "target_url": "https://example.com",
                    "wallet_id": wallet_id,
                    "viewport_width": 1280,
                    "viewport_height": 720,
                },
                headers=headers,
            )
            assert created.status_code == 201, created.text
            session_id = created.json()["session_id"]

            deleted = await client.delete(
                f"/v1/awi/dom/session/{session_id}", headers=headers
            )
            assert deleted.status_code == 204, deleted.text


class TestAWIRAGEngine:
    """Tests for AWI RAG Engine."""

    @pytest.fixture
    def engine(self):
        return AWIRAGEngine(embedding_dimension=32)

    @pytest.mark.asyncio
    async def test_chromadb_backend_fails_closed_when_package_is_present(
        self, engine, monkeypatch, caplog
    ):
        """An ambient vulnerable ChromaDB package must not be activated."""
        client_calls = 0

        class FakeClient:
            def get_or_create_collection(self, **_kwargs):
                return object()

        def fake_persistent_client(**_kwargs):
            nonlocal client_calls
            client_calls += 1
            return FakeClient()

        chromadb_module = ModuleType("chromadb")
        chromadb_config_module = ModuleType("chromadb.config")
        setattr(chromadb_module, "PersistentClient", fake_persistent_client)
        setattr(chromadb_config_module, "Settings", lambda **_kwargs: object())
        monkeypatch.setitem(sys.modules, "chromadb", chromadb_module)
        monkeypatch.setitem(sys.modules, "chromadb.config", chromadb_config_module)

        engine._use_chroma = True
        engine._chroma_client = object()
        engine._chroma_collection = object()

        with caplog.at_level("WARNING"):
            initialized = await engine.init_chroma()

        assert initialized is False
        assert client_calls == 0
        assert engine._use_chroma is False
        assert engine._chroma_client is None
        assert engine._chroma_collection is None
        assert "GHSA-f4j7-r4q5-qw2c" in caplog.text

    @pytest.mark.asyncio
    async def test_index_session(self, engine):
        """Test session indexing."""
        memory_id = await engine.index_session(
            session_id="session1",
            session_type="shopping",
            action_history=[
                {"action": "search_and_sort", "parameters": {"query": "laptop"}},
                {"action": "add_to_cart", "parameters": {"product": "ThinkPad X1"}},
            ],
            state_snapshots=[
                {"summary": "Product listing page", "page_type": "product_listing"},
            ],
        )

        assert memory_id is not None

        memory = await engine.get_memory(memory_id)
        assert memory is not None
        assert memory.session_type == "shopping"
        assert memory.session_id == "session1"

    @pytest.mark.asyncio
    async def test_search(self, engine):
        """Test semantic search."""
        await engine.index_session(
            session_id="session1",
            session_type="shopping",
            action_history=[
                {"action": "search_and_sort", "parameters": {"query": "laptop"}},
            ],
            state_snapshots=[
                {"summary": "Product listing page", "page_type": "shopping"},
            ],
        )

        results = await engine.search(
            query="shopping for laptops",
            top_k=5,
            similarity_threshold=0.0,
        )

        assert len(results) > 0
        assert results[0].session_type == "shopping"

    @pytest.mark.asyncio
    async def test_search_with_type_filter(self, engine):
        """Test search with session type filter."""
        await engine.index_session(
            session_id="session1",
            session_type="shopping",
            action_history=[{"action": "search"}],
            state_snapshots=[],
        )

        await engine.index_session(
            session_id="session2",
            session_type="form_filling",
            action_history=[{"action": "fill_form"}],
            state_snapshots=[],
        )

        results = await engine.search(
            query="task",
            session_type="shopping",
            top_k=5,
            similarity_threshold=0.0,
        )

        assert all(r.session_type == "shopping" for r in results)

    @pytest.mark.asyncio
    async def test_search_by_entities(self, engine):
        """Test entity-based search."""
        await engine.index_session(
            session_id="session1",
            session_type="shopping",
            action_history=[],
            state_snapshots=[],
        )

        results = await engine.search_by_entities(
            entities=["ThinkPad", "MacBook"],
            top_k=5,
        )

        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_get_session_context(self, engine):
        """Test getting session context."""
        await engine.index_session(
            session_id="session1",
            session_type="shopping",
            action_history=[
                {"action": "search_and_sort"},
                {"action": "add_to_cart"},
                {"action": "checkout"},
            ],
            state_snapshots=[],
        )

        context = await engine.get_session_context(
            current_session_id="session2",
            current_state={"url": "https://shop.example.com", "goal": "buy laptop"},
            top_k=3,
        )

        assert "current_session_type" in context
        assert "suggested_next_actions" in context

    @pytest.mark.asyncio
    async def test_delete_memory(self, engine):
        """Test memory deletion."""
        memory_id = await engine.index_session(
            session_id="session1",
            session_type="shopping",
            action_history=[],
            state_snapshots=[],
        )

        result = await engine.delete_memory(memory_id)
        assert result is True

        memory = await engine.get_memory(memory_id)
        assert memory is None

    @pytest.mark.asyncio
    async def test_delete_memory_not_found(self, engine):
        """Test deleting non-existent memory."""
        result = await engine.delete_memory("non-existent-id")
        assert result is False

    @pytest.mark.asyncio
    async def test_owner_scope_filters_before_scoring_and_truncation(self, engine):
        """An owner-scoped retrieval never scores, touches, or returns another
        owner's memory, so it cannot be crowded out of ``top_k`` either."""
        history = [{"action": "add_to_cart", "parameters": {"product": "Widget"}}]
        own_a = await engine.index_session(
            session_id="sess-a",
            session_type="shopping",
            action_history=history,
            state_snapshots=[],
            owner_wallet_id="wallet-a",
        )
        foreign = await engine.index_session(
            session_id="sess-b",
            session_type="shopping",
            action_history=history,
            state_snapshots=[],
            owner_wallet_id="wallet-b",
        )
        ownerless = await engine.index_session(
            session_id="sess-admin",
            session_type="shopping",
            action_history=history,
            state_snapshots=[],
        )
        a_only = {"wallet-a"}
        # Identical histories embed identically, so every memory scores 1.0
        # against this probe and only the owner filter can separate them.
        memory = engine._memories[own_a]
        probe = engine._prepare_embedding_text(
            memory.session_type,
            memory.action_sequence,
            memory.page_summaries,
            memory.key_entities,
            memory.user_intent,
        )

        searched = await engine.search(probe, top_k=1, owner_wallet_ids=a_only)
        by_entity = await engine.search_by_entities(
            ["Widget"], top_k=5, owner_wallet_ids=a_only
        )
        a_or_ownerless = await engine.search(
            probe, top_k=5, owner_wallet_ids={"wallet-a", None}
        )
        context = await engine.get_session_context(
            current_session_id="sess-a",
            current_state={"goal": probe},
            session_type="shopping",
            top_k=5,
            owner_wallet_ids=a_only,
        )
        # sess-a's own memory is the probe, so the A scope leaves nothing else.
        similar = await engine.get_similar_sessions(
            "sess-a", top_k=5, owner_wallet_ids=a_only
        )

        assert [r.memory_id for r in searched] == [own_a]
        assert [r.memory_id for r in by_entity] == [own_a]
        assert {r.memory_id for r in a_or_ownerless} == {own_a, ownerless}
        assert [m["memory_id"] for m in context["relevant_past_sessions"]] == [own_a]
        assert similar == []
        assert engine._memories[foreign].access_count == 0

        # Unscoped calls (bootstrap/internal) keep their prior behavior.
        unscoped = await engine.search(probe, top_k=5)
        assert {r.memory_id for r in unscoped} == {own_a, foreign, ownerless}

    @pytest.mark.asyncio
    async def test_get_stats(self, engine):
        """Test getting statistics."""
        await engine.index_session(
            session_id="session1",
            session_type="shopping",
            action_history=[],
            state_snapshots=[],
        )

        await engine.index_session(
            session_id="session2",
            session_type="form_filling",
            action_history=[],
            state_snapshots=[],
        )

        stats = await engine.get_stats()

        assert stats["total_memories"] >= 2
        assert stats["total_sessions"] >= 2
        assert "type_counts" in stats

    def test_infer_session_type(self, engine):
        """Test session type inference."""
        # Shopping
        result = engine._infer_session_type(
            "generic",
            ["search_and_sort", "add_to_cart", "checkout"],
            [{"page_type": "shopping"}],
        )
        assert result == "shopping"

        # Form filling
        result = engine._infer_session_type(
            "generic",
            ["fill_form"],
            [{"page_type": "form"}],
        )
        assert result == "form_filling"

        # Login
        result = engine._infer_session_type(
            "generic",
            ["login"],
            [{"page_type": "login"}],
        )
        assert result == "authentication"

    def test_cosine_similarity(self, engine):
        """Test cosine similarity calculation."""
        a = [1.0, 0.0, 0.0]
        b = [1.0, 0.0, 0.0]
        assert engine._cosine_similarity(a, b) == pytest.approx(1.0)

        a = [1.0, 0.0, 0.0]
        b = [0.0, 1.0, 0.0]
        assert engine._cosine_similarity(a, b) == pytest.approx(0.0)

        a = [1.0, 1.0]
        b = [1.0, 1.0]
        assert engine._cosine_similarity(a, b) == pytest.approx(1.0)

    def test_extract_entities(self, engine):
        """Test entity extraction."""
        action_history = [
            {"action": "add_to_cart", "parameters": {"product": "ThinkPad X1"}},
            {"action": "add_to_cart", "parameters": {"product": "MacBook Pro"}},
        ]

        entities = engine._extract_entities(action_history, [])

        assert "ThinkPad X1" in entities
        assert "MacBook Pro" in entities

    def test_suggest_actions(self, engine):
        """Test action suggestion."""
        similar_sessions = [
            SearchResult(
                memory_id="m1",
                session_id="s1",
                session_type="shopping",
                user_intent="buy laptop",
                action_sequence=["search", "add_to_cart", "checkout"],
                key_entities=[],
                similarity_score=0.9,
                created_at=utc_now(),
                accessed_at=utc_now(),
                access_count=1,
            ),
            SearchResult(
                memory_id="m2",
                session_id="s2",
                session_type="shopping",
                user_intent="buy laptop",
                action_sequence=["search", "add_to_cart"],
                key_entities=[],
                similarity_score=0.8,
                created_at=utc_now(),
                accessed_at=utc_now(),
                access_count=1,
            ),
        ]

        suggestions = engine._suggest_actions(similar_sessions, {})

        assert "search" in suggestions
        assert "add_to_cart" in suggestions


# ─────────────────────────────────────────────────────────────────────────────
# Tenant isolation of the AWI RAG / DOM HTTP routes
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def rag_engine(monkeypatch):
    """A private RAG engine so no other test's memories reach these routes."""
    engine = AWIRAGEngine(embedding_model="mock-embedding")
    monkeypatch.setattr(awi_rag_engine_module, "_rag_engine", engine)
    return engine


async def _tenant_with_session(client: AsyncClient) -> dict[str, Any]:
    tenant = await provision_agent_wallet(client)
    resp = await client.post(
        "/v1/awi/sessions",
        json={
            "target_url": "https://example.com",
            "wallet_id": tenant["agent_wallet_id"],
        },
        headers=tenant["agent_headers"],
    )
    assert resp.status_code == 201, resp.text
    tenant["session_id"] = resp.json()["session_id"]
    return tenant


async def _index_memory(
    client: AsyncClient, tenant: dict[str, Any], *, product: str, idem: str
) -> str:
    """Index a shopping memory for ``tenant``'s session via the governed route."""
    permit = await create_tool_permit(
        client,
        wallet_id=tenant["agent_wallet_id"],
        key_id=tenant["key_id"],
        tool_name="awi_memory_index",
        max_credits=50,
        idem_key=f"permit-{idem}",
    )
    resp = await client.post(
        "/v1/awi/rag/index",
        json={
            "session_id": tenant["session_id"],
            "session_type": "shopping",
            "action_history": [
                {"action": "add_to_cart", "parameters": {"product": product}}
            ],
        },
        headers={
            **tenant["agent_headers"],
            "X-Permit-Id": permit["permit_id"],
            "Idempotency-Key": idem,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["memory_id"]


def _assert_hides_owner(resp, owner: dict[str, Any]) -> None:
    """A denial must not disclose who owns the resource."""
    assert "wallet_id" not in resp.json()["detail"]
    assert owner["agent_wallet_id"] not in resp.text
    assert owner["sponsor_wallet_id"] not in resp.text


@pytest.mark.proof
@pytest.mark.anyio
async def test_foreign_rag_memory_is_indistinguishable_from_missing(
    client, clean_database, rag_engine
):
    a = await _tenant_with_session(client)
    b = await _tenant_with_session(client)
    b_memory = await _index_memory(
        client, b, product="tenant-b-secret-widget", idem="awi-rag-mem-b"
    )

    missing = await client.get(
        f"/v1/awi/rag/memory/{uuid4()}", headers=a["agent_headers"]
    )
    foreign_get = await client.get(
        f"/v1/awi/rag/memory/{b_memory}", headers=a["agent_headers"]
    )
    foreign_delete = await client.delete(
        f"/v1/awi/rag/memory/{b_memory}", headers=a["agent_headers"]
    )

    assert missing.status_code == 404
    for resp in (foreign_get, foreign_delete):
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == {
            "error": "not_found",
            "message": f"Memory {b_memory} not found",
        }
        assert resp.json()["detail"].keys() == missing.json()["detail"].keys()
        _assert_hides_owner(resp, b)
        assert b["session_id"] not in resp.text

    # Nothing was deleted, and the owner and a bootstrap admin still read it.
    owner_get = await client.get(
        f"/v1/awi/rag/memory/{b_memory}", headers=b["agent_headers"]
    )
    admin_get = await client.get(
        f"/v1/awi/rag/memory/{b_memory}", headers=BOOTSTRAP_HEADERS
    )
    assert owner_get.status_code == 200, owner_get.text
    assert owner_get.json()["memory_id"] == b_memory
    assert admin_get.status_code == 200, admin_get.text

    # Once the owning session is gone the memory is admin-only, and a non-admin
    # sees a plain 404 rather than an admin_access_denied 403.
    destroyed = await client.delete(
        f"/v1/awi/sessions/{b['session_id']}", headers=b["agent_headers"]
    )
    assert destroyed.status_code == 204
    for headers in (a["agent_headers"], b["agent_headers"]):
        orphan = await client.get(f"/v1/awi/rag/memory/{b_memory}", headers=headers)
        assert orphan.status_code == 404, orphan.text
        assert orphan.json()["detail"]["error"] == "not_found"
    admin_orphan = await client.get(
        f"/v1/awi/rag/memory/{b_memory}", headers=BOOTSTRAP_HEADERS
    )
    assert admin_orphan.status_code == 200


@pytest.mark.proof
@pytest.mark.anyio
async def test_foreign_awi_session_routes_answer_404_without_owner(
    client, clean_database, rag_engine
):
    a = await _tenant_with_session(client)
    b = await _tenant_with_session(client)
    b_memory = await _index_memory(
        client, b, product="tenant-b-secret-widget", idem="awi-rag-sess-b"
    )

    missing = await client.get(
        f"/v1/awi/rag/sessions/awi-{uuid4().hex[:12]}/memories",
        headers=a["agent_headers"],
    )
    probes = [
        await client.get(
            f"/v1/awi/rag/sessions/{b['session_id']}/memories",
            headers=a["agent_headers"],
        ),
        await client.delete(
            f"/v1/awi/rag/sessions/{b['session_id']}/memories",
            headers=a["agent_headers"],
        ),
        await client.get(
            f"/v1/awi/rag/context/{b['session_id']}", headers=a["agent_headers"]
        ),
        await client.get(
            f"/v1/awi/passkey/status/{b['session_id']}/checkout",
            headers=a["agent_headers"],
        ),
    ]

    assert missing.status_code == 404
    for resp in probes:
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == {
            "error": "not_found",
            "message": f"Session {b['session_id']} not found",
        }
        _assert_hides_owner(resp, b)

    owner_view = await client.get(
        f"/v1/awi/rag/sessions/{b['session_id']}/memories",
        headers=b["agent_headers"],
    )
    assert owner_view.status_code == 200, owner_view.text
    assert [m["memory_id"] for m in owner_view.json()["memories"]] == [b_memory]


@pytest.mark.proof
@pytest.mark.anyio
async def test_foreign_dom_session_answers_404_without_owner(
    client, clean_database, monkeypatch
):
    a = await provision_agent_wallet(client)
    b = await provision_agent_wallet(client)
    bridge = AWIPlaywrightBridge(mode=TranslationMode.CDP_DIRECT)
    monkeypatch.setattr(awi_playwright_bridge_module, "_bridge", bridge)
    dom_session_id = str(uuid4())
    bridge._sessions[dom_session_id] = BridgeSession(
        session_id=dom_session_id, current_url="https://example.com"
    )
    monkeypatch.setitem(
        awi_enhanced_router._DOM_SESSION_WALLETS,
        dom_session_id,
        b["agent_wallet_id"],
    )
    preview = {"session_id": dom_session_id, "action": "scroll", "parameters": {}}

    missing = await client.get(
        f"/v1/awi/dom/state/{uuid4()}", headers=a["agent_headers"]
    )
    probes = [
        await client.get(
            f"/v1/awi/dom/state/{dom_session_id}", headers=a["agent_headers"]
        ),
        await client.post(
            "/v1/awi/dom/preview", json=preview, headers=a["agent_headers"]
        ),
        await client.delete(
            f"/v1/awi/dom/session/{dom_session_id}", headers=a["agent_headers"]
        ),
    ]

    assert missing.status_code == 404
    for resp in probes:
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == {
            "error": "not_found",
            "message": f"Session {dom_session_id} not found",
        }
        _assert_hides_owner(resp, b)
    assert dom_session_id in bridge._sessions

    owner_preview = await client.post(
        "/v1/awi/dom/preview", json=preview, headers=b["agent_headers"]
    )
    assert owner_preview.status_code == 200, owner_preview.text


@pytest.mark.proof
@pytest.mark.anyio
async def test_session_context_draws_only_on_the_callers_memories(
    client, clean_database, rag_engine
):
    a = await _tenant_with_session(client)
    b = await _tenant_with_session(client)
    a_memory = await _index_memory(
        client, a, product="tenant-a-own-widget", idem="awi-rag-ctx-a"
    )
    b_memory = await _index_memory(
        client, b, product="tenant-b-secret-widget", idem="awi-rag-ctx-b"
    )
    context_params = {"session_type": "shopping", "top_k": 5}

    resp = await client.get(
        f"/v1/awi/rag/context/{a['session_id']}",
        params=context_params,
        headers=a["agent_headers"],
    )

    assert resp.status_code == 200, resp.text
    past = resp.json()["relevant_past_sessions"]
    assert [m["memory_id"] for m in past] == [a_memory]
    assert b_memory not in resp.text
    assert b["session_id"] not in resp.text
    assert "tenant-b-secret-widget" not in resp.text
    # Scoring another tenant's memory must not bump its access counters either.
    assert rag_engine._memories[b_memory].access_count == 0

    owner_b = await client.get(
        f"/v1/awi/rag/context/{b['session_id']}",
        params=context_params,
        headers=b["agent_headers"],
    )
    admin = await client.get(
        f"/v1/awi/rag/context/{a['session_id']}",
        params=context_params,
        headers=BOOTSTRAP_HEADERS,
    )
    assert owner_b.status_code == 200, owner_b.text
    assert [m["memory_id"] for m in owner_b.json()["relevant_past_sessions"]] == [
        b_memory
    ]
    assert admin.status_code == 200, admin.text
    assert [m["memory_id"] for m in admin.json()["relevant_past_sessions"]] == [
        a_memory
    ]
