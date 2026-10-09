"""Focused tests for the AWI session manager.

Covers session lifecycle, action execution (including edge and error
cases), human intervention, representation requests, and cleanup.
Uses only local state, no network or external services.
"""

import pytest

from app.schemas.awi import (
    AWIExecutionRequest,
    AWIHumanIntervention,
    AWIRepresentationRequest,
    AWIRepresentationType,
    AWISessionCreate,
    AWISessionStatus,
    AWIStandardAction,
)
from app.services.awi_session import AWISessionManager


def _create_request(target_url="https://example.com", **kwargs):
    return AWISessionCreate(target_url=target_url, **kwargs)


class TestCreateAndGetSession:
    @pytest.mark.anyio
    async def test_create_session_initializes_state(self):
        manager = AWISessionManager()
        session = await manager.create_session(_create_request())

        assert session.session_id.startswith("awi-")
        assert session.target_url == "https://example.com"
        assert session.current_url == "https://example.com"
        assert session.status == AWISessionStatus.CREATED
        assert session.step_count == 0

        state = manager._session_state[session.session_id]
        assert state["current_url"] == "https://example.com"
        assert "page_loaded" in state["capabilities"]

    @pytest.mark.anyio
    async def test_get_session_returns_none_for_unknown_id(self):
        manager = AWISessionManager()
        assert await manager.get_session("awi-does-not-exist") is None

    @pytest.mark.anyio
    async def test_get_session_returns_created_session(self):
        manager = AWISessionManager()
        created = await manager.create_session(_create_request())

        fetched = await manager.get_session(created.session_id)

        assert fetched is not None
        assert fetched.session_id == created.session_id


class TestExecuteAction:
    @pytest.mark.anyio
    async def test_navigate_to_updates_url_and_step_count(self):
        manager = AWISessionManager()
        session = await manager.create_session(_create_request())

        result = await manager.execute_action(
            AWIExecutionRequest(
                session_id=session.session_id,
                action=AWIStandardAction.NAVIGATE_TO,
                parameters={"url": "https://example.com/next"},
            )
        )

        assert result.status == "success"
        assert result.result == {"success": True, "new_url": "https://example.com/next"}
        updated = await manager.get_session(session.session_id)
        assert updated is not None
        assert updated.current_url == "https://example.com/next"
        assert updated.step_count == 1
        assert updated.status == AWISessionStatus.ACTIVE

    @pytest.mark.anyio
    async def test_execute_on_unknown_session_returns_error(self):
        manager = AWISessionManager()

        result = await manager.execute_action(
            AWIExecutionRequest(
                session_id="awi-missing",
                action=AWIStandardAction.NAVIGATE_TO,
                parameters={"url": "https://example.com"},
            )
        )

        assert result.status == "error"
        assert result.effect_status == "not_dispatched"
        assert "awi-missing" in (result.error or "")

    @pytest.mark.anyio
    async def test_dry_run_is_rejected_without_side_effects(self):
        manager = AWISessionManager()
        session = await manager.create_session(_create_request())

        result = await manager.execute_action(
            AWIExecutionRequest(
                session_id=session.session_id,
                action=AWIStandardAction.NAVIGATE_TO,
                parameters={"url": "https://example.com/other"},
                dry_run=True,
            )
        )

        assert result.status == "error"
        assert result.error == "dry_run_unsupported"
        assert result.effect_status == "not_dispatched"
        unchanged = await manager.get_session(session.session_id)
        assert unchanged is not None
        assert unchanged.step_count == 0

    @pytest.mark.anyio
    async def test_invalid_parameters_return_validation_error(self):
        manager = AWISessionManager()
        session = await manager.create_session(_create_request())

        result = await manager.execute_action(
            AWIExecutionRequest(
                session_id=session.session_id,
                action=AWIStandardAction.NAVIGATE_TO,
                parameters={},
            )
        )

        assert result.status == "error"
        assert result.effect_status == "not_dispatched"
        assert result.error

    @pytest.mark.anyio
    async def test_max_steps_reached_completes_session(self):
        manager = AWISessionManager()
        session = await manager.create_session(_create_request(max_steps=1))

        first = await manager.execute_action(
            AWIExecutionRequest(
                session_id=session.session_id,
                action=AWIStandardAction.NAVIGATE_TO,
                parameters={"url": "https://example.com/one"},
            )
        )
        assert first.status == "success"

        second = await manager.execute_action(
            AWIExecutionRequest(
                session_id=session.session_id,
                action=AWIStandardAction.NAVIGATE_TO,
                parameters={"url": "https://example.com/two"},
            )
        )

        assert second.status == "max_steps_reached"
        updated = await manager.get_session(session.session_id)
        assert updated is not None
        assert updated.status == AWISessionStatus.COMPLETED

    @pytest.mark.anyio
    async def test_paused_session_blocks_actions_until_resumed(self):
        manager = AWISessionManager()
        session = await manager.create_session(_create_request())
        await manager.human_intervention(
            AWIHumanIntervention(
                session_id=session.session_id,
                action="pause",
                reason="review",
            )
        )

        blocked = await manager.execute_action(
            AWIExecutionRequest(
                session_id=session.session_id,
                action=AWIStandardAction.NAVIGATE_TO,
                parameters={"url": "https://example.com/blocked"},
            )
        )
        assert blocked.status == "paused"

        await manager.human_intervention(
            AWIHumanIntervention(session_id=session.session_id, action="resume")
        )
        allowed = await manager.execute_action(
            AWIExecutionRequest(
                session_id=session.session_id,
                action=AWIStandardAction.NAVIGATE_TO,
                parameters={"url": "https://example.com/allowed"},
            )
        )
        assert allowed.status == "success"


