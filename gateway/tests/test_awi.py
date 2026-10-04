"""
Tests for AWI (Agentic Web Interface) — Phase 7
=================================================
Based on arXiv:2506.10953v1 - "Build the web for agents, not agents for the web"
"""

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import app.services.awi_session as awi_session_module
import app.services.awi_task_queue as awi_task_queue_module
from app.core.config import get_settings
from app.core.durable_state import DurableStateStore
from app.main import app
from app.schemas.awi import (
    AWIActionCategory,
    AWIActionRiskLevel,
    AWIActionStatus,
    AWIActionTier,
    AWIExecutionRequest,
    AWISessionCreate,
    AWISessionStatus,
    AWIStandardAction,
    AWITaskCreate,
    AWITaskStatus,
    AWIRepresentationType,
    AWIHumanIntervention,
)
from app.services.awi_action_vocab import get_awi_vocabulary
from app.services.awi_representation import get_awi_representation
from app.services.awi_task_queue import AWITaskQueue
from app.services.awi_session import AWISessionManager
from app.services.audit_log import list_audit_events
from scripts.awi_representation_benchmark import _validate, run_benchmark

HEADERS = {"X-API-Key": "test-key"}


class TestAWIActionVocabulary:
    """Test AWI action vocabulary."""

    def test_vocabulary_singleton(self):
        """Test vocabulary is singleton."""
        vocab1 = get_awi_vocabulary()
        vocab2 = get_awi_vocabulary()
        assert vocab1 is vocab2

    def test_list_all_actions(self):
        """Test listing all actions."""
        vocab = get_awi_vocabulary()
        actions = vocab.list_all_actions()
        assert len(actions) > 0
        assert all(hasattr(a, "action") for a in actions)

    def test_get_action(self):
        """Test getting a specific action."""
        vocab = get_awi_vocabulary()
        action = vocab.get_action(AWIStandardAction.SEARCH_AND_SORT)
        assert action is not None
        assert action.category == AWIActionCategory.SEARCH

    def test_action_metadata_for_awi_alignment(self):
        """Vocabulary exposes tier, status, risk, and sensitive metadata."""
        vocab = get_awi_vocabulary()

        login = vocab.get_action(AWIStandardAction.LOGIN)
        click = vocab.get_action(AWIStandardAction.CLICK_BUTTON)
        scroll = vocab.get_action(AWIStandardAction.SCROLL)
        checkout = vocab.get_action(AWIStandardAction.CHECKOUT)

        assert login is not None
        assert login.status == AWIActionStatus.PROVISIONAL
        assert login.risk_level == AWIActionRiskLevel.HIGH
        assert login.sensitive_parameters == ["username", "password"]
        assert click is not None
        assert click.tier == AWIActionTier.COMPATIBILITY
        assert scroll is not None
        assert scroll.tier == AWIActionTier.COMPATIBILITY
        assert checkout is not None
        assert checkout.risk_level == AWIActionRiskLevel.HIGH

    def test_redaction_normalizes_common_key_variants(self):
        """Redaction catches snake, camel, and hyphenated sensitive keys."""
        vocab = get_awi_vocabulary()

        redacted = vocab.redact_parameters(
            AWIStandardAction.CHECKOUT,
            {
                "paymentMethod": "card-token",
                "nested": {
                    "access-token": "access-secret",
                    "safe": "visible",
                },
            },
        )

        assert redacted["paymentMethod"] == "[REDACTED]"
        assert redacted["nested"]["access-token"] == "[REDACTED]"
        assert redacted["nested"]["safe"] == "visible"

    def test_list_by_category(self):
        """Test listing actions by category."""
        vocab = get_awi_vocabulary()
        actions = vocab.list_actions_by_category(AWIActionCategory.NAVIGATION)
        assert len(actions) > 0
        assert all(a.category == AWIActionCategory.NAVIGATION for a in actions)

    def test_validate_parameters_valid(self):
        """Test parameter validation with valid params."""
        vocab = get_awi_vocabulary()
        is_valid, error = vocab.validate_parameters(
            AWIStandardAction.SEARCH_AND_SORT, {"query": "laptops"}
        )
        assert is_valid is True
        assert error is None

    def test_validate_parameters_missing_required(self):
        """Test parameter validation with missing required params."""
        vocab = get_awi_vocabulary()
        is_valid, error = vocab.validate_parameters(
            AWIStandardAction.SEARCH_AND_SORT, {}
        )
        assert is_valid is False
        assert "query" in error

    def test_login_accepts_credential_handle_or_legacy_credentials(self):
        """Login validation supports v0.1 transition without requiring secrets."""
        vocab = get_awi_vocabulary()

        handle_valid, handle_error = vocab.validate_parameters(
            AWIStandardAction.LOGIN, {"credential_handle": "credh_123"}
        )
        legacy_valid, legacy_error = vocab.validate_parameters(
            AWIStandardAction.LOGIN,
            {"username": "agent@example.com", "password": "secret"},
        )
        invalid, invalid_error = vocab.validate_parameters(
            AWIStandardAction.LOGIN, {"username": "agent@example.com"}
        )

        assert handle_valid is True
        assert handle_error is None
        assert legacy_valid is True
        assert legacy_error is None
        assert invalid is False
        assert "credential_handle" in invalid_error

    def test_get_estimated_cost(self):
        """Test getting estimated cost for action."""
        vocab = get_awi_vocabulary()
        cost = vocab.get_estimated_cost(AWIStandardAction.CHECKOUT)
        assert cost > 0

    def test_draft_spec_lists_all_standard_actions(self):
        """The draft vocabulary spec should not drift from the enum."""
        spec_path = (
            Path(__file__).resolve().parents[1]
            / "docs"
            / "awi-action-vocabulary-spec.md"
        )
        spec_text = spec_path.read_text()
        missing = [
            action.value
            for action in AWIStandardAction
            if f"`{action.value}`" not in spec_text
        ]
        assert missing == []


