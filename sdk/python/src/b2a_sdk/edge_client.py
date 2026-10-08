"""Legacy shared HTTP edge client for framework wrappers.

.. deprecated:: 0.5.0
    The edge client's ``call_mcp_tool`` bypasses the trust-plane loop
    (no permit, no signed receipt). It sends an ``Idempotency-Key`` header,
    but without the governed loop a retry under a different key still
    dispatches and charges again, so callers must reuse the key per logical
    call. Prefer the governed flow below.

    Use ``AgentMiddlewareClient`` with the governed flow instead:
    ``discover_tools() → create_permit() → invoke_tool()`` gives you
    idempotent replay, signed receipts, and scoped authorization.

The framework wrappers (langchain, crewai, autogen) all need the same
narrow surface: list MCP tools, call an MCP tool, drive an AWI session, read
a wallet balance. This base centralizes that surface so each wrapper only has
to add the framework-specific glue.

This is intentionally a smaller surface than ``AgentMiddlewareClient``.
``AgentMiddlewareClient`` is the full agent-facing client used by service code
and decorators; this base is the wrapper-facing edge client used by framework
integrations that just need to talk to the middleware over HTTP.

Governed in-process validation (0.5+)
-------------------------------------

The replacement path for framework middleware lives here as well:

* :class:`LocalPermitValidator` — verifies a permit's Ed25519 signature
  against the trust plane's published keys and mirrors the server's
  per-call permit checks in-process (expiry, tool scope, budget,
  per-tool call caps) with the server's own denial reason strings.
* :class:`GovernedEdgeSession` — fetches the permit and trust keys once,
  verifies the permit locally, then routes every call through the governed
  ``AgentMiddlewareClient.invoke_tool`` loop, skipping the server round
  trip only for calls the cached permit already provably denies.

The local checks are an optimistic latency mirror, never an authorization
authority: the server's ``authorize_and_reserve`` remains the atomic gate
and can still deny a call the local mirror allowed.
"""

from __future__ import annotations

import base64
import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx

from .errors import PermitDeniedError
from .receipt_verifier import (
    VerificationError,
    canonical_json,
    key_set_from_document,
    verify_ed25519,
)

if TYPE_CHECKING:  # imported for annotations only; no runtime coupling
    from .client import AgentMiddlewareClient
    from .models import InvocationResult


