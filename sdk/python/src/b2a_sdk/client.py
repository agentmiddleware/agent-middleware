"""
Agent Middleware API client
============================================

Async Python client for Agent Middleware API.
Provides wallet management, telemetry, and billing for agent swarms.
"""

from __future__ import annotations

import logging
import uuid
import warnings
from contextlib import asynccontextmanager
from typing import Any

import httpx

from .errors import (
    APIError,
    AuthenticationError,
    AuthorizationError,
    DeliveryUncertainError,
    IdempotencyConflictError,
    InsufficientFundsError,
    PermitDeniedError,
    TransportError,
)
from .models import (
    ACPCheckoutRequest,
    ACPCheckoutResponse,
    EvidenceBundle,
    InvocationResult,
    Permit,
    PermitRequest,
    Receipt,
    ReceiptVerification,
    ToolDefinition,
)

logger = logging.getLogger("b2a_sdk")


def new_idempotency_key() -> str:
    """Mint a fresh caller-owned idempotency key.

    Returns a random ``uuid4`` hex string (32 characters, within the
    server's 128-character limit). Use it when starting a new logical
    money-moving action that has no caller-supplied key yet, and reuse
    the returned value for every retry of that same action. Minting a
    second key for a retry turns the retry into a second charge.
    """
    return uuid.uuid4().hex


class DryRunSimulation:
    """
    Context manager for dry-run billing simulation.

    Usage:
        async with b2a.simulate_session(wallet_id) as sim:
            # All charges inside use dry_run=True automatically
            result1 = await some_billable_function(...)
            result2 = await another_function(...)

        print(f"Total simulated cost: {sim.total_cost}")
        print(f"Would succeed: {sim.would_succeed}")
    """

    def __init__(
        self,
        client: AgentMiddlewareClient,
        wallet_id: str,
        session_id: str | None = None,
    ):
        self._client = client
        self.wallet_id = wallet_id
        self.session_id = session_id
        self._charges: list[dict] = []
        self._total_cost = 0.0
        self._would_succeed = True
        self._active = False

    @property
    def total_cost(self) -> float:
        """Total credits simulated."""
        return self._total_cost

    @property
    def charge_count(self) -> int:
        """Number of simulated charges."""
        return len(self._charges)

    @property
    def would_succeed(self) -> bool:
        """Whether all simulated charges would succeed."""
        return self._would_succeed

    @property
    def charges(self) -> list[dict]:
        """List of simulated charges."""
        return self._charges.copy()

    def add_charge_result(self, result: dict) -> None:
        """Add a simulated charge result."""
        if result.get("would_succeed", False):
            self._charges.append(result)
            self._total_cost += result.get("credits_would_charge", 0)
        else:
            self._would_succeed = False

    async def end(self) -> dict:
        """End the simulation session and get summary."""
        if self.session_id:
            try:
                response = await self._client._client.delete(
                    f"/v1/billing/dry-run/session/{self.session_id}"
                )
                if response.status_code == 200:
                    return response.json()
            except Exception as e:
                logger.warning(f"Failed to end dry-run session: {e}")

        return {
            "session_id": self.session_id or "unknown",
            "wallet_id": self.wallet_id,
            "total_simulated_credits": self._total_cost,
            "charge_count": self.charge_count,
            "would_succeed": self._would_succeed,
            "charges": self._charges,
        }


