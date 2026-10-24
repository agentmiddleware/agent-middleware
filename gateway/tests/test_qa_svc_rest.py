"""Deep QA for gateway/app/services rest-module: sandbox, llm, webauthn.

Service-level tests (no network, no sleeps, deterministic) covering error
handling, auth-adjacent behavior and scoring integrity. Bug-repro tests are
marked with the fix they prove.
"""

from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.core.time import utc_now
from app.services import webauthn_provider as wmod
from app.services.llm import LLMService
from app.services.sandbox import (
    Difficulty,
    EnvironmentType,
    SandboxEngine,
)
from app.services.webauthn_provider import WebAuthnProvider


@pytest.fixture
def engine() -> SandboxEngine:
    return SandboxEngine()


# --------------------------------------------------------------------------
# SandboxEngine: creation validation
# --------------------------------------------------------------------------


class TestSandboxCreation:
    async def test_create_invalid_env_type_raises(self, engine):
        with pytest.raises(ValueError, match="not a valid EnvironmentType"):
            await engine.create_environment(env_type="nope", difficulty="easy")

    async def test_create_invalid_difficulty_raises(self, engine):
        with pytest.raises(ValueError, match="not a valid Difficulty"):
            await engine.create_environment(env_type="pattern", difficulty="nope")

    async def test_create_all_types_all_difficulties(self, engine):
        for env_type in ("pattern", "navigation", "api_mock", "adversarial"):
            for difficulty in ("easy", "medium", "hard", "extreme"):
                env = await engine.create_environment(
                    env_type=env_type, difficulty=difficulty, seed=7
                )
                assert env.env_type == EnvironmentType(env_type)
                assert env.difficulty == Difficulty(difficulty)
                assert env.state.max_steps > 0

    async def test_submit_unknown_env_raises(self, engine):
        with pytest.raises(ValueError, match="not found"):
            await engine.submit_action("env-missing", {"type": "observe"})

    async def test_evaluate_unknown_env_raises(self, engine):
        with pytest.raises(ValueError, match="not found"):
            await engine.evaluate("env-missing")


# --------------------------------------------------------------------------
# SandboxEngine: pattern evaluation integrity
# --------------------------------------------------------------------------


class TestPatternEvaluation:
    async def _color_swap_env(self, engine):
        env = await engine.create_environment(
            env_type="pattern", difficulty="easy", seed=1
        )
        env.hidden_rules = ["Transform input grid by applying: color_swap"]
        return env

    async def test_color_swap_answer_is_accepted(self, engine):
        """Repro: the true color_swap answer was rejected (no keyword match)
        while wrong answers like 'rotate' were accepted."""
        env = await self._color_swap_env(engine)
        result = await engine.submit_action(
            env.env_id, {"type": "submit_transform", "value": "color_swap"}
        )
        assert result.action_accepted is True
        assert result.done is True
        assert result.reward == 10.0

    async def test_swap_word_is_accepted(self, engine):
        env = await self._color_swap_env(engine)
        result = await engine.submit_action(
            env.env_id, {"type": "submit_transform", "value": "swap colors"}
        )
        assert result.done is True

    async def test_wrong_guess_rejected(self, engine):
        env = await engine.create_environment(
            env_type="pattern", difficulty="easy", seed=1
        )
        result = await engine.submit_action(
            env.env_id, {"type": "submit_transform", "value": "banana"}
        )
        assert result.action_accepted is True
        assert result.done is False
        assert result.reward == -1.0

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "BUG sandbox.py _evaluate_action: any known keyword solves any "
            "pattern env, the guess is never matched against the actual "
            "hidden rule (e.g. rule 'mirror' solved by 'rotate'). Fixing "
            "changes game semantics, left for the owner."
        ),
    )
    async def test_wrong_keyword_does_not_solve(self, engine):
        env = await engine.create_environment(
            env_type="pattern", difficulty="easy", seed=1
        )
        env.hidden_rules = ["Transform input grid by applying: mirror"]
        result = await engine.submit_action(
            env.env_id, {"type": "submit_transform", "value": "rotate"}
        )
        assert result.done is False

    async def test_observe_records_without_solving(self, engine):
        env = await engine.create_environment(
            env_type="pattern", difficulty="easy", seed=1
        )
        result = await engine.submit_action(env.env_id, {"type": "observe"})
        assert result.reward == 0.5
        assert result.done is False
        assert result.step == 1


# --------------------------------------------------------------------------
# SandboxEngine: completion and step caps
# --------------------------------------------------------------------------