class TestAWIRepresentation:
    """Test progressive representation engine."""

    @pytest.mark.anyio
    async def test_generate_summary(self):
        """Test generating summary representation."""
        engine = get_awi_representation()
        page_state = {
            "html": "<html><body><h1>Test</h1><p>Content here</p></body></html>",
            "title": "Test Page",
            "url": "https://example.com",
            "elements": [{"tag": "h1"}, {"tag": "p"}],
        }

        result = await engine.generate_representation(
            "test-session",
            AWIRepresentationType.SUMMARY,
            page_state,
            {"max_length": 100},
        )

        assert result["representation_type"] == "summary"
        assert "content" in result
        assert "metadata" in result

    @pytest.mark.anyio
    async def test_generate_embedding(self):
        """Test generating embedding representation."""
        engine = get_awi_representation()
        page_state = {
            "html": "<html><body>Test content</body></html>",
            "title": "Test",
            "url": "https://example.com",
        }

        result = await engine.generate_representation(
            "test-session", AWIRepresentationType.EMBEDDING, page_state, {}
        )

        assert result["representation_type"] == "embedding"
        assert "vector" in result["content"]

    @pytest.mark.anyio
    async def test_local_representation_benchmark_is_repeatable(self):
        """CI runs the local benchmark harness without threshold gating."""
        first = await run_benchmark()
        second = await run_benchmark()

        def stable(report):
            return [
                {key: value for key, value in row.items() if key != "latency_ms"}
                for row in report["results"]
            ]

        assert first["benchmark"] == "awi-local-representation-v0"
        assert stable(first) == stable(second)
        assert {row["representation_type"] for row in first["results"]} == {
            item.value for item in AWIRepresentationType
        }

    @pytest.mark.anyio
    async def test_local_representation_benchmark_rejects_duplicate_rows(self):
        """Benchmark validation rejects duplicate or missing representation rows."""
        report = await run_benchmark()
        report["results"][1] = dict(report["results"][0])

        with pytest.raises(SystemExit, match="duplicates"):
            _validate(report)