class AgentMiddlewareClient:
    """
    Core async client for Agent Middleware API.

    Usage:
        client = AgentMiddlewareClient(api_key="agt-xyz123")
        await client.discover_tools()
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.thisisatest.tech",
        timeout: float = 10.0,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        bearer_token: str | None = None,
    ) -> None:
        """
        Initialize the B2A client.

        Args:
            api_key: API key for authentication (X-API-Key header)
            base_url: Base URL of the middleware API
            timeout: Request timeout in seconds
            transport: Optional HTTPX transport, primarily for in-process tests
            bearer_token: Optional Bearer token for IGA-governed flows. Omit it
                to send no Authorization header; a blank string is rejected
                rather than sent as a malformed ``Bearer `` header, so an unset
                environment variable fails here instead of at the server.

        Raises:
            ValueError: if bearer_token is supplied but blank.
        """
        # The key is held only in the transport's default headers (see the
        # ``api_key`` property), not duplicated as instance state, so debug
        # dumps of ``vars(client)`` do not carry the secret.
        self.base_url = base_url.rstrip("/")
        headers = {
            "X-API-Key": api_key,
            "Content-Type": "application/json",
            "User-Agent": "b2a-sdk/0.5.0",
        }
        if bearer_token is not None:
            token = bearer_token.strip()
            if not token:
                raise ValueError(
                    "bearer_token must not be blank; omit it to send no Authorization header"
                )
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
        )

    @property
    def api_key(self) -> str:
        """The API key this client sends as ``X-API-Key`` (read-only)."""
        return self._client.headers["X-API-Key"]

    @staticmethod
    def _validate_idempotency_key(idempotency_key: str) -> str:
        key = idempotency_key.strip()
        if not key:
            raise ValueError("idempotency_key must not be blank")
        if len(key) > 128:
            raise ValueError("idempotency_key must be at most 128 characters")
        return key

    @staticmethod
    def _error_detail(payload: dict[str, Any], fallback: str) -> str:
        detail = payload.get("detail")
        if isinstance(detail, str) and detail:
            return detail
        if isinstance(detail, dict):
            nested = detail.get("error") or detail.get("detail")
            if isinstance(nested, str) and nested:
                return nested
        error = payload.get("error")
        if isinstance(error, str) and error:
            return error
        return fallback

    def _raise_http_error(
        self,
        *,
        status_code: int,
        payload: dict[str, Any],
        wallet_id: str | None,
    ) -> None:
        detail = self._error_detail(payload, f"HTTP {status_code}")
        if status_code == 401:
            raise AuthenticationError(
                detail,
                status_code=status_code,
                payload=payload,
            )
        if status_code == 402:
            body_detail = payload.get("detail")
            detail_payload = body_detail if isinstance(body_detail, dict) else {}
            raise InsufficientFundsError(
                wallet_id=wallet_id or "unknown",
                shortfall=detail_payload.get("shortfall"),
                top_up_url=detail_payload.get("top_up_url"),
                payload=payload,
            )
        if status_code == 403:
            if detail.startswith("permit_"):
                raise PermitDeniedError(detail, payload=payload)
            raise AuthorizationError(
                detail,
                status_code=status_code,
                payload=payload,
            )
        if status_code == 409:
            raise IdempotencyConflictError(
                detail,
                status_code=status_code,
                payload=payload,
            )
        raise APIError(detail, status_code=status_code, payload=payload)

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        wallet_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise TransportError(f"{method} {path} failed: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise APIError(
                "invalid_json_response",
                status_code=response.status_code,
            ) from exc
        if not isinstance(payload, dict):
            raise APIError(
                "invalid_object_response",
                status_code=response.status_code,
            )
        if response.is_error:
            self._raise_http_error(
                status_code=response.status_code,
                payload=payload,
                wallet_id=wallet_id,
            )
        return payload

    @staticmethod
    def _parse_receipt(data: Any, *, response_name: str) -> Receipt:
        if not isinstance(data, dict):
            raise APIError(f"{response_name}_missing_receipt")
        try:
            return Receipt.from_dict(data)
        except ValueError as exc:
            raise APIError(f"invalid_{response_name}_receipt: {exc}") from exc

    async def discover_tools(self) -> list[ToolDefinition]:
        """Return the executable tools advertised by the MCP gateway."""
        payload = await self._request_json("GET", "/mcp/tools.json")
        tools = payload.get("tools")
        if not isinstance(tools, list):
            raise APIError("invalid_tools_response")
        if not all(isinstance(tool, dict) for tool in tools):
            raise APIError("invalid_tools_response", payload=payload)
        try:
            return [ToolDefinition.from_dict(tool) for tool in tools]
        except (TypeError, ValueError) as exc:
            raise APIError(f"invalid_tools_response: {exc}", payload=payload) from exc

    async def create_permit(
        self,
        request: PermitRequest,
        *,
        idempotency_key: str,
    ) -> Permit:
        """Create a signed permit using a caller-owned idempotency key."""
        key = self._validate_idempotency_key(idempotency_key)
        payload = await self._request_json(
            "POST",
            "/v1/permits",
            wallet_id=request.issuer_wallet_id,
            headers={"Idempotency-Key": key},
            json=request.to_payload(),
        )
        try:
            return Permit.from_dict(payload)
        except ValueError as exc:
            raise APIError(f"invalid_permit_response: {exc}", payload=payload) from exc

    def _raise_mcp_error(
        self,
        error: dict[str, Any],
        *,
        wallet_id: str,
        envelope: dict[str, Any],
    ) -> None:
        reason = str(error.get("message") or "mcp_call_failed")
        data = error.get("data")
        error_data = data if isinstance(data, dict) else {}
        receipt_data = error_data.get("receipt")
        receipt: Receipt | None = None
        if isinstance(receipt_data, dict):
            receipt = self._parse_receipt(receipt_data, response_name="mcp_error")
        receipt_id = receipt.receipt_id if receipt else None
        outcome = receipt.outcome if receipt else None

        if outcome == "delivery_uncertain" or reason in {
            "delivery_uncertain",
            "outcome_unknown",
        }:
            if not receipt_id:
                raise APIError("delivery_uncertain_without_receipt", payload=envelope)
            raise DeliveryUncertainError(
                receipt_id=receipt_id,
                detail=reason,
                payload=envelope,
            )
        if reason == "insufficient_funds" or outcome == "insufficient_funds":
            raise InsufficientFundsError(
                wallet_id=wallet_id,
                receipt_id=receipt_id,
                payload=envelope,
            )
        if reason in {"idempotency_in_progress", "idempotency_key_reused"}:
            raise IdempotencyConflictError(
                reason,
                status_code=409,
                payload=envelope,
            )
        if reason.startswith("permit_"):
            raise PermitDeniedError(
                reason,
                receipt_id=receipt_id,
                payload=envelope,
            )
        if error.get("code") == -32003:
            raise AuthorizationError(reason, status_code=403, payload=envelope)
        raise APIError(reason, payload=envelope)

    async def invoke_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        wallet_id: str,
        permit_id: str,
        idempotency_key: str,
    ) -> InvocationResult:
        """Invoke one MCP tool through the governed permit and receipt loop."""
        key = self._validate_idempotency_key(idempotency_key)
        request_payload = {
            "jsonrpc": "2.0",
            "id": key,
            "method": "tools/call",
            "params": {
                "name": name,
                "arguments": arguments,
                "mcpContext": {
                    "wallet_id": wallet_id,
                    "permit_id": permit_id,
                    "idempotency_key": key,
                },
            },
        }
        envelope = await self._request_json(
            "POST",
            "/mcp/messages",
            wallet_id=wallet_id,
            headers={"Idempotency-Key": key},
            json=request_payload,
        )
        error = envelope.get("error")
        if isinstance(error, dict):
            self._raise_mcp_error(error, wallet_id=wallet_id, envelope=envelope)

        result = envelope.get("result")
        if not isinstance(result, dict):
            raise APIError("invalid_mcp_result", payload=envelope)
        receipt = self._parse_receipt(result.get("receipt"), response_name="mcp")
        if receipt.outcome == "delivery_uncertain":
            raise DeliveryUncertainError(
                receipt_id=receipt.receipt_id,
                payload=envelope,
            )
        if result.get("isError") is True:
            raise APIError(
                str(result.get("error") or "mcp_call_failed"),
                payload=envelope,
            )
        content = result.get("content")
        if not isinstance(content, list) or not all(isinstance(item, dict) for item in content):
            raise APIError("invalid_mcp_content", payload=envelope)
        structured_content = result.get("structuredContent")
        if structured_content is not None and not isinstance(structured_content, dict):
            raise APIError("invalid_mcp_structured_content", payload=envelope)
        return InvocationResult(
            content=content,
            structured_content=(
                dict(structured_content) if isinstance(structured_content, dict) else None
            ),
            receipt=receipt,
            raw=result,
        )

    async def get_receipt(self, receipt_id: str) -> Receipt:
        """Fetch a wallet-authorized signed receipt."""
        payload = await self._request_json("GET", f"/v1/receipts/{receipt_id}")
        return self._parse_receipt(payload, response_name="get")

    async def verify_receipt(self, receipt_id: str) -> ReceiptVerification:
        """Ask the trust plane to verify a receipt signature and linkage."""
        payload = await self._request_json(
            "POST",
            "/v1/receipts/verify",
            json={"receipt_id": receipt_id},
        )
        receipt_data = payload.get("receipt")
        receipt = (
            self._parse_receipt(receipt_data, response_name="verify")
            if receipt_data is not None
            else None
        )
        valid = payload.get("valid")
        if not isinstance(valid, bool):
            raise APIError("invalid_receipt_verification", payload=payload)
        return ReceiptVerification(
            valid=valid,
            reason=(str(payload["reason"]) if payload.get("reason") is not None else None),
            receipt=receipt,
            raw=payload,
        )

    async def get_evidence(self, receipt_id: str) -> EvidenceBundle:
        """Fetch the buyer-facing verification bundle for a receipt."""
        payload = await self._request_json("GET", f"/v1/evidence/{receipt_id}")
        receipt = self._parse_receipt(payload.get("receipt"), response_name="evidence")
        permit_data = payload.get("permit")
        permit: Permit | None = None
        if permit_data is not None:
            if not isinstance(permit_data, dict):
                raise APIError("invalid_evidence_permit", payload=payload)
            try:
                permit = Permit.from_dict(permit_data)
            except ValueError as exc:
                raise APIError(f"invalid_evidence_permit: {exc}", payload=payload) from exc
        verification_data = payload.get("verification")
        if not isinstance(verification_data, dict):
            raise APIError("invalid_evidence_verification", payload=payload)
        valid = payload.get("valid")
        if not isinstance(valid, bool):
            raise APIError("invalid_evidence_validity", payload=payload)
        ledger_entry = payload.get("ledger_entry")
        audit_event = payload.get("audit_event")
        dispatch = payload.get("dispatch")
        return EvidenceBundle(
            receipt_id=str(payload.get("receipt_id") or receipt.receipt_id),
            valid=valid,
            receipt=receipt,
            permit=permit,
            ledger_entry=(dict(ledger_entry) if isinstance(ledger_entry, dict) else None),
            audit_event=(dict(audit_event) if isinstance(audit_event, dict) else None),
            verification={str(key): str(value) for key, value in verification_data.items()},
            dispatch=(dict(dispatch) if isinstance(dispatch, dict) else None),
            raw=payload,
        )

    async def charge(
        self,
        wallet_id: str,
        service_category: str,
        units: float = 1.0,
        request_path: str | None = None,
        description: str | None = None,
        *,
        idempotency_key: str | None = None,
        max_attempts: int = 1,
    ) -> dict[str, Any]:
        """
        Charge a wallet for API usage (micro-metering).

        Every charge carries an ``Idempotency-Key``. Pass a caller-owned key
        and reuse it for every retry of the same logical charge; the server
        then replays the original outcome instead of debiting twice. When no
        key is passed the SDK mints one per ``charge()`` call, so a single
        call is protected but two separate calls (an ordinary caller-level
        retry after a timeout) are two charges: callers that retry across
        ``charge()`` calls must pass their own key, or use ``max_attempts``
        so the SDK retries internally with the same key.

        Args:
            wallet_id: The wallet to charge
            service_category: Service category (e.g., "iot_bridge", "content_factory")
            units: Number of units consumed (default: 1.0)
            request_path: Optional API path for tracking
            description: Optional description of the charge
            idempotency_key: Optional caller-owned key sent as
                ``Idempotency-Key``; must be nonblank and at most 128
                characters. When omitted the SDK mints one fresh key for
                this call (see :func:`new_idempotency_key`).
            max_attempts: How many times to attempt the charge after a
                transport-level failure (a timeout, a dropped connection)
                before giving up. Every attempt reuses the same key; a new
                key is never minted for a retry. Must be a positive integer.

        Returns:
            Ledger entry with action, amount, balance_after

        Raises:
            InsufficientFundsError: If wallet has insufficient balance
            IdempotencyConflictError: If the key was already used for a
                different charge, or that charge is still in progress
            ValueError: If ``idempotency_key`` is blank or too long, or
                ``max_attempts`` is not a positive integer (nothing is sent)
        """
        if not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        # Minted once per charge() call, before any attempt: retries below
        # reuse this same key, so a retried charge replays instead of
        # billing again.
        key = (
            self._validate_idempotency_key(idempotency_key)
            if idempotency_key is not None
            else new_idempotency_key()
        )
        headers = {"Idempotency-Key": key}

        params: dict[str, str | float] = {
            "wallet_id": wallet_id,
            "service": service_category,
            "units": units,
        }
        if request_path:
            params["request_path"] = request_path
        if description:
            params["description"] = description

        attempts = 0
        while True:
            attempts += 1
            try:
                response = await self._client.post(
                    "/v1/billing/charge", params=params, headers=headers
                )
            except httpx.HTTPError:
                if attempts >= max_attempts:
                    raise
                continue
            break

        if response.status_code == 402:
            data = response.json().get("detail", {})
            raise InsufficientFundsError(
                wallet_id=wallet_id,
                shortfall=data.get("shortfall", "unknown"),
                top_up_url=f"{self.base_url}/dashboard/top-up?wallet={wallet_id}",
            )
        # Every charge now carries a key, so a 409 is always an idempotency
        # conflict (key reused with different terms, or charge in progress).
        if response.status_code == 409:
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if not isinstance(payload, dict):
                payload = {}
            raise IdempotencyConflictError(
                self._error_detail(payload, "HTTP 409"),
                status_code=409,
                payload=payload,
            )

        response.raise_for_status()
        return response.json()

    async def telemetry(
        self,
        event_type: str,
        source: str,
        message: str,
        severity: str = "info",
        stack_trace: str | None = None,
        **metadata: Any,
    ) -> None:
        """
        Fire-and-forget telemetry ingestion.

        Telemetry failures are silently swallowed to prevent
        blocking agent execution.

        Args:
            event_type: Event type (error, warning, api_call, etc.)
            source: Originating service/module name
            message: Human-readable event message
            severity: Severity level (critical, high, medium, low, info)
            stack_trace: Optional stack trace for errors
            **metadata: Additional structured context
        """
        payload = {
            "event_type": event_type,
            "source": source,
            "message": message,
            "severity": severity,
            "metadata": metadata,
        }
        if stack_trace:
            payload["stack_trace"] = stack_trace

        try:
            response = await self._client.post(
                "/v1/telemetry/events/single",
                json=payload,
            )
            response.raise_for_status()
        except Exception as e:
            logger.debug(f"Telemetry push failed (non-blocking): {e}")

    async def create_sponsor_wallet(
        self,
        sponsor_name: str,
        email: str,
        initial_credits: float = 0.0,
    ) -> dict[str, Any]:
        """
        Create a sponsor (liability sink) wallet.

        Args:
            sponsor_name: Name of the human sponsor
            email: Sponsor's email address
            initial_credits: Starting credit balance

        Returns:
            Created wallet details including wallet_id
        """
        response = await self._client.post(
            "/v1/billing/wallets/sponsor",
            json={
                "sponsor_name": sponsor_name,
                "email": email,
                "initial_credits": initial_credits,
            },
        )
        response.raise_for_status()
        return response.json()

    async def create_agent_wallet(
        self,
        sponsor_wallet_id: str,
        agent_id: str,
        budget_credits: float,
        daily_limit: float | None = None,
    ) -> dict[str, Any]:
        """
        Provision an agent wallet from a sponsor's balance.

        Args:
            sponsor_wallet_id: ID of the sponsoring wallet
            agent_id: Unique identifier for the agent
            budget_credits: Credits to provision
            daily_limit: Optional daily spending limit; 0 means "spend nothing",
                None means no cap

        Returns:
            Created agent wallet details
        """
        payload = {
            "sponsor_wallet_id": sponsor_wallet_id,
            "agent_id": agent_id,
            "budget_credits": budget_credits,
        }
        # `is not None`: a truthiness test dropped 0, and the server reads an
        # absent daily_limit as "no cap at all" -- the opposite of 0.
        if daily_limit is not None:
            payload["daily_limit"] = daily_limit

        response = await self._client.post(
            "/v1/billing/wallets/agent",
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    async def get_wallet(self, wallet_id: str) -> dict[str, Any]:
        """Get wallet details by ID."""
        response = await self._client.get(f"/v1/billing/wallets/{wallet_id}")
        response.raise_for_status()
        return response.json()

    async def list_wallets(
        self,
        wallet_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """List all wallets, optionally filtered by type."""
        params = {}
        if wallet_type:
            params["wallet_type"] = wallet_type

        response = await self._client.get("/v1/billing/wallets", params=params)
        response.raise_for_status()
        return response.json()["wallets"]

    async def get_balance(self, wallet_id: str) -> float:
        """Get the current balance of a wallet."""
        wallet = await self.get_wallet(wallet_id)
        return wallet["balance"]

    async def get_pricing(self) -> dict[str, Any]:
        """Get the current pricing table."""
        response = await self._client.get("/v1/billing/pricing")
        response.raise_for_status()
        return response.json()

    async def prepare_top_up(
        self,
        wallet_id: str,
        amount_fiat: float,
        currency: str = "USD",
    ) -> dict[str, Any]:
        """
        Prepare a fiat top-up via Stripe.

        Returns a client_secret for Stripe Elements.

        Args:
            wallet_id: Wallet to credit
            amount_fiat: Amount in fiat currency
            currency: Currency code (default: USD)

        Returns:
            Stripe PaymentIntent details including client_secret
        """
        response = await self._client.post(
            "/v1/billing/top-up/prepare",
            params={
                "wallet_id": wallet_id,
                "amount_fiat": amount_fiat,
                "currency": currency,
            },
        )
        response.raise_for_status()
        return response.json()

    async def health_check(self) -> dict[str, Any]:
        """Check API health status."""
        response = await self._client.get("/health")
        response.raise_for_status()
        return response.json()

    @asynccontextmanager
    async def simulate_session(self, wallet_id: str):
        """
        Context manager for dry-run billing simulation.

        All @billable decorated functions called within this context
        will use dry_run=True and be tracked by the shadow ledger.

        Usage:
            async with b2a.simulate_session(wallet_id="agent-001") as sim:
                # Simulate charges without real deductions
                await generate_video(url)
                await process_data(operation="transform", data="hello")

            print(f"Total cost: {sim.total_cost}")
            print(f"Would succeed: {sim.would_succeed}")

        Args:
            wallet_id: Wallet to simulate charges against

        Yields:
            DryRunSimulation object with tracking and summary methods
        """
        from .decorators import _dry_run_context

        response = await self._client.post(
            "/v1/billing/dry-run/session",
            json={"wallet_id": wallet_id},
        )
        response.raise_for_status()
        session_data = response.json()
        session_id = session_data.get("session_id")

        sim = DryRunSimulation(
            client=self,
            wallet_id=wallet_id,
            session_id=session_id,
        )
        sim._active = True

        token = _dry_run_context.set(sim)

        try:
            yield sim
        finally:
            _dry_run_context.reset(token)
            sim._active = False
            await sim.end()

    async def simulate_charge(
        self,
        wallet_id: str,
        service_category: str,
        units: float = 1.0,
        session_id: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """
        Simulate a charge without affecting real balance.

        Args:
            wallet_id: Wallet being simulated
            service_category: Service category to simulate
            units: Number of units
            session_id: Optional session for cumulative tracking
            description: Optional description

        Returns:
            SimulatedChargeResult with cost estimate
        """
        params = {
            "wallet_id": wallet_id,
            "service": service_category,
            "units": units,
        }
        if session_id:
            params["dry_run_session_id"] = session_id
        if description:
            params["description"] = description

        response = await self._client.post(
            "/v1/billing/dry-run/charge",
            json=params,
        )
        response.raise_for_status()
        return response.json()

    async def get_dry_run_estimate(
        self,
        wallet_id: str,
        service_category: str,
        units: float = 1.0,
    ) -> dict[str, Any]:
        """
        Convenience method for single-shot cost estimation without session overhead.

        Use this for quick price checks when you don't need cumulative tracking.

        Args:
            wallet_id: Wallet to check
            service_category: Service category to estimate
            units: Number of units

        Returns:
            Dict with credits_would_charge, would_succeed, etc.

        Example:
            estimate = await b2a.get_dry_run_estimate(
                "agent-001",
                "content_factory",
                units=10.0,
            )
            print(f"Would cost: {estimate['credits_would_charge']} credits")
        """
        return await self.simulate_charge(
            wallet_id=wallet_id,
            service_category=service_category,
            units=units,
            session_id=None,
        )

    async def acp_checkout(
        self,
        request: ACPCheckoutRequest,
        *,
        sponsor_wallet_id: str,
        agent_wallet_id: str,
        idempotency_key: str,
    ) -> ACPCheckoutResponse:
        """Create an ACP (Agentic Commerce Protocol) checkout.

        The server mints the permit; this is not invoke_tool. The checkout
        creates a permit, settles the payment via the Shared Payment Token,
        and returns the trust-plane evidence identifiers.

        Dormant surface: ``/v1/billing/acp/checkout`` lives on the server's
        ``billing.expansion_router``, which is mounted only when
        ``ENABLE_PROOF_SURFACES=true``. A deployment with proof surfaces off —
        the supported production default — returns 404 for this call. Treat
        this client method as proof-only until that surface is unfrozen.

        Args:
            request: ACP checkout request with line items and SPT token
            sponsor_wallet_id: Sponsor wallet that issues the checkout permit
            agent_wallet_id: Agent wallet the permit is bound to
            idempotency_key: Caller-supplied idempotency key

        Returns:
            ACPCheckoutResponse with permit_id, receipt_id, and derived total
        """
        key = self._validate_idempotency_key(idempotency_key)
        # Both wallet ids are required *query* params on the server route; a
        # body-only request is refused with 422 before authorization.
        payload = await self._request_json(
            "POST",
            "/v1/billing/acp/checkout",
            headers={"Idempotency-Key": key},
            params={
                "sponsor_wallet_id": sponsor_wallet_id,
                "agent_wallet_id": agent_wallet_id,
            },
            json=request.to_payload(),
        )
        try:
            return ACPCheckoutResponse.from_dict(payload)
        except ValueError as exc:
            raise APIError(f"invalid_acp_checkout_response: {exc}", payload=payload) from exc

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def __aenter__(self) -> AgentMiddlewareClient:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()


class B2AClient(AgentMiddlewareClient):
    """Deprecated compatibility name for :class:`AgentMiddlewareClient`."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        warnings.warn(
            "B2AClient is deprecated; use AgentMiddlewareClient",
            DeprecationWarning,
            stacklevel=2,
        )
        super().__init__(*args, **kwargs)


__all__ = [
    "AgentMiddlewareClient",
    "B2AClient",
    "DryRunSimulation",
    "new_idempotency_key",
]
