"""x402 facilitation helpers for the middleware settle endpoint.

Client-side only: detect an HTTP 402 payment demand on a response, extract
its ``X-402-*`` headers, and ask the trust plane to authorize and settle it
against a permit. The dict ``settle_402`` returns carries the
transfer-authorization payload the *payer wallet* must sign — this SDK holds
no on-chain keys and signs nothing itself; the middleware's role is
facilitation (permit budget authorization, shadow-ledger metering, signed
receipt), never on-chain settlement.
"""

from __future__ import annotations

from typing import Any

import httpx

# Same lazy tier as this module: both x402 and client require httpx, and the
# package only reaches either through the PEP 562 lazy attributes in
# __init__.py, so importing .client here keeps the offline (no-httpx)
# verification path dependency-free.
from .client import AgentMiddlewareClient
from .errors import (
    APIError,
    AuthenticationError,
    AuthorizationError,
    IdempotencyConflictError,
    PermitDeniedError,
    TransportError,
)

_AMOUNT_HEADER = "x-402-amount"
_PAY_TO_HEADER = "x-402-payto"
_NETWORK_HEADER = "x-402-network"

# Reuse the core client's private helpers instead of duplicating them here;
# client.py is the single source of truth for both behaviors.
_error_detail = AgentMiddlewareClient._error_detail
_validate_idempotency_key = AgentMiddlewareClient._validate_idempotency_key


def parse_402_response(response: Any) -> dict[str, Any] | None:
    """Extract the x402 payment requirement from a 402 response, or ``None``.

    Duck-types ``httpx.Response``: anything with a ``status_code`` attribute
    and a ``headers`` mapping works. Returns ``None`` for a non-402 status
    and for a 402 that does not carry the full ``X-402-*`` header set (not an
    x402 demand). Header values are returned as received — the middleware's
    ``/v1/x402`` endpoints are the strict validator.

    Returns a dict with ``amount_usd``, ``pay_to``, ``network``, and ``asset``
    (when present), matching the server's parse endpoint. The legacy ``amount``
    key is returned alongside ``amount_usd`` with the same value, so existing
    callers keep working; ``amount_usd`` is the name to read going forward.
    """
    if getattr(response, "status_code", None) != 402:
        return None
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    lowered = {str(key).lower(): str(value) for key, value in dict(headers).items()}
    amount = lowered.get(_AMOUNT_HEADER)
    pay_to = lowered.get(_PAY_TO_HEADER)
    network = lowered.get(_NETWORK_HEADER)
    if amount is None or pay_to is None or network is None:
        return None
    result: dict[str, Any] = {
        # Legacy alias kept for compatibility; same value as ``amount_usd``.
        "amount": amount,
        "amount_usd": amount,
        "pay_to": pay_to,
        "network": network,
    }
    asset = lowered.get("x-402-asset")
    if asset is not None:
        result["asset"] = asset
    return result