class TestSandboxCompletion:
    async def test_solved_env_rejects_further_actions(self, engine):
        env = await engine.create_environment(
            env_type="pattern", difficulty="easy", seed=1
        )
        env.hidden_rules = ["Apply: rotate"]
        first = await engine.submit_action(
            env.env_id, {"type": "submit_transform", "value": "rotate"}
        )
        assert first.done is True
        second = await engine.submit_action(
            env.env_id, {"type": "submit_transform", "value": "rotate"}
        )
        assert second.action_accepted is False
        assert second.done is True
        assert second.step == first.step
        assert "complete" in second.feedback.lower()

    async def test_max_steps_caps_actions(self, engine):
        env = await engine.create_environment(
            env_type="pattern", difficulty="easy", seed=1
        )
        assert env.state.max_steps == 20
        last = None
        for _ in range(20):
            last = await engine.submit_action(env.env_id, {"type": "observe"})
        assert last.done is True
        capped = await engine.submit_action(env.env_id, {"type": "observe"})
        assert capped.action_accepted is False
        assert capped.done is True
        assert capped.step == 20

    async def test_evaluate_unsolved_math(self, engine):
        env = await engine.create_environment(
            env_type="pattern", difficulty="easy", seed=1
        )
        await engine.submit_action(env.env_id, {"type": "observe"})
        report = await engine.evaluate(env.env_id)
        assert report["steps_used"] == 1
        assert report["max_steps"] == 20
        assert report["solved"] is False
        assert report["efficiency"] == pytest.approx(0.95)
        assert report["generalization_score"] == pytest.approx(20.5)

    async def test_safe_state_never_reveals_hidden_rules(self, engine):
        env = await engine.create_environment(
            env_type="pattern", difficulty="hard", seed=3
        )
        state = engine._safe_state(env)
        assert "hidden_rules" not in state
        assert "hidden_rules" not in state["metadata"]
        for rule in env.hidden_rules:
            assert rule not in str(state)


# --------------------------------------------------------------------------
# SandboxEngine: navigation, api_mock, adversarial
# --------------------------------------------------------------------------


class TestNavigationEvaluation:
    async def test_valid_move_advances(self, engine):
        env = await engine.create_environment(
            env_type="navigation", difficulty="easy", seed=5
        )
        neighbors = env.state.metadata["visible_edges"]["0"]
        assert neighbors, "seed must give node 0 at least one edge"
        result = await engine.submit_action(
            env.env_id, {"type": "move", "value": neighbors[0]}
        )
        assert result.action_accepted is True
        assert result.reward == 0.5
        assert env.state.metadata["current_node"] == neighbors[0]

    async def test_invalid_move_penalized(self, engine):
        env = await engine.create_environment(
            env_type="navigation", difficulty="easy", seed=5
        )
        result = await engine.submit_action(env.env_id, {"type": "move", "value": 9999})
        assert result.reward == -0.5
        assert result.done is False
        assert env.state.metadata["current_node"] == 0


class TestApiMockEvaluation:
    async def test_schema_shifts_every_fifth_interaction(self, engine):
        env = await engine.create_environment(
            env_type="api_mock", difficulty="medium", seed=9
        )
        total = env.state.metadata["num_endpoints"]
        assert total == 6
        for _ in range(4):
            result = await engine.submit_action(
                env.env_id, {"type": "call_endpoint", "value": "x"}
            )
            assert "successfully" in result.feedback
        assert env.state.metadata["endpoints_discovered"] == 4
        fifth = await engine.submit_action(
            env.env_id, {"type": "call_endpoint", "value": "x"}
        )
        assert "schema has changed" in fifth.feedback
        assert env.state.metadata["endpoints_discovered"] == 4
        assert fifth.done is False


class TestAdversarialEvaluation:
    async def test_trap_costs_and_careful_solves(self, engine):
        env = await engine.create_environment(
            env_type="adversarial", difficulty="easy", seed=11
        )
        assert env.state.metadata["traps_remaining"] == 1
        trap = await engine.submit_action(
            env.env_id, {"type": "choose", "value": "obvious"}
        )
        assert trap.reward == -3.0
        assert trap.done is False
        result = None
        for _ in range(4):
            result = await engine.submit_action(
                env.env_id, {"type": "choose", "value": "careful"}
            )
        assert result.done is True
        assert result.reward == 10.0


# --------------------------------------------------------------------------
# LLMService: payload building, parsing, failure handling (no network)
# --------------------------------------------------------------------------