class B2AEdgeClient:
    """Shared async HTTP surface for framework wrappers.

    Subclasses can override or extend specific methods to add
    framework-specific affordances (e.g. returning langchain ``Tool`` objects
    instead of raw dicts) without re-implementing the HTTP plumbing.
    """

    def __init__(
        self,
        api_url: str = "http://localhost:8000",
        api_key: str | None = None,
        wallet_id: str | None = None,
        timeout: float = 30.0,
    ):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.wallet_id = wallet_id
        self.timeout = timeout
        self._client = httpx.AsyncClient(timeout=timeout)

    def _auth_headers(self) -> dict[str, str]:
        """Bare auth headers for requests with no body (GET)."""
        return {"X-API-Key": self.api_key} if self.api_key else {}

    def _headers(self) -> dict[str, str]:
        """Auth + JSON headers for requests with a body (POST/PUT/PATCH)."""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    async def get_mcp_tools(self) -> list[dict[str, Any]]:
        """Fetch available MCP tools from the server."""
        response = await self._client.get(
            f"{self.api_url}/mcp/tools.json",
            headers=self._auth_headers(),
        )
        response.raise_for_status()
        return response.json().get("tools", [])

    async def call_mcp_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Call an MCP tool by name.

        .. warning::
            This method bypasses the trust-plane loop: no permit, no signed
            receipt. It does send an ``Idempotency-Key`` header (a caller
            key when given, otherwise one minted for this call), but without
            the governed loop there is no permit binding or signed receipt,
            so a retry with a *different* key still dispatches and charges
            again. Callers that retry one logical call must reuse the key.

            For governed invocations with replay protection and signed receipts,
            use ``AgentMiddlewareClient.invoke_tool()`` instead, which requires
            a permit and an idempotency key.
        """
        if idempotency_key is None:
            key = uuid.uuid4().hex
        else:
            key = idempotency_key.strip() if isinstance(idempotency_key, str) else ""
            if not key:
                raise ValueError("idempotency_key must not be blank")
            if len(key) > 128:
                raise ValueError("idempotency_key must be at most 128 characters")
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": name,
                "arguments": arguments,
            },
            "id": key,
        }
        if self.wallet_id:
            payload["params"]["mcpContext"] = {"wallet_id": self.wallet_id}

        response = await self._client.post(
            f"{self.api_url}/mcp/messages",
            json=payload,
            headers={**self._headers(), "Idempotency-Key": key},
        )
        response.raise_for_status()
        return response.json()

    async def create_awi_session(
        self,
        target_url: str,
        max_steps: int = 100,
    ) -> dict[str, Any]:
        """Create an AWI session for web interaction."""
        payload = {"target_url": target_url, "max_steps": max_steps}
        response = await self._client.post(
            f"{self.api_url}/v1/awi/sessions",
            json=payload,
            headers=self._headers(),
        )
        response.raise_for_status()
        return response.json()

    async def execute_awi_action(
        self,
        session_id: str,
        action: str,
        parameters: dict[str, Any],
        *,
        permit_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Execute an AWI action on a session.

        ``POST /v1/awi/execute`` is governed: it requires a permit for tool
        ``awi_execute`` (``X-Permit-Id``) and an ``Idempotency-Key``. Reuse
        the key when retrying one logical action. Raises ``ValueError``
        without sending anything when either value is unusable.
        """
        permit = permit_id.strip() if isinstance(permit_id, str) else ""
        if not permit:
            raise ValueError("permit_id must not be blank")
        key = idempotency_key.strip() if isinstance(idempotency_key, str) else ""
        if not key:
            raise ValueError("idempotency_key must not be blank")
        if len(key) > 128:
            raise ValueError("idempotency_key must be at most 128 characters")
        payload = {
            "session_id": session_id,
            "action": action,
            "parameters": parameters,
        }
        response = await self._client.post(
            f"{self.api_url}/v1/awi/execute",
            json=payload,
            headers={
                **self._headers(),
                "X-Permit-Id": permit,
                "Idempotency-Key": key,
            },
        )
        response.raise_for_status()
        return response.json()

    async def get_balance(self) -> float:
        """Get wallet balance. Requires ``wallet_id`` set on the client."""
        if not self.wallet_id:
            raise ValueError("wallet_id required for balance check")
        response = await self._client.get(
            f"{self.api_url}/v1/billing/wallets/{self.wallet_id}",
            headers=self._auth_headers(),
        )
        response.raise_for_status()
        return response.json().get("balance", 0.0)

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()

    async def __aenter__(self) -> B2AEdgeClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()


@dataclass(frozen=True)
class LocalDecision:
    """Verdict of one in-process permit check.

    ``reason`` uses the server's own vocabulary (``permit_expired``,
    ``permit_tool_not_allowed``, ``permit_scope_missing``,
    ``permit_budget_exceeded``, ``permit_max_calls_exceeded``, ...) so callers
    see one denial language whether the check ran locally or on the server.
    """

    allowed: bool
    reason: str | None = None