class TestHumanIntervention:
    @pytest.mark.anyio
    async def test_unknown_session_reports_not_found(self):
        manager = AWISessionManager()

        result = await manager.human_intervention(
            AWIHumanIntervention(session_id="awi-missing", action="pause")
        )

        assert result == {"success": False, "error": "Session not found"}

    @pytest.mark.anyio
    async def test_unknown_action_reports_error(self):
        manager = AWISessionManager()
        session = await manager.create_session(_create_request())

        result = await manager.human_intervention(
            AWIHumanIntervention(session_id=session.session_id, action="dance")
        )

        assert result["success"] is False
        assert "dance" in result["error"]

    @pytest.mark.anyio
    async def test_steer_without_instructions_is_rejected(self):
        manager = AWISessionManager()
        session = await manager.create_session(_create_request())

        result = await manager.human_intervention(
            AWIHumanIntervention(session_id=session.session_id, action="steer")
        )

        assert result == {
            "success": False,
            "error": "No steering instructions provided",
        }


class TestRepresentationAndLifecycle:
    @pytest.mark.anyio
    async def test_request_representation_unknown_session_returns_none(self):
        manager = AWISessionManager()

        result = await manager.request_representation(
            AWIRepresentationRequest(
                session_id="awi-missing",
                representation_type=AWIRepresentationType.SUMMARY,
            )
        )

        assert result is None

    @pytest.mark.anyio
    async def test_request_representation_records_history(self):
        manager = AWISessionManager()
        session = await manager.create_session(_create_request())

        response = await manager.request_representation(
            AWIRepresentationRequest(
                session_id=session.session_id,
                representation_type=AWIRepresentationType.SUMMARY,
            )
        )

        assert response is not None
        assert response.session_id == session.session_id
        updated = await manager.get_session(session.session_id)
        assert updated is not None
        assert len(updated.representation_history) == 1

    @pytest.mark.anyio
    async def test_destroy_unknown_session_returns_false(self):
        manager = AWISessionManager()
        assert await manager.destroy_session("awi-missing") is False

    @pytest.mark.anyio
    async def test_destroy_removes_session(self):
        manager = AWISessionManager()
        session = await manager.create_session(_create_request())

        assert await manager.destroy_session(session.session_id) is True
        assert await manager.get_session(session.session_id) is None

    def test_cleanup_expired_removes_only_expired(self):
        manager = AWISessionManager()
        assert manager.cleanup_expired() == {"sessions_removed": 0}