def _openai_settings(**overrides):
    base = {
        "LLM_PROVIDER": "openai",
        "LLM_BASE_URL": "https://api.openai.com/v1",
        "LLM_API_KEY": "synthetic",
        "LLM_MODEL": "synthetic",
        "LLM_MAX_TOKENS": 10,
        "LLM_TEMPERATURE": 0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _chat_success_response(content: str):
    def respond(request):
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                "model": "synthetic",
                "usage": {},
            },
        )

    return respond


class TestLLMPayloads:
    def test_anthropic_tools_rejected(self):
        """Repro: tools were silently dropped for anthropic, so a caller
        asking for function calling got plain chat with no error."""
        service = LLMService()
        service.settings = _openai_settings(LLM_PROVIDER="anthropic")
        with pytest.raises(ValueError, match="tools"):
            service._build_payload(
                [{"role": "user", "content": "hi"}],
                tools=[{"type": "function", "function": {"name": "f"}}],
            )

    def test_openai_payload_includes_tools(self):
        service = LLMService()
        service.settings = _openai_settings()
        payload = service._build_payload(
            [{"role": "user", "content": "hi"}],
            system="sys",
            tools=[{"type": "function", "function": {"name": "f"}}],
            tool_choice="auto",
        )
        assert payload["messages"][0] == {"role": "system", "content": "sys"}
        assert payload["tools"][0]["function"]["name"] == "f"
        assert payload["tool_choice"] == "auto"

    def test_ollama_payload_shape(self):
        service = LLMService()
        service.settings = SimpleNamespace(
            LLM_PROVIDER="ollama",
            OLLAMA_MODEL="tiny",
            OLLAMA_BASE_URL="http://localhost:11434",
        )
        payload = service._build_payload([{"role": "user", "content": "hi"}])
        assert payload["model"] == "tiny"
        assert payload["stream"] is False
        assert "user: hi" in payload["prompt"]

    def test_headers_openai_bearer(self):
        service = LLMService()
        service.settings = _openai_settings()
        headers = service._build_headers()
        assert headers["Authorization"] == "Bearer synthetic"


class TestLLMParsing:
    def test_parse_openai_tool_calls(self):
        service = LLMService()
        service.settings = _openai_settings()
        import json as std_json

        response = {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {"tool_calls": [{"id": "1"}]},
                }
            ],
            "model": "m",
            "usage": {},
        }
        parsed = service._parse_response(response, "openai")
        assert std_json.loads(parsed.content) == {"tool_calls": [{"id": "1"}]}
        assert parsed.finish_reason == "tool_calls"

    def test_parse_anthropic_empty_content(self):
        service = LLMService()
        service.settings = _openai_settings()
        parsed = service._parse_response({"content": [], "model": "m"}, "anthropic")
        assert parsed.content == ""

    def test_parse_ollama(self):
        service = LLMService()
        service.settings = SimpleNamespace(LLM_PROVIDER="ollama", OLLAMA_MODEL="tiny")
        parsed = service._parse_response(
            {"response": "r", "model": "m", "usage": {}, "done_reason": "stop"},
            "ollama",
        )
        assert parsed.content == "r"
        assert parsed.finish_reason == "stop"


class TestLLMFailureHandling:
    async def test_mock_response_when_no_key_no_network(self):
        service = LLMService()
        service.settings = _openai_settings(LLM_API_KEY="")
        response = await service.chat([{"role": "user", "content": "hi"}])
        assert response.model == "mock"
        assert "Mock response" in response.content

    async def test_circuit_breaker_open_raises(self):
        service = LLMService()
        service.settings = SimpleNamespace(LLM_PROVIDER="ollama")
        for _ in range(5):
            service._circuit_breaker.record_failure()
        with pytest.raises(Exception, match="circuit breaker"):
            await service.chat([{"role": "user", "content": "hi"}])

    async def test_analyze_valid_json(self):
        service = LLMService()
        service.settings = _openai_settings()
        service._client = httpx.AsyncClient(
            transport=httpx.MockTransport(_chat_success_response('{"a": 1}'))
        )
        try:
            assert await service.analyze("data", "task") == {"a": 1}
        finally:
            await service.close()

    async def test_analyze_parse_failed_returns_error(self):
        service = LLMService()
        service.settings = _openai_settings()
        service._client = httpx.AsyncClient(
            transport=httpx.MockTransport(_chat_success_response("not json"))
        )
        try:
            result = await service.analyze("data", "task")
            assert result["error"] == "parse_failed"
            assert result["raw"] == "not json"
        finally:
            await service.close()


