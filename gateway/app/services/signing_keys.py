from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.config import Settings, get_settings
from app.core.time import utc_now
from app.db.database import get_session_factory
from app.db.models import SigningKeyModel


class SigningKeyError(RuntimeError):
    """Raised when trust-plane signing or verification cannot proceed."""


# Names the exact byte-level contract :func:`canonical_json` implements, so an
# independent verifier can state which rules it was written against. Bump only
# when the bytes a signature covers change shape — never for additive fields,
# which are already handled by signing them only when present.
RECEIPT_CANONICALIZATION = "awi-canonical-json/1"


def _decode_private_key(configured: str) -> Ed25519PrivateKey:
    """Decode strict base64 Ed25519 seed material without exposing it."""

    try:
        raw = base64.b64decode(configured, validate=True)
        return Ed25519PrivateKey.from_private_bytes(raw)
    except (binascii.Error, ValueError) as exc:
        raise SigningKeyError("invalid_trust_signing_private_key") from exc


def validate_signing_key_configuration(settings: Settings | None = None) -> str:
    """Validate configured signing material without touching durable state.

    Returns a non-secret state for startup logs and dependency health. The
    configured value must be strict base64 that decodes to the 32-byte seed
    accepted by ``Ed25519PrivateKey.from_private_bytes``.
    """

    settings = settings or get_settings()
    configured = settings.TRUST_SIGNING_PRIVATE_KEY_B64.strip()
    if configured:
        _decode_private_key(configured)
        return "loaded"
    if settings.TRUST_MODE_ENABLED:
        raise SigningKeyError("trust_signing_private_key_required")
    return "ephemeral"


def canonical_json(
    payload: dict[str, Any],
    *,
    ensure_ascii: bool = True,
    allow_nan: bool = True,
) -> str:
    """Serialize a payload into stable JSON before hashing or signing."""

    def normalize(value: Any) -> Any:
        if isinstance(value, Decimal):
            normalized = value.normalize()
            if normalized == normalized.to_integral():
                return format(normalized, "f")
            return format(normalized, "f")
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc).isoformat()
        if isinstance(value, dict):
            return {str(k): normalize(v) for k, v in sorted(value.items())}
        if isinstance(value, list):
            return [normalize(v) for v in value]
        return value

    return json.dumps(
        normalize(payload),
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=ensure_ascii,
        allow_nan=allow_nan,
    )


def sha256_hex(payload: dict[str, Any] | str | bytes) -> str:
    if isinstance(payload, dict):
        data = canonical_json(payload).encode()
    elif isinstance(payload, str):
        data = payload.encode()
    else:
        data = payload
    return hashlib.sha256(data).hexdigest()


