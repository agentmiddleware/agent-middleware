"""Focused schema tests for app.schemas.awi_enhanced (Phase 9 models)."""

import pytest
from pydantic import ValidationError

from app.schemas import awi_enhanced as schemas


def test_enums_expose_expected_values():
    assert schemas.PasskeyStatus.VERIFIED.value == "verified"
    assert schemas.DOMSessionStatus.ACTIVE.value == "active"
    assert schemas.DOMRepresentationType.SUMMARY.value == "summary"
    assert schemas.TranslationMode.PLAYWRIGHT.value == "playwright"
    assert schemas.CommandType.CLICK.value == "click"


def test_passkey_challenge_and_verify_round_trip():
    req = schemas.PasskeyChallengeRequest(session_id="s1", action="checkout")
    assert req.session_id == "s1"

    resp = schemas.PasskeyChallengeResponse(
        challenge_id="c1",
        challenge="abc",
        rp_id="example.com",
        rp_name="Example",
        timeout=60000,
        public_key_cred_params=[{"type": "public-key", "alg": -7}],
        authenticator_selection={"userVerification": "required"},
    )
    assert resp.user_verification == "required"
    assert resp.exclude_credentials == []

    verify = schemas.PasskeyVerifyRequest(
        challenge_id="c1", credential={"id": "cred-1", "type": "public-key"}
    )
    assert verify.credential["id"] == "cred-1"

    done = schemas.PasskeyVerifyResponse(
        verified=True,
        challenge_id="c1",
        session_id="s1",
        action="checkout",
        verified_at="2026-10-08T00:00:00Z",
    )
    assert done.expires_in_seconds == 300

    status = schemas.PasskeySessionStatus(
        session_id="s1", action="checkout", is_verified=True
    )
    assert status.verified_at is None
    assert status.expires_in_seconds is None


def test_passkey_request_rejects_missing_fields():
    with pytest.raises(ValidationError):
        schemas.PasskeyChallengeRequest(session_id="s1")
    with pytest.raises(ValidationError):
        schemas.PasskeyVerifyRequest(challenge_id="c1")


def test_dom_bridge_session_viewport_bounds():
    ok = schemas.DOMBridgeSessionRequest(target_url="https://example.com/shop")
    assert ok.headless is True
    assert (ok.viewport_width, ok.viewport_height) == (1280, 720)

    with pytest.raises(ValidationError):
        schemas.DOMBridgeSessionRequest(
            target_url="https://example.com", viewport_width=100
        )
    with pytest.raises(ValidationError):
        schemas.DOMBridgeSessionRequest(
            target_url="https://example.com", viewport_height=5000
        )


def test_dom_models_defaults_and_state_request():
    el = schemas.DOMElementInfo(tag="button", css_selector=".buy", xpath="//button")
    assert el.text_content == ""
    assert el.attributes == {}
    assert el.is_interactive is False

    cmd = schemas.PlaywrightCommandInfo(command_type="click", target="#buy")
    assert cmd.value is None
    assert cmd.options == {}

    sync_req = schemas.DOMSyncRequest(session_id="b1", action="add_to_cart")
    assert sync_req.parameters == {}
    assert sync_req.return_screenshot is False

    sync_resp = schemas.DOMSyncResponse(
        session_id="b1",
        execution_id="e1",
        action="add_to_cart",
        commands_generated=2,
        commands_executed=2,
        state_representation={"page": "cart"},
    )
    assert sync_resp.status == "success"
    assert sync_resp.error is None

    state_req = schemas.DOMStateRequest()
    assert state_req.representation_type == schemas.DOMRepresentationType.SUMMARY
    assert state_req.include_elements is True

    preview = schemas.DOMActionPreviewResponse(
        session_id="b1", action="search", commands=[], estimated_duration_ms=100
    )
    assert preview.elements_found == {}


def test_dom_state_response_requires_page_type():
    with pytest.raises(ValidationError):
        schemas.DOMStateResponse(
            session_id="b1",
            url="https://example.com",
            title="Shop",
            representation_type=schemas.DOMRepresentationType.SUMMARY,
            main_content="hello",
        )


def test_rag_query_validation_edges():
    ok = schemas.RAGQueryRequest(query="shopping for laptops")
    assert ok.top_k == 5
    assert ok.similarity_threshold == 0.7

    with pytest.raises(ValidationError):
        schemas.RAGQueryRequest(query="   ")
    with pytest.raises(ValidationError):
        schemas.RAGQueryRequest(query="")
    with pytest.raises(ValidationError):
        schemas.RAGQueryRequest(query="x", top_k=0)
    with pytest.raises(ValidationError):
        schemas.RAGQueryRequest(query="x", top_k=21)
    with pytest.raises(ValidationError):
        schemas.RAGQueryRequest(query="x", similarity_threshold=1.5)


def test_rag_memory_index_and_search_models():
    idx = schemas.MemoryIndexRequest(session_id="s1", session_type="shopping")
    assert idx.action_history == []
    assert idx.metadata == {}

    resp = schemas.MemoryIndexResponse(
        memory_id="m1", session_id="s1", entities_extracted=2, intent_inferred="buy"
    )
    assert resp.indexed_at is not None

    ctx_req = schemas.SessionContextRequest(current_session_id="s2")
    assert ctx_req.top_k == 3

    with pytest.raises(ValidationError):
        schemas.SessionContextRequest(current_session_id="s2", top_k=11)

    delete = schemas.MemoryDeleteRequest(memory_id="m1")
    assert delete.cascade is False


def test_integration_session_create_bounds():
    create = schemas.AWIEnhancedSessionCreate(target_url="https://example.com")
    assert create.max_steps == 100
    assert create.enable_passkey_gate is True
    assert create.enable_dom_bridge is False

    with pytest.raises(ValidationError):
        schemas.AWIEnhancedSessionCreate(target_url="https://example.com", max_steps=0)
    with pytest.raises(ValidationError):
        schemas.AWIEnhancedSessionCreate(
            target_url="https://example.com", timeout_seconds=5
        )

    exec_req = schemas.AWIEnhancedExecutionRequest(session_id="s1", action="search")
    assert exec_req.parameters == {}
    assert exec_req.passkey_verified is False
    assert exec_req.bypass_dom_bridge is False