class TestAWITaskQueue:
    """Test AWI task queue."""

    @pytest.mark.anyio
    async def test_create_task(self):
        """Test creating a task."""
        queue = AWITaskQueue(max_concurrent_tasks=5)
        request = AWITaskCreate(
            task_type="web_scraping",
            target_url="https://example.com",
            action_sequence=[{"action": "navigate_to"}],
            priority=3,
        )

        task = await queue.create_task(request)

        assert task.task_id.startswith("awi-task-")
        assert task.task_type == "web_scraping"
        assert task.status == AWITaskStatus.PENDING
        assert task.priority == 3

    @pytest.mark.anyio
    async def test_priority_queue_ordering(self):
        """Test tasks are ordered by priority."""
        queue = AWITaskQueue()

        task1 = await queue.create_task(
            AWITaskCreate(
                task_type="low",
                target_url="https://example.com",
                action_sequence=[],
                priority=5,
            )
        )
        task2 = await queue.create_task(
            AWITaskCreate(
                task_type="high",
                target_url="https://example.com",
                action_sequence=[],
                priority=1,
            )
        )
        task3 = await queue.create_task(
            AWITaskCreate(
                task_type="medium",
                target_url="https://example.com",
                action_sequence=[],
                priority=3,
            )
        )

        assert queue._pending_queue[0] == task2.task_id
        assert queue._pending_queue[1] == task3.task_id
        assert queue._pending_queue[2] == task1.task_id

    @pytest.mark.anyio
    async def test_global_pause_resume(self):
        """Test global pause and resume."""
        queue = AWITaskQueue()

        await queue.global_pause("Human review needed")
        is_paused, reason = queue.is_global_paused()

        assert is_paused is True
        assert reason == "Human review needed"

        await queue.global_resume()
        is_paused, _ = queue.is_global_paused()

        assert is_paused is False

    @pytest.mark.anyio
    async def test_task_queue_rehydrates_from_durable_state(
        self, tmp_path, monkeypatch
    ):
        """Another queue instance can reload pending and completed tasks."""
        monkeypatch.setenv("STATE_BACKEND", "sqlite")
        monkeypatch.setenv("SQLITE_URL", str(tmp_path / "awi-tasks.db"))
        get_settings.cache_clear()
        store = DurableStateStore()
        monkeypatch.setattr(awi_task_queue_module, "get_durable_state", lambda: store)

        try:
            queue = AWITaskQueue()
            task = await queue.create_task(
                AWITaskCreate(
                    task_type="durable",
                    target_url="https://example.com",
                    action_sequence=[{"action": "navigate_to"}],
                    priority=2,
                )
            )
            started = await queue.start_next_task()
            assert started is not None
            await queue.complete_task(task.task_id, {"ok": True})

            rehydrated_queue = AWITaskQueue()
            rehydrated = await rehydrated_queue.get_task(task.task_id)
            status = await rehydrated_queue.get_queue_status()

            assert rehydrated is not None
            assert rehydrated.status == AWITaskStatus.COMPLETED
            assert rehydrated.result == {"ok": True}
            assert status.total_completed == 1
        finally:
            await store.close()
            get_settings.cache_clear()