# --------------------------------------------------------------------------
# WebAuthnProvider (no network, no sleeps; time travel by expiry mutation)
# --------------------------------------------------------------------------


@pytest.fixture
def mock_webauthn(monkeypatch):
    """Force the mock verification path even if py_webauthn is installed."""
    monkeypatch.setattr(wmod, "PY_WEBAUTHN_AVAILABLE", False)
    monkeypatch.setenv("WEBAUTHN_ALLOW_MOCK", "true")


@pytest.fixture
def wa_provider():
    return WebAuthnProvider(
        rp_id="test.example.com",
        rp_name="Test App",
        challenge_expiry_seconds=60,
        verification_validity_seconds=60,
    )


@pytest.fixture
def fake_py_webauthn(monkeypatch):
    """Pretend py_webauthn is installed with controllable verification."""
    from types import SimpleNamespace

    state = {"calls": [], "user_verified": True}

    def fake_verify(**kwargs):
        state["calls"].append(kwargs)
        if kwargs.get("require_user_verification") and not state["user_verified"]:
            raise ValueError("user verification required")
        return SimpleNamespace(new_sign_count=1)

    monkeypatch.setattr(wmod, "PY_WEBAUTHN_AVAILABLE", True)
    monkeypatch.setattr(
        wmod, "verify_authentication_response", fake_verify, raising=False
    )
    monkeypatch.setattr(wmod, "parse_credential_id", lambda value: value, raising=False)
    monkeypatch.setattr(wmod, "generate_challenge", lambda: b"\x01" * 64, raising=False)
    monkeypatch.setattr(
        wmod,
        "base64url_to_bytes",
        lambda value: value.encode("ascii"),
        raising=False,
    )
    monkeypatch.setenv("WEBAUTHN_ALLOW_MOCK", "false")
    return state


class TestWebAuthnRegistration:
    async def test_cross_user_takeover_rejected(self, wa_provider):
        """Repro: re-registering a credential_id under another user silently
        stole it. The original owner's credential must stay intact."""
        await wa_provider.register_credential(
            user_id="user-a",
            credential_id="cred-shared",
            public_key=b"key-a",
        )
        with pytest.raises(ValueError, match="already registered"):
            await wa_provider.register_credential(
                user_id="user-b",
                credential_id="cred-shared",
                public_key=b"key-b",
            )
        owned = wa_provider.get_registered_credentials("user-a")
        assert [c["credential_id"] for c in owned] == ["cred-shared"]
        assert wa_provider.get_registered_credentials("user-b") == []

    async def test_same_user_reregister_updates(self, wa_provider):
        await wa_provider.register_credential(
            user_id="user-a", credential_id="cred-1", public_key=b"key-1"
        )
        assert (
            await wa_provider.register_credential(
                user_id="user-a", credential_id="cred-1", public_key=b"key-2"
            )
            is True
        )
        assert len(wa_provider.get_registered_credentials("user-a")) == 1


class TestWebAuthnVerificationKeys:
    async def _verified(self, wa_provider, action):
        challenge = await wa_provider.create_challenge(
            session_id="sess-1", action=action
        )
        return await wa_provider.verify_response(
            challenge_id=challenge["challenge_id"],
            credential={"id": "any-cred"},
        )

    async def test_action_case_insensitive(self, wa_provider, mock_webauthn):
        """Repro: requires_passkey lowercases but the verification key did
        not, so 'Checkout' verified yet 'checkout' read back unverified."""
        result = await self._verified(wa_provider, "Checkout")
        assert result["verified"] is True
        assert await wa_provider.is_action_verified("sess-1", "checkout") is True
        assert await wa_provider.is_action_verified("sess-1", "CHECKOUT") is True
        status = await wa_provider.get_verification_status("sess-1", "checkout")
        assert status["is_verified"] is True

    async def test_invalidate_matches_case_insensitively(
        self, wa_provider, mock_webauthn
    ):
        await self._verified(wa_provider, "Payment")
        assert await wa_provider.invalidate_verification("sess-1", "payment") == 1
        assert await wa_provider.is_action_verified("sess-1", "PAYMENT") is False

    async def test_distinct_actions_stay_distinct(self, wa_provider, mock_webauthn):
        await self._verified(wa_provider, "checkout")
        assert await wa_provider.is_action_verified("sess-1", "payment") is False