class X402Client:
    """Minimal async client for the ``/v1/x402`` facilitation surface."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.thisisatest.tech",
        timeout: float = 10.0,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # Held only in the transport's default headers (see ``api_key``), not
        # duplicated as instance state, so ``vars(client)`` dumps omit it.
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "X-API-Key": api_key,
                "Content-Type": "application/json",
                # Source of truth for the User-Agent string is
                # AgentMiddlewareClient.__init__ in client.py (it exposes no
                # importable constant); keep the two in lockstep.
                "User-Agent": "b2a-sdk/0.5.0",
            },
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
        )

    @property
    def api_key(self) -> str:
        """The API key this client sends as ``X-API-Key`` (read-only)."""
        return self._client.headers["X-API-Key"]

    async def parse_402(
        self,
        status_code: int,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        """POST the observed response to /v1/x402/parse and return the requirement.

        The server strictly validates the headers and returns a requirement
        dict with ``amount_usd``, ``pay_to``, ``network``, and an optional
        ``asset``. Raises APIError on invalid/missing headers, and also when a
        2xx body omits the three required fields or carries them as anything
        other than non-empty strings — a malformed requirement must not reach
        settlement. ``asset`` stays optional here, matching
        :func:`parse_402_response` and :meth:`settle_402`; when present it is
        still required to be a non-empty string.
        """
        try:
            response = await self._client.post(
                "/v1/x402/parse",
                json={"status_code": status_code, "headers": headers},
            )
        except httpx.HTTPError as exc:
            raise TransportError(f"POST /v1/x402/parse failed: {exc}") from exc
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
            detail = _error_detail(payload, f"HTTP {response.status_code}")
            if response.status_code == 401:
                raise AuthenticationError(detail, status_code=401, payload=payload)
            if response.status_code == 403:
                raise AuthorizationError(detail, status_code=403, payload=payload)
            raise APIError(detail, status_code=response.status_code, payload=payload)
        # A 2xx body still has to honour the documented contract. Without this,
        # a malformed body would reach settle_402() as an empty amount or
        # pay_to, or raise KeyError in a caller reading the documented fields.
        # Only these three are required: parse_402_response() and settle_402()
        # both treat `asset` as optional, so requiring it here would reject an
        # asset-less requirement the rest of the SDK handles fine.
        for required in ("amount_usd", "pay_to", "network"):
            value = payload.get(required)
            if not isinstance(value, str) or not value:
                raise APIError(
                    f"invalid_x402_parse_response: {required!r} must be a non-empty string",
                    status_code=response.status_code,
                    payload=payload,
                )
        asset = payload.get("asset")
        if asset is not None and (not isinstance(asset, str) or not asset):
            raise APIError(
                "invalid_x402_parse_response: 'asset' must be a non-empty string when present",
                status_code=response.status_code,
                payload=payload,
            )
        return payload

    async def settle_402(
        self,
        *,
        permit_id: str,
        wallet_id: str,
        requirement: dict[str, Any],
        idempotency_key: str,
        payer: str | None = None,
    ) -> dict[str, Any]:
        """POST the requirement to ``/v1/x402/settle`` and return the settlement.

        ``requirement`` is the dict from :func:`parse_402_response` (keys
        ``amount_usd``/``pay_to``/``network``, optional ``asset``; the legacy
        ``amount`` alias is accepted only when ``amount_usd`` is absent);
        ``payer`` is
        the payer wallet's on-chain address — required by the server for EVM
        networks so the attested EIP-712 message binds the real ``from``. The
        server response includes the transfer authorization for the payer
        wallet to sign plus the settlement receipt id.
        """
        key = _validate_idempotency_key(idempotency_key)
        # ``amount_usd`` is the canonical server field and wins; ``amount`` is
        # only the legacy alias. parse_402_response() sets both to the same
        # value, but a hand-built or mutated requirement can carry both with
        # different values, and this is the amount that gets settled.
        amount = requirement.get("amount_usd")
        if amount is None:
            amount = requirement.get("amount")
        body: dict[str, Any] = {
            "permit_id": permit_id,
            "wallet_id": wallet_id,
            "amount": str(amount) if amount is not None else "",
            "pay_to": requirement.get("pay_to", ""),
            "network": requirement.get("network", ""),
        }
        asset = requirement.get("asset")
        if asset:
            body["asset"] = asset
        if payer is not None:
            body["payer"] = payer
        try:
            response = await self._client.post(
                "/v1/x402/settle",
                json=body,
                headers={"Idempotency-Key": key},
            )
        except httpx.HTTPError as exc:
            raise TransportError(f"POST /v1/x402/settle failed: {exc}") from exc
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
            detail = _error_detail(payload, f"HTTP {response.status_code}")
            if response.status_code == 401:
                raise AuthenticationError(detail, status_code=401, payload=payload)
            # Deliberate divergence from client.py's _raise_http_error, which
            # maps permit_* details to PermitDeniedError only on 403: the x402
            # settle endpoint returns permit denials as 400 (denied reason) or
            # 404 (permit_not_found), so any permit_* detail is surfaced as
            # the typed permit error regardless of status.
            if detail.startswith("permit_"):
                raise PermitDeniedError(detail, payload=payload)
            if response.status_code == 403:
                raise AuthorizationError(detail, status_code=403, payload=payload)
            if response.status_code == 409:
                raise IdempotencyConflictError(detail, status_code=409, payload=payload)
            raise APIError(detail, status_code=response.status_code, payload=payload)
        return payload

    async def handle_402(
        self,
        response: Any,
        *,
        permit_id: str,
        wallet_id: str,
        idempotency_key: str,
        payer: str | None = None,
    ) -> dict[str, Any] | None:
        """Parse a 402 response and settle it in one step.

        Returns ``None`` when the response is not an x402 payment demand;
        otherwise the settlement dict, whose ``authorization`` the payer
        wallet signs before the caller retries the original request.
        """
        requirement = parse_402_response(response)
        if requirement is None:
            return None
        return await self.settle_402(
            permit_id=permit_id,
            wallet_id=wallet_id,
            requirement=requirement,
            idempotency_key=idempotency_key,
            payer=payer,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> X402Client:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()


__all__ = ["X402Client", "parse_402_response"]