class SigningKeyService:
    """Ed25519 signing helper with DB-backed public key metadata."""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._private_key: Ed25519PrivateKey | None = None
        self._key_id = self._settings.TRUST_SIGNING_KEY_ID

    def _load_private_key(self) -> Ed25519PrivateKey:
        if self._private_key:
            return self._private_key

        configured = self._settings.TRUST_SIGNING_PRIVATE_KEY_B64.strip()
        if configured:
            self._private_key = _decode_private_key(configured)
            return self._private_key

        if self._settings.TRUST_MODE_ENABLED:
            raise SigningKeyError("trust_signing_private_key_required")

        # Development/test fallback is deliberately process-ephemeral. It lets
        # local tests verify signatures without silently persisting a private key.
        self._private_key = Ed25519PrivateKey.generate()
        # Content-address the ephemeral key's id so a restart (or a second
        # instance) publishes its distinct public key under its own key_id
        # instead of overwriting the durable metadata a previous ephemeral key
        # signed under. Old signatures keep verifying against their own stored
        # public key rather than being silently invalidated.
        raw_public = self._private_key.public_key().public_bytes(
            Encoding.Raw, PublicFormat.Raw
        )
        fingerprint = hashlib.sha256(raw_public).hexdigest()[:16]
        self._key_id = f"{self._settings.TRUST_SIGNING_KEY_ID}-ephemeral-{fingerprint}"
        return self._private_key

    def _public_key_b64(self) -> str:
        public_key = self._load_private_key().public_key()
        raw = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
        return base64.b64encode(raw).decode()

    @staticmethod
    def _assert_public_key_mapping(
        key: SigningKeyModel,
        public_key_b64: str,
    ) -> None:
        """Reject attempts to bind existing key metadata to new key material."""

        if key.public_key_b64 != public_key_b64:
            raise SigningKeyError("signing_key_id_public_key_mismatch")

    @staticmethod
    def _assert_key_not_disabled(key: SigningKeyModel) -> None:
        """Keep explicit key revocation terminal for future signing."""

        if key.status == "disabled":
            raise SigningKeyError("signing_key_disabled")

    async def ensure_active_key(self) -> SigningKeyModel:
        factory = get_session_factory()
        public_key_b64 = self._public_key_b64()
        async with factory() as session:
            result = await session.execute(
                select(SigningKeyModel).where(
                    cast(ColumnElement[bool], SigningKeyModel.key_id == self._key_id)
                )
            )
            key = result.scalar_one_or_none()
            # Read-mostly fast path: when the active key already matches, do no
            # write. This keeps the common (per-signature) call free of write
            # contention and the first-time insert race below.
            if key:
                self._assert_public_key_mapping(key, public_key_b64)
                self._assert_key_not_disabled(key)
                if key.status == "active" and key.retired_at is None:
                    return key
                key.status = "active"
                key.activated_at = utc_now()
                key.retired_at = None
                session.add(key)
            else:
                key = SigningKeyModel(
                    key_id=self._key_id,
                    public_key_b64=public_key_b64,
                    status="active",
                    activated_at=utc_now(),
                )
                session.add(key)
            try:
                await session.commit()
            except IntegrityError:
                # A concurrent writer created the row first; adopt theirs.
                await session.rollback()
                key = (
                    await session.execute(
                        select(SigningKeyModel).where(
                            cast(
                                ColumnElement[bool],
                                SigningKeyModel.key_id == self._key_id,
                            )
                        )
                    )
                ).scalar_one()
                self._assert_public_key_mapping(key, public_key_b64)
                self._assert_key_not_disabled(key)
            return key

    async def get_active_key(self) -> SigningKeyModel:
        """Return the current active public metadata, creating it if needed."""

        return await self.ensure_active_key()

    async def list_public_keys(self) -> list[SigningKeyModel]:
        """Return every published public key, newest activation first.

        Retired keys stay in the list on purpose: a receipt signed under a
        retired key must remain verifiable for as long as the receipt is
        evidence. Only ``disabled`` keys are excluded, mirroring
        :meth:`verify_payload`, which refuses them.
        """

        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(SigningKeyModel).where(
                    cast(ColumnElement[bool], SigningKeyModel.status != "disabled")
                )
            )
            keys = list(result.scalars())
        # Sort in Python so the ordering is identical on SQLite and PostgreSQL
        # regardless of how each backend collates NULL activated_at.
        keys.sort(
            key=lambda key: key.activated_at or key.created_at,
            reverse=True,
        )
        return keys

    async def get_public_key(
        self,
        key_id: str,
        *,
        session: AsyncSession | None = None,
    ) -> SigningKeyModel | None:
        # Refresh caller-owned identity-map rows so disabling a key cannot
        # retain a cached active verdict inside a larger transaction.
        query = (
            select(SigningKeyModel)
            .where(cast(ColumnElement[bool], SigningKeyModel.key_id == key_id))
            .execution_options(populate_existing=True)
        )
        if session is not None:
            return (await session.execute(query)).scalar_one_or_none()
        factory = get_session_factory()
        async with factory() as owned_session:
            result = await owned_session.execute(query)
            return result.scalar_one_or_none()

    async def retire_key_metadata(self, key_id: str) -> SigningKeyModel:
        """Retire public-key metadata without disabling historical verification."""

        factory = get_session_factory()
        async with factory() as session:
            key = await session.get(SigningKeyModel, key_id)
            if not key:
                raise SigningKeyError("signing_key_not_found")
            if key.status != "disabled":
                key.status = "retired"
                key.retired_at = utc_now()
            session.add(key)
            await session.commit()
            await session.refresh(key)
            return key

    async def rotate_active_key_metadata(self, new_key_id: str) -> SigningKeyModel:
        """
        Move future signatures to a new key id while preserving old public metadata.

        This intentionally rotates metadata only. Private-key rotation through
        environment configuration must pair new key material with a new key id;
        reusing an existing id for different material is rejected so historical
        signatures remain verifiable.
        """

        if not new_key_id.strip():
            raise SigningKeyError("signing_key_id_required")

        old_key = await self.ensure_active_key()
        new_key_id = new_key_id.strip()
        now = utc_now()
        public_key_b64 = self._public_key_b64()
        factory = get_session_factory()
        async with factory() as session:
            key = await session.get(SigningKeyModel, new_key_id)
            if key:
                self._assert_public_key_mapping(key, public_key_b64)
                self._assert_key_not_disabled(key)

            if old_key.key_id != new_key_id:
                old = await session.get(SigningKeyModel, old_key.key_id)
                if old and old.status != "disabled":
                    old.status = "retired"
                    old.retired_at = now
                    session.add(old)

            if key:
                key.status = "active"
                key.activated_at = now
                key.retired_at = None
            else:
                key = SigningKeyModel(
                    key_id=new_key_id,
                    public_key_b64=public_key_b64,
                    status="active",
                    activated_at=now,
                )
            session.add(key)
            await session.commit()
            await session.refresh(key)

        self._key_id = new_key_id
        return key

    async def sign_payload(self, payload: dict[str, Any]) -> tuple[str, str, str]:
        key = await self.ensure_active_key()
        return self.sign_payload_with_key_id(payload, key.key_id)

    async def validate_prepared_signing_key(
        self,
        key_id: str,
        *,
        session: AsyncSession,
    ) -> SigningKeyModel:
        """Lock and revalidate a pre-provisioned key before caller-owned signing."""
        key = await session.get(
            SigningKeyModel,
            key_id,
            with_for_update=True,
            populate_existing=True,
        )
        if key is None:
            raise SigningKeyError("signing_key_not_found")
        self._assert_public_key_mapping(key, self._public_key_b64())
        self._assert_key_not_disabled(key)
        if key.status != "active" or key.retired_at is not None:
            raise SigningKeyError("signing_key_not_active")
        return key

    def sign_payload_with_key_id(
        self,
        payload: dict[str, Any],
        key_id: str,
    ) -> tuple[str, str, str]:
        """Sign with an already-provisioned key id, without touching the DB.

        Use when the active key has been ensured beforehand and the caller is
        holding an open transaction (so a nested ``ensure_active_key`` write
        would otherwise contend for the connection / lock).
        """
        signing_payload = dict(payload)
        signing_payload.setdefault("alg", "Ed25519")
        signing_payload.setdefault("kid", key_id)
        payload_hash = sha256_hex(signing_payload)
        signing_payload.setdefault("payload_hash", payload_hash)
        signature = self._load_private_key().sign(
            canonical_json(signing_payload).encode()
        )
        return base64.b64encode(signature).decode(), key_id, payload_hash

    async def verify_payload(
        self,
        payload: dict[str, Any],
        *,
        signature: str,
        key_id: str,
        session: AsyncSession | None = None,
    ) -> bool:
        key = await self.get_public_key(key_id, session=session)
        if not key or key.status == "disabled":
            return False
        # Verification must fail closed on malformed stored data, not raise. The
        # base64 decodes and key construction were outside the try, so a bad
        # signature/public-key with wrong padding or length raised
        # binascii.Error / ValueError and surfaced as HTTP 500 from
        # /v1/receipts/verify and /v1/audit/verify-chain instead of a clean
        # "invalid" verdict — a single corrupt row could mask tampering behind a
        # 500. Treat any decode/key/verify failure as "not verified".
        try:
            public_key = Ed25519PublicKey.from_public_bytes(
                base64.b64decode(key.public_key_b64, validate=True)
            )
            public_key.verify(
                base64.b64decode(signature, validate=True),
                canonical_json(payload).encode(),
            )
            return True
        except (binascii.Error, InvalidSignature, ValueError, TypeError):
            return False


_signing_key_service: SigningKeyService | None = None


def get_signing_key_service() -> SigningKeyService:
    global _signing_key_service
    if _signing_key_service is None:
        _signing_key_service = SigningKeyService()
    return _signing_key_service