class LocalPermitValidator:
    """Client-side mirror of the trust plane's permit checks.

    Holds one permit dict (as returned by ``GET /v1/permits/{id}``) and a key
    set parsed from ``/.well-known/trust-keys.json`` via
    :func:`b2a_sdk.receipt_verifier.key_set_from_document`, and answers two
    questions with no network round trip:

    * :meth:`verify_permit` — does the permit's Ed25519 signature verify
      against the issuer's published keys? (Requires the ``cryptography``
      package: ``pip install "b2a-sdk[verify]"``.)
    * :meth:`check` — would the server's validation obviously deny this call
      right now (expiry, status, tool scope, budget, per-tool call caps)?

    Honesty about what this is: the local checks are an *optimistic mirror*
    kept for latency — they eliminate the RPC hop only for calls the cached
    permit already provably denies. The server's ``authorize_and_reserve``
    stays authoritative: it re-validates atomically under a row lock and can
    still deny a call the local mirror allowed (concurrent spend elsewhere,
    revocation since the fetch, aggregate value caps, forbidden fields).
    ``spent_credits`` in the cached dict is a snapshot; :meth:`record_use`
    layers a local reservation counter on top of it so repeated calls in one
    session do not silently overrun the cached budget.
    """

    def __init__(self, permit: dict[str, Any], key_set: dict[str, bytes]) -> None:
        self._permit = dict(permit)
        self._key_set = dict(key_set)
        # Locally reserved credits and per-tool call counts, advanced by
        # record_use(). They start at zero: the server does not expose its
        # tool_call_counts, so the local cap mirror counts this session's own
        # calls only (conservative for budget, permissive for counts — the
        # server enforces the true lifetime counter either way).
        self._reserved = Decimal("0")
        self._call_counts: dict[str, int] = {}

    @property
    def permit(self) -> dict[str, Any]:
        """A copy of the cached permit dict."""
        return dict(self._permit)

    @property
    def reserved_credits(self) -> Decimal:
        """Credits locally reserved via :meth:`record_use`."""
        return self._reserved

    @property
    def call_counts(self) -> dict[str, int]:
        """A copy of the local per-tool call counters."""
        return dict(self._call_counts)

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime:
        """Parse a permit timestamp into an aware UTC datetime.

        The trust plane persists naive-UTC datetimes and serializes them
        without an offset; naive input is therefore interpreted as UTC,
        exactly as the server's canonicalization does.
        """
        if isinstance(value, datetime):
            parsed = value
        else:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @classmethod
    def _canonical_timestamp(cls, value: Any) -> str:
        """Render a timestamp exactly as the server's canonical_json does."""
        return cls._parse_timestamp(value).isoformat()

    @staticmethod
    def _canonical_decimal(value: Any) -> str:
        """Render a credit amount exactly as the server's canonical_json does."""
        return format(Decimal(str(value)).normalize(), "f")

    @classmethod
    def permit_signing_payload(cls, permit: dict[str, Any]) -> dict[str, Any]:
        """Rebuild the exact payload the trust plane signed for this permit.

        Byte-for-byte mirror of the server's reconstruction
        (``app/services/permits.py::PermitService._verification_payload``), with
        Decimal/datetime canonicalization matching the server's
        ``canonical_json`` and the ``alg``/``kid``/``payload_hash`` fields
        folded in the way ``sign_payload`` does. Load-bearing properties:

        * ``status`` is hardcoded ``"active"`` — revocation is enforced by
          validation, not by breaking the signature.
        * ``spent_credits`` (and the server's tool-call counters) are NOT in
          the signed payload: spend is mutable server state guarded by the
          atomic budget UPDATE, so tampering with it locally is meaningless.
        * ``requires_human_approval`` and each v2 constraint
          (``max_calls_per_tool``, ``aggregate_value_cap``,
          ``forbidden_fields``, ``recipient_domain``) enter the payload only
          when set/non-empty, matching the additive-fields signing rule that
          keeps older permit signatures verifying.
        """
        payload: dict[str, Any] = {
            "permit_id": str(permit["permit_id"]),
            "issuer_wallet_id": str(permit["issuer_wallet_id"]),
            "subject_wallet_id": str(permit["subject_wallet_id"]),
            "subject_key_id": (
                str(permit["subject_key_id"]) if permit.get("subject_key_id") is not None else None
            ),
            "scopes": [str(scope) for scope in permit.get("scopes") or []],
            "allowed_tools": [str(tool) for tool in permit.get("allowed_tools") or []],
            "max_credits": cls._canonical_decimal(permit["max_credits"]),
            "expires_at": cls._canonical_timestamp(permit["expires_at"]),
            "nonce": str(permit["nonce"]),
            "status": "active",
            "issued_at": cls._canonical_timestamp(permit["issued_at"]),
            "alg": "Ed25519",
            "kid": str(permit["key_id"]),
        }
        if permit.get("requires_human_approval"):
            payload["requires_human_approval"] = True
        max_calls = permit.get("max_calls_per_tool") or {}
        if max_calls:
            payload["max_calls_per_tool"] = {
                str(tool): int(limit) for tool, limit in max_calls.items()
            }
        if permit.get("aggregate_value_cap") is not None:
            payload["aggregate_value_cap"] = cls._canonical_decimal(permit["aggregate_value_cap"])
        forbidden = permit.get("forbidden_fields") or []
        if forbidden:
            payload["forbidden_fields"] = [str(name) for name in forbidden]
        if permit.get("recipient_domain"):
            payload["recipient_domain"] = str(permit["recipient_domain"])
        if permit.get("allow_identical_repeats"):
            payload["allow_identical_repeats"] = True
        if permit.get("repeat_window_seconds") is not None:
            payload["repeat_window_seconds"] = int(permit["repeat_window_seconds"])
        # Action bindings are additive signed fields. Match ActionPermitFields
        # without importing the server or adding an SDK runtime dependency.
        action_fields = (
            "action_contract_version",
            "action_payload_hash",
            "action_schema_id",
            "action_schema_version",
            "action_public_tool_id",
            "action_upstream_binding_hash",
        )
        action = {name: permit.get(name) for name in action_fields}
        if any(value is not None for value in action.values()):
            if any(value is None for value in action.values()):
                raise ValueError("incomplete_action_binding")
            version = action["action_contract_version"]
            if type(version) is not int or version != 1:
                raise ValueError("unsupported_action_contract")
            for name in action_fields[1:]:
                value = action[name]
                if not isinstance(value, str) or not value:
                    raise ValueError("invalid_action_binding")
                if name.endswith("_hash") and re.fullmatch(r"[0-9a-f]{64}", value) is None:
                    raise ValueError("invalid_action_binding")
            payload.update(action)
        payload["payload_hash"] = hashlib.sha256(
            canonical_json(payload).encode("utf-8")
        ).hexdigest()
        return payload

    def verify_permit(self, permit: dict[str, Any] | None = None) -> bool:
        """Verify a permit's Ed25519 signature against the held key set.

        Verifies the permit passed in, or the cached one when omitted. Fails
        closed on the *evidence*, mirroring the server's ``verify_payload``:
        a malformed permit dict, an unknown/absent signing key, undecodable
        material, or a bad signature all return ``False``.

        Verifier failures are different:
        :class:`~b2a_sdk.receipt_verifier.VerificationError` propagates when
        the Ed25519 verifier itself is unavailable (``cryptography`` not
        installed — the ``verify`` extra) or its backend fails unexpectedly.
        A verifier failure is NOT a cryptographic negative and is never
        swallowed as a forged signature; callers that must not raise should
        catch it and report "cannot verify" rather than "invalid".
        ``cryptography`` is imported lazily inside the verification helper,
        so merely importing this module needs nothing.
        """
        target = self._permit if permit is None else permit
        try:
            payload = self.permit_signing_payload(target)
            signature = base64.b64decode(str(target["signature"]), validate=True)
        except (KeyError, TypeError, ValueError, ArithmeticError):
            return False
        raw_public_key = self._key_set.get(payload["kid"])
        if raw_public_key is None or len(raw_public_key) != 32:
            return False
        if len(signature) != 64:
            return False
        return verify_ed25519(
            raw_public_key,
            signature,
            canonical_json(payload).encode("utf-8"),
        )

    def check(
        self,
        tool_name: str,
        estimated_credits: Decimal,
        now: datetime | None = None,
    ) -> LocalDecision:
        """Run the in-process permit checks for one prospective call.

        Mirrors the server's validation order and reason strings: status,
        expiry, allowed tools, scopes (both ``tool:{name}:invoke`` and
        ``billing:charge``), remaining budget
        (``max_credits - spent_credits - locally reserved``), and
        ``max_calls_per_tool`` against the local counter. Purely local — no
        network, no state change (use :meth:`record_use` after a completed
        call). Malformed expiry/amount fields fail closed with the matching
        denial reason. An allow here is advisory; the server still decides.
        """
        permit = self._permit
        status = str(permit.get("status") or "active")
        if status != "active":
            return LocalDecision(False, f"permit_{status}")
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        try:
            expires_at = self._parse_timestamp(permit["expires_at"])
        except (KeyError, TypeError, ValueError):
            return LocalDecision(False, "permit_expired")
        if expires_at <= moment:
            return LocalDecision(False, "permit_expired")
        # Mirror the server exactly (app/services/permits.py): an EMPTY
        # allowlist means unrestricted — only a non-empty list that omits the
        # tool denies. The scope check below still gates every tool.
        allowed_tools = [str(tool) for tool in permit.get("allowed_tools") or []]
        if allowed_tools and tool_name not in allowed_tools:
            return LocalDecision(False, "permit_tool_not_allowed")
        scopes = {str(scope) for scope in permit.get("scopes") or []}
        if f"tool:{tool_name}:invoke" not in scopes or "billing:charge" not in scopes:
            return LocalDecision(False, "permit_scope_missing")
        try:
            max_credits = Decimal(str(permit["max_credits"]))
            spent = Decimal(str(permit.get("spent_credits") or "0"))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            return LocalDecision(False, "permit_budget_exceeded")
        remaining = max_credits - spent - self._reserved
        if estimated_credits > remaining:
            return LocalDecision(False, "permit_budget_exceeded")
        limits = permit.get("max_calls_per_tool") or {}
        if tool_name in limits:
            try:
                limit = int(limits[tool_name])
            except (TypeError, ValueError):
                # Fail closed on a malformed limit, like the server does.
                return LocalDecision(False, "permit_max_calls_exceeded")
            if self._call_counts.get(tool_name, 0) >= limit:
                return LocalDecision(False, "permit_max_calls_exceeded")
        return LocalDecision(True, None)

    def record_use(self, tool_name: str, credits: Decimal) -> None:
        """Advance the local reservation counters after a completed call.

        Adds ``credits`` to the local reservation (subtracted from remaining
        budget by :meth:`check`) and increments the tool's local call count.
        The mirror errs conservative: an idempotent replay that returns the
        same receipt still advances the local counters, under-estimating the
        remaining budget rather than over-estimating it.
        """
        self._call_counts[tool_name] = self._call_counts.get(tool_name, 0) + 1
        self._reserved += credits


