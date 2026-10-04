"""
AWI Session Manager — Phase 7
==============================
Stateful AWI session manager that ties together actions, representations,
and task queues into cohesive agentic web interactions.

Based on arXiv:2506.10953v1 - "Build the web for agents, not agents for the web"

Provides:
- Stateful sessions with persistent context
- Action execution with vocabulary validation
- Progressive representation generation
- Human pause/steer capabilities
- Integration with MCP layer
- Phase 9: Playwright DOM bridge routing
"""

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from ..core.durable_state import get_durable_state
from ..schemas.awi import (
    AWIExecutionRequest,
    AWIExecutionResponse,
    AWIHumanIntervention,
    AWIRepresentationRequest,
    AWIRepresentationResponse,
    AWISession,
    AWISessionCreate,
    AWISessionStatus,
    AWIStandardAction,
)
from .awi_action_vocab import get_awi_vocabulary
from .awi_representation import get_awi_representation
from .awi_playwright_bridge import get_playwright_bridge
from .audit_log import record_audit_event

logger = logging.getLogger(__name__)


class AWISessionManager:
    """
    Stateful AWI session manager.

    Based on the paper's principle: "Stateful interfaces (AWI)" -
    MCP is great but insufficient for web agents; they need stateful
    interfaces with persistent context.

    Phase 9: Supports Playwright DOM bridge routing for live browser automation.
    """

    def __init__(self):
        self._sessions: dict[str, AWISession] = {}
        self._session_state: dict[str, dict[str, Any]] = {}
        self._state = get_durable_state()
        self._vocabulary = get_awi_vocabulary()
        self._representation = get_awi_representation()
        self._playwright_bridge = get_playwright_bridge()
        self._dom_sessions: dict[
            str, str
        ] = {}  # Maps AWI session_id -> DOM bridge session_id

    @staticmethod
    def _session_key(session_id: str) -> str:
        return f"awi.sessions.{session_id}"

    @staticmethod
    def _session_state_key(session_id: str) -> str:
        return f"awi.session_state.{session_id}"

    def _initial_session_state(self, request: AWISessionCreate) -> dict[str, Any]:
        return {
            "target_url": request.target_url,
            "current_url": request.target_url,
            "cookies": {},
            "local_storage": {},
            "session_storage": {},
            "form_data": {},
            "capabilities": ["page_loaded"],
            "page_state": {
                "html": "<html><body>Initial page</body></html>",
                "title": request.target_url,
                "url": request.target_url,
                "elements": [],
            },
        }

    async def _save_session(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if not session:
            return

        await self._state.save_json(
            self._session_key(session_id), session.model_dump(mode="json")
        )
        await self._state.save_json(
            self._session_state_key(session_id),
            self._session_state.get(session_id, {}),
        )

    async def _load_session(self, session_id: str) -> AWISession | None:
        session = self._sessions.get(session_id)
        if session:
            return session

        payload = await self._state.load_json(self._session_key(session_id))
        if not isinstance(payload, dict):
            return None

        session = AWISession.model_validate(payload)
        raw_state = await self._state.load_json(self._session_state_key(session_id))
        session_state = raw_state if isinstance(raw_state, dict) else {}

        self._sessions[session_id] = session
        self._session_state[session_id] = session_state
        dom_session_id = session_state.get("dom_session_id")
        if isinstance(dom_session_id, str):
            self._dom_sessions[session_id] = dom_session_id
        return session

    async def create_session(self, request: AWISessionCreate) -> AWISession:
        """Create a new AWI session."""
        session_id = f"awi-{uuid.uuid4().hex[:12]}"

        session = AWISession(
            session_id=session_id,
            target_url=request.target_url,
            wallet_id=request.wallet_id,
            status=AWISessionStatus.CREATED,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            current_url=request.target_url,
            max_steps=request.max_steps,
            timeout_seconds=request.timeout_seconds,
            human_pause_enabled=request.allow_human_pause,
            representation_history=[],
            action_history=[],
        )

        self._sessions[session_id] = session
        self._session_state[session_id] = self._initial_session_state(request)
        await self._save_session(session_id)

        logger.info(f"Created AWI session: {session_id}")
        return session

    async def get_session(self, session_id: str) -> AWISession | None:
        """Get an existing session."""
        return await self._load_session(session_id)

    async def execute_action(
        self, request: AWIExecutionRequest
    ) -> AWIExecutionResponse:
        """Execute an AWI action within a session."""
        response_parameters = self._vocabulary.redact_parameters(
            request.action, request.parameters
        )
        if request.dry_run:
            return AWIExecutionResponse(
                execution_id=f"exec-{uuid.uuid4().hex[:12]}",
                session_id=request.session_id,
                action=request.action,
                status="error",
                parameters=response_parameters,
                effect_status="not_dispatched",
                error="dry_run_unsupported",
            )
        session = await self._load_session(request.session_id)
        if not session:
            return AWIExecutionResponse(
                execution_id=f"exec-{uuid.uuid4().hex[:12]}",
                session_id=request.session_id,
                action=request.action,
                status="error",
                parameters=response_parameters,
                effect_status="not_dispatched",
                error=f"Session not found: {request.session_id}",
            )

        if session.status == AWISessionStatus.PAUSED and session.paused_by_human:
            return AWIExecutionResponse(
                execution_id=f"exec-{uuid.uuid4().hex[:12]}",
                session_id=request.session_id,
                action=request.action,
                status="paused",
                parameters=response_parameters,
                effect_status="not_dispatched",
                error="Session is paused by human intervention",
            )

        if session.step_count >= session.max_steps:
            session.status = AWISessionStatus.COMPLETED
            session.updated_at = datetime.now(timezone.utc)
            await self._save_session(request.session_id)
            return AWIExecutionResponse(
                execution_id=f"exec-{uuid.uuid4().hex[:12]}",
                session_id=request.session_id,
                action=request.action,
                status="max_steps_reached",
                parameters=response_parameters,
                effect_status="not_dispatched",
                error="Maximum steps reached",
            )

        execution_id = f"awi-exec-{uuid.uuid4().hex[:12]}"
        start_time = datetime.now(timezone.utc)

        is_valid, error = self._vocabulary.validate_parameters(
            request.action, request.parameters
        )
        if not is_valid:
            return AWIExecutionResponse(
                execution_id=execution_id,
                session_id=request.session_id,
                action=request.action,
                status="error",
                parameters=response_parameters,
                effect_status="not_dispatched",
                error=error,
            )

        state = self._session_state.get(request.session_id, {})
        preconditions_met, unmet = self._vocabulary.check_preconditions(
            request.action, state
        )

        if not preconditions_met:
            return AWIExecutionResponse(
                execution_id=execution_id,
                session_id=request.session_id,
                action=request.action,
                status="error",
                parameters=response_parameters,
                effect_status="not_dispatched",
                error=f"Preconditions not met: {unmet}",
            )

        # Phase 9: Check passkey requirement for high-risk actions
        from .webauthn_provider import get_webauthn_provider

        webauthn = get_webauthn_provider()

        if await webauthn.requires_passkey(request.session_id, request.action.value):
            if not await webauthn.is_action_verified(
                request.session_id, request.action.value
            ):
                return AWIExecutionResponse(
                    execution_id=execution_id,
                    session_id=request.session_id,
                    action=request.action,
                    status="passkey_required",
                    parameters=response_parameters,
                    effect_status="not_dispatched",
                    error="This action requires biometric verification. "
                    "Call POST /v1/awi/passkey/challenge first.",
                )

        # Phase 9: Route to Playwright DOM bridge if attached
        dom_session_id = self._dom_sessions.get(request.session_id)
        if dom_session_id:
            logger.info(
                f"Routing action {request.action.value} to live Playwright DOM bridge for session {request.session_id}"
            )
            try:
                result = await self._execute_via_dom_bridge(
                    request.session_id,
                    dom_session_id,
                    request.action,
                    request.parameters,
                )
            except Exception as e:
                # A failed live browser action must surface as a failure. The
                # previous fallback re-ran the mock logic and reported
                # ``status="success"``, so the governed route signed a success
                # receipt. An exception does not prove absence of partial
                # browser effects; retain that uncertainty in the response.
                logger.warning(f"DOM bridge routing failed: {e}")
                return AWIExecutionResponse(
                    execution_id=execution_id,
                    session_id=request.session_id,
                    action=request.action,
                    status="error",
                    parameters=response_parameters,
                    effect_status="unknown",
                    error=f"dom_bridge_failed: {e}",
                )
            if not result.get("success", False):
                return AWIExecutionResponse(
                    execution_id=execution_id,
                    session_id=request.session_id,
                    action=request.action,
                    status="error",
                    parameters=response_parameters,
                    result=result,
                    effect_status="unknown",
                    error="dom_bridge_failed",
                )
        else:
            # Fall back to the existing mock/internal logic for headless/API-only AWI sessions
            result = await self._execute_action_logic(
                request.action, request.parameters, state
            )

        session.action_history.append(
            {
                "execution_id": execution_id,
                "action": request.action.value,
                "parameters": response_parameters,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "success": result.get("success", True),
            }
        )

        session.step_count += 1
        session.updated_at = datetime.now(timezone.utc)
        session.status = AWISessionStatus.ACTIVE
        if isinstance(state.get("current_url"), str):
            session.current_url = state["current_url"]

        representation = None
        if request.representation_request:
            rep_result = await self._representation.generate_representation(
                request.session_id,
                request.representation_request,
                state.get("page_state", {}),
                {},
            )
            session.representation_history.append(rep_result)
            representation = rep_result

        duration_ms = int(
            (datetime.now(timezone.utc) - start_time).total_seconds() * 1000
        )
        await self._save_session(request.session_id)

        return AWIExecutionResponse(
            execution_id=execution_id,
            session_id=request.session_id,
            action=request.action,
            status="success",
            parameters=response_parameters,
            result=result,
            new_state=state,
            representation=representation,
            duration_ms=duration_ms,
        )

    async def _execute_action_logic(
        self, action: AWIStandardAction, params: dict[str, Any], state: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute the logic for an AWI action."""
        if action == AWIStandardAction.NAVIGATE_TO:
            new_url = params.get("url", state.get("current_url"))
            state["current_url"] = new_url
            state["capabilities"].append("page_loaded")
            return {"success": True, "new_url": new_url}

        elif action == AWIStandardAction.SEARCH_AND_SORT:
            query = params.get("query", "")
            state["last_search"] = query
            state["last_sort"] = params.get("sort_by")
            return {
                "success": True,
                "results_count": 0,
                "items": [],
            }

        elif action == AWIStandardAction.FILL_FORM:
            fields = params.get("fields", {})
            redacted_fields = self._vocabulary.redact_parameters(
                AWIStandardAction.FILL_FORM, {"fields": fields}
            ).get("fields", {})
            state["form_data"].update(redacted_fields)
            return {"success": True, "fields_filled": len(fields)}

        elif action == AWIStandardAction.CLICK_BUTTON:
            button_id = params.get("button_id", params.get("button_text", ""))
            return {"success": True, "button_clicked": button_id}

        elif action == AWIStandardAction.GET_REPRESENTATION:
            return {
                "success": True,
                "representation_generated": True,
            }

        else:
            return {"success": True, "action": action.value}

    async def request_representation(
        self, request: AWIRepresentationRequest
    ) -> AWIRepresentationResponse | None:
        """Request a specific representation of the session state."""
        session = await self._load_session(request.session_id)
        if not session:
            return None

        state = self._session_state.get(request.session_id, {})
        page_state = state.get("page_state", {})

        result = await self._representation.generate_representation(
            request.session_id,
            request.representation_type,
            page_state,
            request.options,
        )

        session.representation_history.append(result)
        session.updated_at = datetime.now(timezone.utc)
        await self._save_session(request.session_id)

        return AWIRepresentationResponse(
            representation_id=result.get("representation_id", ""),
            session_id=request.session_id,
            representation_type=request.representation_type,
            content=result.get("content"),
            metadata=result.get("metadata", {}),
            generated_at=datetime.now(timezone.utc),
        )

    async def human_intervention(
        self, intervention: AWIHumanIntervention, auth: Any | None = None
    ) -> dict[str, Any]:
        """Handle human intervention in a session."""
        session = await self._load_session(intervention.session_id)
        if not session:
            return {"success": False, "error": "Session not found"}

        if intervention.action == "pause":
            session.status = AWISessionStatus.PAUSED
            session.paused_by_human = True
            session.pause_reason = intervention.reason
            session.updated_at = datetime.now(timezone.utc)
            await self._save_session(intervention.session_id)
            await self._record_human_intervention_audit(
                session=session,
                intervention=intervention,
                status="paused",
                auth=auth,
            )
            return {"success": True, "status": "paused"}

        elif intervention.action == "resume":
            session.status = AWISessionStatus.ACTIVE
            session.paused_by_human = False
            session.pause_reason = None
            session.updated_at = datetime.now(timezone.utc)
            await self._save_session(intervention.session_id)
            await self._record_human_intervention_audit(
                session=session,
                intervention=intervention,
                status="active",
                auth=auth,
            )
            return {"success": True, "status": "active"}

        elif intervention.action == "steer":
            if not intervention.steer_instructions:
                await self._record_human_intervention_audit(
                    session=session,
                    intervention=intervention,
                    status=session.status.value,
                    auth=auth,
                    ok=False,
                    error="missing_steer_instructions",
                )
                return {"success": False, "error": "No steering instructions provided"}
            instructions_hash = self._hash_text(intervention.steer_instructions)
            session.action_history.append(
                {
                    "type": "human_steer",
                    "instructions_sha256": instructions_hash,
                    "instructions_length": len(intervention.steer_instructions),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
            session.updated_at = datetime.now(timezone.utc)
            await self._save_session(intervention.session_id)
            await self._record_human_intervention_audit(
                session=session,
                intervention=intervention,
                status="steered",
                auth=auth,
            )
            return {
                "success": True,
                "status": "steered",
                "instructions_recorded": True,
                "instructions_sha256": instructions_hash,
            }

        await self._record_human_intervention_audit(
            session=session,
            intervention=intervention,
            status=session.status.value,
            auth=auth,
            ok=False,
            error=f"unknown_action:{intervention.action}",
        )
        return {"success": False, "error": f"Unknown action: {intervention.action}"}

    @staticmethod
    def _hash_text(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    async def _record_human_intervention_audit(
        self,
        *,
        session: AWISession,
        intervention: AWIHumanIntervention,
        status: str,
        auth: Any | None,
        ok: bool = True,
        error: str | None = None,
    ) -> None:
        metadata: dict[str, Any] = {
            "session_id": session.session_id,
            "action": intervention.action,
            "status": status,
            "reason": intervention.reason,
        }
        if intervention.steer_instructions:
            metadata["steer_instructions_sha256"] = self._hash_text(
                intervention.steer_instructions
            )
            metadata["steer_instructions_length"] = len(intervention.steer_instructions)

        try:
            await record_audit_event(
                event="awi.human_intervention",
                wallet_id=session.wallet_id,
                tool="awi",
                endpoint="/v1/awi/intervene",
                auth_source=getattr(auth, "source", None),
                key_id=getattr(auth, "key_id", None),
                request_id=session.session_id,
                ok=ok,
                error=error,
                metadata=metadata,
            )
        except Exception:
            logger.exception(
                "Failed to audit AWI human intervention",
                extra={
                    "event": "awi.human_intervention",
                    "session_id": session.session_id,
                    "wallet_id": session.wallet_id,
                },
            )

    async def destroy_session(self, session_id: str) -> bool:
        """Destroy an AWI session."""
        if not await self._load_session(session_id):
            return False

        # Cleanup DOM bridge if attached
        await self.detach_dom_bridge(session_id)

        self._sessions.pop(session_id, None)
        self._session_state.pop(session_id, None)
        await self._state.delete(self._session_key(session_id))
        await self._state.delete(self._session_state_key(session_id))

        logger.info(f"Destroyed AWI session: {session_id}")
        return True

    # ─────────────────────────────────────────────────────────────────────────
    # Phase 9: Playwright DOM Bridge Integration
    # ─────────────────────────────────────────────────────────────────────────

    async def attach_dom_bridge(
        self, session_id: str, target_url: str | None = None
    ) -> dict[str, Any]:
        """
        Attach a Playwright DOM bridge to an AWI session.

        After attachment, all actions on this session will be routed
        through the real browser via Playwright.

        Args:
            session_id: AWI session to attach.
            target_url: Optional URL override (defaults to session target_url).

        Returns:
            Dict with attachment status and initial DOM state.
        """
        session = await self._load_session(session_id)
        if not session:
            raise ValueError(f"Session not found: {session_id}")

        url = target_url or session.target_url

        dom_session = await self._playwright_bridge.create_session(target_url=url)

        self._dom_sessions[session_id] = dom_session.session_id

        state = self._session_state.get(session_id, {})
        state["dom_attached"] = True
        state["dom_session_id"] = dom_session.session_id
        self._session_state[session_id] = state

        representation = await self._playwright_bridge.extract_state_representation(
            session_id=dom_session.session_id,
            representation_type="summary",
            include_elements=True,
        )

        state["page_state"] = representation
        state["current_url"] = url
        session.updated_at = datetime.now(timezone.utc)
        await self._save_session(session_id)

        logger.info(
            f"Attached DOM bridge to AWI session {session_id} (DOM: {dom_session.session_id})"
        )

        return {
            "status": "attached",
            "session_id": session_id,
            "dom_session_id": dom_session.session_id,
            "elements_count": len(representation.get("interactive_elements", [])),
            "page_type": representation.get("page_type", "unknown"),
        }

    async def detach_dom_bridge(self, session_id: str) -> dict[str, Any]:
        """
        Detach the Playwright DOM bridge from an AWI session.

        Returns the session to mock/internal AWI mode.
        """
        dom_session_id = self._dom_sessions.pop(session_id, None)
        if not dom_session_id:
            return {"status": "not_attached", "session_id": session_id}

        await self._playwright_bridge.destroy_session(dom_session_id)

        state = self._session_state.get(session_id, {})
        state["dom_attached"] = False
        state.pop("dom_session_id", None)
        self._session_state[session_id] = state
        session = await self._load_session(session_id)
        if session:
            session.updated_at = datetime.now(timezone.utc)
            await self._save_session(session_id)

        logger.info(f"Detached DOM bridge from AWI session {session_id}")

        return {
            "status": "detached",
            "session_id": session_id,
            "dom_session_id": dom_session_id,
        }

    async def get_dom_bridge_status(self, session_id: str) -> dict[str, Any]:
        """Check if a session has a DOM bridge attached."""
        await self._load_session(session_id)
        dom_session_id = self._dom_sessions.get(session_id)
        if not dom_session_id:
            return {"attached": False, "session_id": session_id}

        dom_session = await self._playwright_bridge.get_session(dom_session_id)
        if not dom_session:
            return {
                "attached": False,
                "session_id": session_id,
                "error": "DOM session not found",
            }

        return {
            "attached": True,
            "session_id": session_id,
            "dom_session_id": dom_session_id,
            "current_url": dom_session.current_url,
        }

    async def _execute_via_dom_bridge(
        self,
        session_id: str,
        dom_session_id: str,
        action: AWIStandardAction,
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Execute an AWI action via the Playwright DOM bridge.

        Translates the semantic action to Playwright commands, executes them,
        and returns the resulting DOM state.
        """
        commands = await self._playwright_bridge.translate_action(
            session_id=dom_session_id,
            action=action.value,
            parameters=parameters,
        )

        execution = await self._playwright_bridge.execute_commands(
            session_id=dom_session_id,
            commands=commands,
        )

        state = self._session_state.get(session_id, {})

        if execution.success:
            representation = await self._playwright_bridge.extract_state_representation(
                session_id=dom_session_id,
                representation_type="summary",
                include_elements=True,
            )
            state["page_state"] = representation
            state["current_url"] = representation.get("url", state.get("current_url"))

        return {
            "success": execution.success,
            "commands_executed": execution.commands_executed,
            "new_url": execution.new_url,
            "error": execution.error,
            "duration_ms": execution.duration_ms,
        }

    def cleanup_expired(self) -> dict[str, int]:
        """
        Remove expired sessions and their associated state.

        Returns:
            Dict with count of removed sessions.
        """
        now = datetime.now(timezone.utc)
        expired_ids = [
            sid for sid, session in self._sessions.items() if session.expires_at < now
        ]

        for session_id in expired_ids:
            if session_id in self._dom_sessions:
                import asyncio

                asyncio.create_task(
                    self._playwright_bridge.destroy_session(
                        self._dom_sessions[session_id]
                    )
                )
            del self._sessions[session_id]
            if session_id in self._session_state:
                del self._session_state[session_id]
            if session_id in self._dom_sessions:
                del self._dom_sessions[session_id]

        if expired_ids:
            logger.info(f"Cleaned up {len(expired_ids)} expired sessions")

        return {"sessions_removed": len(expired_ids)}

    async def cleanup_expired_async(self) -> dict[str, int]:
        """
        Remove expired sessions from local memory and durable state.

        This is the production cleanup path used by the FastAPI lifespan task.
        The synchronous cleanup_expired method is retained for legacy callers
        that only need to sweep the current process.
        """
        for key in await self._state.list_keys("awi.sessions."):
            session_id = key.removeprefix("awi.sessions.")
            await self._load_session(session_id)

        expired_ids = [
            sid
            for sid, session in self._sessions.items()
            if session.expires_at < datetime.now(timezone.utc)
        ]

        for session_id in expired_ids:
            dom_session_id = self._dom_sessions.pop(session_id, None)
            if dom_session_id:
                await self._playwright_bridge.destroy_session(dom_session_id)

            self._sessions.pop(session_id, None)
            self._session_state.pop(session_id, None)
            await self._state.delete(self._session_key(session_id))
            await self._state.delete(self._session_state_key(session_id))

        if expired_ids:
            logger.info(f"Cleaned up {len(expired_ids)} expired sessions")

        return {"sessions_removed": len(expired_ids)}


_awi_session_manager: AWISessionManager | None = None


def get_awi_session_manager() -> AWISessionManager:
    """Get singleton AWI session manager instance."""
    global _awi_session_manager
    if _awi_session_manager is None:
        _awi_session_manager = AWISessionManager()
    return _awi_session_manager