class TestAWISessionManager:
    """Test AWI session manager."""

    @pytest.mark.anyio
    async def test_create_session(self):
        """Test creating an AWI session."""
        manager = AWISessionManager()
        request = AWISessionCreate(
            target_url="https://example.com",
            max_steps=50,
        )

        session = await manager.create_session(request)

        assert session.session_id.startswith("awi-")
        assert session.target_url == "https://example.com"
        assert session.status == AWISessionStatus.CREATED
        assert session.max_steps == 50

    @pytest.mark.anyio
    async def test_execute_action(self):
        """Test executing an action in a session."""
        manager = AWISessionManager()
        session = await manager.create_session(
            AWISessionCreate(
                target_url="https://example.com",
            )
        )

        result = await manager.execute_action(
            AWIExecutionRequest(
                session_id=session.session_id,
                action=AWIStandardAction.NAVIGATE_TO,
                parameters={"url": "https://new-page.com"},
            )
        )

        assert result.status == "success"
        assert result.action == AWIStandardAction.NAVIGATE_TO

    @pytest.mark.anyio
    async def test_login_password_is_redacted_from_response_and_session_history(self):
        """Legacy login credentials are accepted but never stored in plaintext."""
        manager = AWISessionManager()
        session = await manager.create_session(
            AWISessionCreate(target_url="https://example.com/login")
        )
        manager._session_state[session.session_id]["capabilities"].append(
            "login_page_visible"
        )
        secret = "correct-horse-battery-staple"

        result = await manager.execute_action(
            AWIExecutionRequest(
                session_id=session.session_id,
                action=AWIStandardAction.LOGIN,
                parameters={
                    "username": "agent@example.com",
                    "password": secret,
                    "remember_me": True,
                },
            )
        )
        stored_session = await manager.get_session(session.session_id)

        assert result.status == "success"
        assert result.parameters["username"] == "[REDACTED]"
        assert result.parameters["password"] == "[REDACTED]"
        assert secret not in json.dumps(result.model_dump(mode="json"))
        assert "agent@example.com" not in json.dumps(result.model_dump(mode="json"))
        assert stored_session is not None
        assert secret not in json.dumps(stored_session.model_dump(mode="json"))
        assert "agent@example.com" not in json.dumps(
            stored_session.model_dump(mode="json")
        )
        assert (
            stored_session.action_history[-1]["parameters"]["username"] == "[REDACTED]"
        )
        assert (
            stored_session.action_history[-1]["parameters"]["password"] == "[REDACTED]"
        )

    @pytest.mark.anyio
    async def test_fill_form_redacts_nested_password_from_durable_state(self):
        """Form state storage redacts common secret field names."""
        manager = AWISessionManager()
        session = await manager.create_session(
            AWISessionCreate(target_url="https://example.com/form")
        )
        manager._session_state[session.session_id]["capabilities"].append(
            "form_visible"
        )
        secret = "nested-password-value"

        result = await manager.execute_action(
            AWIExecutionRequest(
                session_id=session.session_id,
                action=AWIStandardAction.FILL_FORM,
                parameters={
                    "fields": {
                        "email": "agent@example.com",
                        "password": secret,
                    }
                },
            )
        )

        assert result.status == "success"
        assert secret not in json.dumps(result.model_dump(mode="json"))
        assert secret not in json.dumps(manager._session_state[session.session_id])

    @pytest.mark.anyio
    async def test_session_rehydrates_from_durable_state(self, tmp_path, monkeypatch):
        """Another manager process can reload AWI session and state rows."""
        monkeypatch.setenv("STATE_BACKEND", "sqlite")
        monkeypatch.setenv("SQLITE_URL", str(tmp_path / "awi-state.db"))
        get_settings.cache_clear()
        store = DurableStateStore()
        monkeypatch.setattr(awi_session_module, "get_durable_state", lambda: store)

        try:
            manager = AWISessionManager()
            session = await manager.create_session(
                AWISessionCreate(
                    target_url="https://example.com",
                    timeout_seconds=42,
                )
            )
            await manager.execute_action(
                AWIExecutionRequest(
                    session_id=session.session_id,
                    action=AWIStandardAction.NAVIGATE_TO,
                    parameters={"url": "https://example.com/checkout"},
                )
            )

            rehydrated_manager = AWISessionManager()
            rehydrated = await rehydrated_manager.get_session(session.session_id)

            assert rehydrated is not None
            assert rehydrated.session_id == session.session_id
            assert rehydrated.current_url == "https://example.com/checkout"
            assert rehydrated.step_count == 1
            assert rehydrated.timeout_seconds == 42
        finally:
            await store.close()
            get_settings.cache_clear()

    @pytest.mark.anyio
    async def test_human_intervention_pause(self):
        """Test human intervention to pause session."""
        manager = AWISessionManager()
        session = await manager.create_session(
            AWISessionCreate(
                target_url="https://example.com",
            )
        )

        result = await manager.human_intervention(
            AWIHumanIntervention(
                session_id=session.session_id,
                action="pause",
                reason="Human review",
            )
        )

        assert result["success"] is True
        assert result["status"] == "paused"

    @pytest.mark.anyio
    async def test_human_intervention_steer(self):
        """Test human steering a session."""
        manager = AWISessionManager()
        session = await manager.create_session(
            AWISessionCreate(
                target_url="https://example.com",
            )
        )
        await manager.human_intervention(
            AWIHumanIntervention(
                session_id=session.session_id,
                action="pause",
            )
        )

        result = await manager.human_intervention(
            AWIHumanIntervention(
                session_id=session.session_id,
                action="steer",
                steer_instructions="Focus on product prices",
            )
        )

        assert result["success"] is True
        assert "instructions_recorded" in result

    @pytest.mark.anyio
    async def test_human_intervention_audit_redacts_steer_instructions(
        self, clean_database
    ):
        """Steering writes audit evidence without storing full instructions."""
        manager = AWISessionManager()
        session = await manager.create_session(
            AWISessionCreate(
                target_url="https://example.com",
                wallet_id="wallet-awi-audit",
            )
        )
        secret_instruction = "Use customer password lunar"

        result = await manager.human_intervention(
            AWIHumanIntervention(
                session_id=session.session_id,
                action="steer",
                reason="operator correction",
                steer_instructions=secret_instruction,
            )
        )
        events = await list_audit_events(
            event="awi.human_intervention",
            wallet_id="wallet-awi-audit",
        )
        stored_session = await manager.get_session(session.session_id)

        assert result["success"] is True
        assert events
        assert events[0].metadata["action"] == "steer"
        assert "steer_instructions_sha256" in events[0].metadata
        assert secret_instruction not in json.dumps(events[0].metadata)
        assert stored_session is not None
        assert secret_instruction not in json.dumps(
            stored_session.model_dump(mode="json")
        )

    @pytest.mark.anyio
    async def test_human_intervention_succeeds_when_audit_write_fails(
        self, monkeypatch
    ):
        """Audit backend failure does not undo an already-authorized intervention."""

        async def fail_audit(**kwargs):
            raise RuntimeError("audit unavailable")

        monkeypatch.setattr(awi_session_module, "record_audit_event", fail_audit)
        manager = AWISessionManager()
        session = await manager.create_session(
            AWISessionCreate(target_url="https://example.com")
        )

        result = await manager.human_intervention(
            AWIHumanIntervention(
                session_id=session.session_id,
                action="pause",
                reason="operator pause",
            )
        )
        stored_session = await manager.get_session(session.session_id)

        assert result == {"success": True, "status": "paused"}
        assert stored_session is not None
        assert stored_session.status == AWISessionStatus.PAUSED
        assert stored_session.paused_by_human is True