class GovernedEdgeSession:
    """One permit-scoped governed session over an ``AgentMiddlewareClient``.

    :meth:`open` fetches the permit (``GET /v1/permits/{id}``) and the trust
    plane's published keys (``GET /.well-known/trust-keys.json``) ONCE,
    verifies the permit's Ed25519 signature locally, and returns a session
    whose :meth:`invoke` runs the in-process checks before each governed
    ``invoke_tool`` call — a locally-denied call raises
    :class:`~b2a_sdk.errors.PermitDeniedError` without touching the server
    (that is the RPC hop this session eliminates).

    The session borrows the client; the caller owns the client's lifecycle.
    Local checks are optimistic (see :class:`LocalPermitValidator`); every
    call that does go out is fully governed — permit, caller-owned
    idempotency key, signed receipt.
    """

    def __init__(
        self,
        client: AgentMiddlewareClient,
        validator: LocalPermitValidator,
        *,
        permit_id: str,
        wallet_id: str,
    ) -> None:
        self._client = client
        self.validator = validator
        self.permit_id = permit_id
        self.wallet_id = wallet_id

    @classmethod
    async def open(
        cls,
        client: AgentMiddlewareClient,
        *,
        permit_id: str,
        wallet_id: str,
        trust_keys_document: dict[str, Any] | None = None,
    ) -> GovernedEdgeSession:
        """Fetch permit + trust keys once, verify locally, return a session.

        ``trust_keys_document`` short-circuits the key fetch for callers that
        already hold (or pin) the issuer's ``trust-keys.json``. Raises
        :class:`~b2a_sdk.errors.PermitDeniedError` with reason:

        * ``permit_signature_invalid`` — the fetched permit does not verify
          against the published keys (a genuine cryptographic negative);
        * ``permit_trust_keys_invalid`` — the trust-keys document is not a
          usable key set (malformed document, no ``keys`` list);
        * ``permit_verification_unavailable`` — the Ed25519 verifier itself
          is unavailable or failed (``cryptography`` missing, backend
          error). Deliberately distinct from ``permit_signature_invalid``: a
          verifier failure is not evidence of forgery.

        The latter two wrap the underlying
        :class:`~b2a_sdk.receipt_verifier.VerificationError` as ``__cause__``
        so callers see the documented boundary error type without losing the
        diagnostic.
        """
        permit = await client._request_json("GET", f"/v1/permits/{permit_id}")
        document = trust_keys_document
        if document is None:
            document = await client._request_json("GET", "/.well-known/trust-keys.json")
        try:
            key_set = key_set_from_document(document)
        except VerificationError as exc:
            raise PermitDeniedError("permit_trust_keys_invalid") from exc
        validator = LocalPermitValidator(permit, key_set)
        try:
            verified = validator.verify_permit()
        except VerificationError as exc:
            raise PermitDeniedError("permit_verification_unavailable") from exc
        if not verified:
            raise PermitDeniedError("permit_signature_invalid", payload=permit)
        return cls(client, validator, permit_id=permit_id, wallet_id=wallet_id)

    async def invoke(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: str,
        estimated_credits: Decimal | None = None,
    ) -> InvocationResult:
        """Locally check, then invoke through the governed loop.

        ``estimated_credits`` feeds the local budget check (default 0 checks
        expiry/status/tools/scopes/call caps only — pass the tool's advertised
        credits to also pre-screen the budget). On local denial this raises
        ``PermitDeniedError(reason)`` immediately, with no server round trip;
        otherwise it calls ``client.invoke_tool`` and, on success, records the
        receipt's ``credits_charged`` in the local reservation counters.
        """
        decision = self.validator.check(
            name,
            estimated_credits if estimated_credits is not None else Decimal("0"),
        )
        if not decision.allowed:
            raise PermitDeniedError(decision.reason or "permit_denied")
        result = await self._client.invoke_tool(
            name,
            arguments,
            wallet_id=self.wallet_id,
            permit_id=self.permit_id,
            idempotency_key=idempotency_key,
        )
        self.validator.record_use(name, result.receipt.credits_charged)
        return result