class TestWebAuthnChallengeLifecycle:
    async def test_create_non_risk_action_raises(self, wa_provider):
        with pytest.raises(ValueError, match="does not require"):
            await wa_provider.create_challenge("sess-1", "browse")

    async def test_requires_passkey_case_insensitive(self, wa_provider):
        assert await wa_provider.requires_passkey("s", "CHECKOUT") is True
        assert await wa_provider.requires_passkey("s", "browse") is False

    async def test_verify_unknown_challenge_raises(self, wa_provider):
        with pytest.raises(ValueError, match="not found"):
            await wa_provider.verify_response("missing", {"id": "x"})

    async def test_double_verify_raises(self, wa_provider, mock_webauthn):
        challenge = await wa_provider.create_challenge("sess-1", "checkout")
        await wa_provider.verify_response(challenge["challenge_id"], {"id": "cred-1"})
        with pytest.raises(ValueError, match="already verified"):
            await wa_provider.verify_response(
                challenge["challenge_id"], {"id": "cred-1"}
            )

    async def test_missing_credential_id_raises(self, wa_provider, mock_webauthn):
        challenge = await wa_provider.create_challenge("sess-1", "checkout")
        with pytest.raises(ValueError, match="Missing credential ID"):
            await wa_provider.verify_response(challenge["challenge_id"], {})

    async def test_expired_challenge_marks_and_raises(self, wa_provider, mock_webauthn):
        challenge = await wa_provider.create_challenge("sess-1", "checkout")
        stored = wa_provider.get_challenge(challenge["challenge_id"])
        stored.expires_at = utc_now() - timedelta(seconds=1)
        with pytest.raises(ValueError, match="expired"):
            await wa_provider.verify_response(
                challenge["challenge_id"], {"id": "cred-1"}
            )
        with pytest.raises(ValueError, match="expired"):
            await wa_provider.verify_response(
                challenge["challenge_id"], {"id": "cred-1"}
            )

    async def test_fail_closed_without_library_or_mock(self, wa_provider, monkeypatch):
        monkeypatch.setattr(wmod, "PY_WEBAUTHN_AVAILABLE", False)
        monkeypatch.setenv("WEBAUTHN_ALLOW_MOCK", "false")
        challenge = await wa_provider.create_challenge("sess-1", "checkout")
        with pytest.raises(ValueError, match="verification failed"):
            await wa_provider.verify_response(
                challenge["challenge_id"], {"id": "cred-1"}
            )

    async def test_unregistered_credential_rejected_real_path(
        self, wa_provider, fake_py_webauthn
    ):
        await wa_provider.register_credential(
            user_id="user-1", credential_id="cred-known", public_key=b"k"
        )
        challenge = await wa_provider.create_challenge(
            session_id="sess-1", action="payment", user_id="user-1"
        )
        with pytest.raises(ValueError, match="verification failed"):
            await wa_provider.verify_response(
                challenge["challenge_id"], {"id": "cred-unknown"}
            )
        assert fake_py_webauthn["calls"] == []

    async def test_verification_expiry_enforced(self, wa_provider, mock_webauthn):
        challenge = await wa_provider.create_challenge("sess-1", "checkout")
        await wa_provider.verify_response(challenge["challenge_id"], {"id": "cred-1"})
        assert await wa_provider.is_action_verified("sess-1", "checkout") is True
        record = next(iter(wa_provider._verifications.values()))
        record.expires_at = utc_now() - timedelta(seconds=1)
        assert await wa_provider.is_action_verified("sess-1", "checkout") is False
        status = await wa_provider.get_verification_status("sess-1", "checkout")
        assert status["is_verified"] is False

    async def test_invalidate_all(self, wa_provider, mock_webauthn):
        for action in ("checkout", "payment"):
            challenge = await wa_provider.create_challenge("sess-1", action)
            await wa_provider.verify_response(
                challenge["challenge_id"], {"id": "cred-1"}
            )
        assert await wa_provider.invalidate_verification("sess-1") == 2
        assert await wa_provider.is_action_verified("sess-1", "checkout") is False

    async def test_cleanup_expired(self, wa_provider, mock_webauthn):
        challenge = await wa_provider.create_challenge("sess-1", "checkout")
        wa_provider.get_challenge(challenge["challenge_id"]).expires_at = (
            utc_now() - timedelta(seconds=1)
        )
        removed = wa_provider.cleanup_expired()
        assert removed["challenges_removed"] == 1
        assert wa_provider.get_challenge(challenge["challenge_id"]) is None