class TestAWIRouter:
    """Test AWI router endpoints."""

    @pytest.mark.anyio
    async def test_create_session_endpoint(self):
        """Test POST /v1/awi/sessions."""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/awi/sessions",
                json={
                    "target_url": "https://example.com",
                    "max_steps": 100,
                },
                headers=HEADERS,
            )

            assert response.status_code == 201
            data = response.json()
            assert "session_id" in data
            assert data["target_url"] == "https://example.com"

    @pytest.mark.anyio
    async def test_get_session_endpoint(self):
        """Test GET /v1/awi/sessions/{session_id}."""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            create_response = await client.post(
                "/v1/awi/sessions",
                json={"target_url": "https://example.com"},
                headers=HEADERS,
            )
            session_id = create_response.json()["session_id"]

            response = await client.get(
                f"/v1/awi/sessions/{session_id}", headers=HEADERS
            )

            assert response.status_code == 200
            data = response.json()
            assert data["session_id"] == session_id

    @pytest.mark.anyio
    async def test_execute_action_endpoint(self):
        """Test POST /v1/awi/execute requires permit and returns receipt."""
        from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            provisioned = await provision_agent_wallet(client)
            wallet_id = provisioned["agent_wallet_id"]
            headers = provisioned["agent_headers"]
            permit = await create_tool_permit(
                client,
                wallet_id=wallet_id,
                key_id=provisioned["key_id"],
                tool_name="awi_execute",
                max_credits=50,
                idem_key="permit-awi-execute-endpoint",
            )

            create_response = await client.post(
                "/v1/awi/sessions",
                json={"target_url": "https://example.com", "wallet_id": wallet_id},
                headers=headers,
            )
            assert create_response.status_code == 201
            session_id = create_response.json()["session_id"]

            denied = await client.post(
                "/v1/awi/execute",
                json={
                    "session_id": session_id,
                    "action": "navigate_to",
                    "parameters": {"url": "https://new-page.com"},
                },
                headers=headers,
            )
            assert denied.status_code == 403
            assert denied.json()["detail"]["error"] == "permit_required"

            response = await client.post(
                "/v1/awi/execute",
                json={
                    "session_id": session_id,
                    "action": "navigate_to",
                    "parameters": {"url": "https://new-page.com"},
                },
                headers={
                    **headers,
                    "X-Permit-Id": permit["permit_id"],
                    "Idempotency-Key": "awi-execute-endpoint-1",
                },
            )

            assert response.status_code == 200
            data = response.json()
            assert "execution_id" in data
            assert data["status"] == "success"
            assert data["receipt"]["permit_id"] == permit["permit_id"]
            assert data["receipt"]["outcome"] == "success"

    @pytest.mark.anyio
    async def test_list_vocabulary_endpoint(self):
        """Test GET /v1/awi/vocabulary."""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/v1/awi/vocabulary")

            assert response.status_code == 200
            data = response.json()
            assert "actions" in data
            assert len(data["actions"]) > 0
            login = next(
                action for action in data["actions"] if action["action"] == "login"
            )
            click = next(
                action
                for action in data["actions"]
                if action["action"] == "click_button"
            )
            assert login["status"] == "provisional"
            assert login["risk_level"] == "high"
            assert login["sensitive_parameters"] == ["username", "password"]
            assert click["tier"] == "compatibility"

    @pytest.mark.anyio
    async def test_awi_manifest_matches_action_and_representation_enums(self):
        """Test GET /.well-known/awi.json discovery contract."""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/.well-known/awi.json")

            assert response.status_code == 200
            data = response.json()
            actions = {action["action"]: action for action in data["actions"]}

            assert data["profile"] == "awi-over-mcp"
            assert data["endpoints"]["execute"] == "/v1/awi/execute"
            assert data["safety_capabilities"]["sensitive_parameter_redaction"] is True
            assert set(actions) == {action.value for action in AWIStandardAction}
            assert set(data["representation_types"]) == {
                item.value for item in AWIRepresentationType
            }
            # The route still declares AWIDiscoveryManifest as its response
            # model. Asserted on the route object rather than app.openapi():
            # the path is hidden from the schema when the app boots with
            # ENABLE_PROOF_SURFACES=false (the suite's shared-app posture),
            # while a proof deployment advertises it normally.
            from app.schemas.awi import AWIDiscoveryManifest
            from tests.conftest import iter_routes

            manifest_route = next(
                r
                for r in iter_routes(app.routes)
                if getattr(r, "path", "") == "/.well-known/awi.json"
            )
            assert manifest_route.response_model is AWIDiscoveryManifest
            assert actions["login"]["status"] == "provisional"
            assert actions["click_button"]["tier"] == "compatibility"

    @pytest.mark.anyio
    async def test_create_task_endpoint(self):
        """Test POST /v1/awi/tasks."""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/awi/tasks",
                json={
                    "task_type": "automation",
                    "target_url": "https://example.com",
                    "action_sequence": [{"action": "navigate_to"}],
                    "priority": 5,
                },
                headers=HEADERS,
            )

            assert response.status_code == 201
            data = response.json()
            assert "task_id" in data
            assert data["task_type"] == "automation"

    @pytest.mark.anyio
    async def test_queue_status_endpoint(self):
        """Test GET /v1/awi/queue/status."""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/v1/awi/queue/status", headers=HEADERS)

            assert response.status_code == 200
            data = response.json()
            assert "total_pending" in data
            assert "total_running" in data

    @pytest.mark.anyio
    async def test_session_endpoints_require_api_key(self):
        """AWI session control is not publicly reachable."""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/awi/sessions",
                json={"target_url": "https://example.com"},
            )

            assert response.status_code == 401

    @pytest.mark.anyio
    async def test_db_key_is_scoped_to_awi_session_wallet(self):
        """DB API keys can only drive AWI sessions for their issuing wallet."""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            wallet_a_resp = await client.post(
                "/v1/billing/wallets/sponsor",
                json={"sponsor_name": "AWI Tenant A", "email": "awi-a@test.com"},
                headers=HEADERS,
            )
            wallet_b_resp = await client.post(
                "/v1/billing/wallets/sponsor",
                json={"sponsor_name": "AWI Tenant B", "email": "awi-b@test.com"},
                headers=HEADERS,
            )
            wallet_a = wallet_a_resp.json()["wallet_id"]
            wallet_b = wallet_b_resp.json()["wallet_id"]

            key_resp = await client.post(
                "/v1/api-keys",
                json={"wallet_id": wallet_a, "key_name": "awi-tenant-key"},
                headers=HEADERS,
            )
            db_headers = {"X-API-Key": key_resp.json()["api_key"]}

            own_session_resp = await client.post(
                "/v1/awi/sessions",
                json={"target_url": "https://example.com", "wallet_id": wallet_a},
                headers=db_headers,
            )
            assert own_session_resp.status_code == 201
            own_session_id = own_session_resp.json()["session_id"]

            own_get = await client.get(
                f"/v1/awi/sessions/{own_session_id}", headers=db_headers
            )
            assert own_get.status_code == 200

            other_session_resp = await client.post(
                "/v1/awi/sessions",
                json={"target_url": "https://example.com", "wallet_id": wallet_b},
                headers=HEADERS,
            )
            other_session_id = other_session_resp.json()["session_id"]

            blocked_get = await client.get(
                f"/v1/awi/sessions/{other_session_id}", headers=db_headers
            )
            blocked_execute = await client.post(
                "/v1/awi/execute",
                json={
                    "session_id": other_session_id,
                    "action": "navigate_to",
                    "parameters": {"url": "https://blocked.example.com"},
                },
                headers=db_headers,
            )
            blocked_intervene = await client.post(
                "/v1/awi/intervene",
                json={
                    "session_id": other_session_id,
                    "action": "pause",
                    "reason": "cross-wallet attempt",
                },
                headers=db_headers,
            )
            unchanged_session = await client.get(
                f"/v1/awi/sessions/{other_session_id}", headers=HEADERS
            )
            unknown_session = await client.get(
                "/v1/awi/sessions/awi-000000000000", headers=db_headers
            )

            # Another wallet's session is indistinguishable from a missing
            # one: same 404, and the owning wallet id is never disclosed.
            assert unknown_session.status_code == 404
            for blocked in (blocked_get, blocked_execute, blocked_intervene):
                assert blocked.status_code == 404, blocked.text
                assert blocked.json()["detail"] == {
                    "error": "not_found",
                    "message": f"Session {other_session_id} not found",
                }
                assert "wallet_id" not in blocked.json()["detail"]
                assert wallet_b not in blocked.text
            assert unchanged_session.status_code == 200
            assert unchanged_session.json()["status"] == "created"

    @pytest.mark.anyio
    async def test_passkey_challenge_requires_session_owner(self):
        """Login-window passkey challenges inherit AWI session ownership and permits."""
        from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            provisioned = await provision_agent_wallet(client)
            wallet_id = provisioned["agent_wallet_id"]
            headers = provisioned["agent_headers"]
            permit = await create_tool_permit(
                client,
                wallet_id=wallet_id,
                key_id=provisioned["key_id"],
                tool_name="awi_passkey_challenge",
                max_credits=50,
                idem_key="permit-awi-passkey-owner",
            )
            session_resp = await client.post(
                "/v1/awi/sessions",
                json={"target_url": "https://example.com", "wallet_id": wallet_id},
                headers=headers,
            )
            session_id = session_resp.json()["session_id"]

            no_auth = await client.post(
                "/v1/awi/passkey/challenge",
                json={"session_id": session_id, "action": "checkout"},
            )
            with_auth_no_permit = await client.post(
                "/v1/awi/passkey/challenge",
                json={"session_id": session_id, "action": "checkout"},
                headers=headers,
            )
            with_permit = await client.post(
                "/v1/awi/passkey/challenge",
                json={"session_id": session_id, "action": "checkout"},
                headers={
                    **headers,
                    "X-Permit-Id": permit["permit_id"],
                    "Idempotency-Key": "awi-passkey-owner-1",
                },
            )

            assert no_auth.status_code == 401
            assert with_auth_no_permit.status_code == 403
            assert with_auth_no_permit.json()["detail"]["error"] == "permit_required"
            assert with_permit.status_code == 200
            assert with_permit.json()["receipt"]["permit_id"] == permit["permit_id"]
