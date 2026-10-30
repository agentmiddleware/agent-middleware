"""
API Key Rotation Service for wallet security.

Handles API key creation, rotation, and revocation for compromised wallets.
Supports automatic rotation on suspicious activity detection.

Architecture:
1. Keys are stored as salted scrypt hashes keyed by a server pepper
   (API_KEY_HASH_PEPPER) and found by a non-secret indexed lookup id; there
   is no unkeyed fallback (see docs/api-key-hashing.md)
2. Only the key prefix and masked key are ever shown
3. Rotation creates new key and optionally revokes old ones
4. Emergency revocation immediately invalidates all keys
"""

import asyncio
import base64
import hashlib
import hmac
import json
import threading
import time
from collections import OrderedDict
import os
import secrets
import logging
from datetime import datetime, timedelta
from typing import Any, Optional, cast
from uuid import uuid4

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from ..core.config import get_settings
from ..core.time import to_naive_utc, utc_now
from ..core.trust_mode import is_production_like_environment
from ..db.database import get_session_factory
from ..db.models import (
    APIKeyModel,
    KeyRotationLogModel,
    RefreshTokenModel,
    WalletModel,
)
from ..schemas.billing import (
    APIKeyStatus,
    RotationType,
)

logger = logging.getLogger(__name__)

API_KEY_LENGTH = 32
API_KEY_PREFIX_LENGTH = 8
# Non-secret lookup id: "b2a_" plus 12 random characters (72 bits) of the key.
# The remaining ~31 characters (~184 bits) stay secret. Stored and indexed so
# a slow, salted hash can be verified against a single row.
API_KEY_LOOKUP_LENGTH = 16


def _demo_attribution_field(
    attribution: dict[str, str] | None, field: str
) -> str | None:
    if not attribution:
        return None
    value = attribution.get(field)
    return value if isinstance(value, str) and value else None


class APIKeyError(Exception):
    """Base exception for API key operations."""

    pass


class KeyNotFoundError(APIKeyError):
    """Raised when an API key is not found."""

    def __init__(self, key_id: str):
        self.key_id = key_id
        super().__init__(f"API key not found: {key_id}")


class WalletNotFoundError(APIKeyError):
    """Raised when a wallet is not found."""

    def __init__(self, wallet_id: str):
        self.wallet_id = wallet_id
        super().__init__(f"Wallet not found: {wallet_id}")


class KeyExpiredError(APIKeyError):
    """Raised when an API key has expired."""

    pass


class KeyRevokedError(APIKeyError):
    """Raised when an API key has been revoked."""

    pass


class InvalidRotationRequestError(APIKeyError):
    """Raised when rotation options cannot identify a key to revoke."""

    pass


API_KEY_HASH_PEPPER_ENV = "API_KEY_HASH_PEPPER"


class APIKeyHashingUnavailable(RuntimeError):
    """Raised when a production-like environment has no API-key pepper."""


# Fixed, PUBLIC pepper used only when ENVIRONMENT is local-compatible and no
# pepper is configured, so local dev and tests need no secret. It is never
# used in production-like environments, which require API_KEY_HASH_PEPPER.
DEV_API_KEY_HASH_PEPPER = b"amw-local-dev-api-key-pepper-not-a-secret"


def _configured_pepper() -> bytes | None:
    """The server pepper: process env first, then Settings (``.env``).

    An explicitly empty environment value masks dotenv and cached Settings;
    production then fails closed instead of reviving a stale pepper.
    Settings holds it as a SecretStr so it is masked in any settings dump.
    It is never logged.
    """
    value = (
        os.environ[API_KEY_HASH_PEPPER_ENV]
        if API_KEY_HASH_PEPPER_ENV in os.environ
        else get_settings().API_KEY_HASH_PEPPER.get_secret_value()
    ).strip()
    return value.encode("utf-8") if value else None


def _dev_pepper_allowed() -> bool:
    return not is_production_like_environment(get_settings().ENVIRONMENT)


def _api_key_hash_pepper() -> bytes | None:
    """Pepper to use, or None when a production-like env has none (fail closed)."""
    pepper = _configured_pepper()
    if pepper is not None:
        return pepper
    if _dev_pepper_allowed():
        return DEV_API_KEY_HASH_PEPPER
    return None


def api_key_hashing_configured() -> bool:
    return _api_key_hash_pepper() is not None


def validate_api_key_hashing_config() -> None:
    """Startup guard: production-like environments refuse to boot unpeppered."""
    if not api_key_hashing_configured():
        raise APIKeyHashingUnavailable(
            f"{API_KEY_HASH_PEPPER_ENV} must be set in production-like "
            "environments. See docs/api-key-hashing.md."
        )


# scrypt cost: N=2**14, r=8, p=1 (16 MiB, the RFC 7914 interactive setting).
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_MAXMEM = 64 * 1024 * 1024
_SALT_BYTES = 16
_HASH_SCHEME = "scrypt"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _scrypt(
    pepper: bytes, salt: bytes, presented_key: str, n: int, r: int, p: int
) -> bytes:
    # The pepper is part of the salt, so a database dump alone cannot be
    # attacked offline; the per-row random salt makes every hash unique.
    return hashlib.scrypt(
        presented_key.encode("utf-8"),
        salt=pepper + salt,
        n=n,
        r=r,
        p=p,
        maxmem=_SCRYPT_MAXMEM,
        dklen=_SCRYPT_DKLEN,
    )


def _encode_hash(salt: bytes, derived: bytes) -> str:
    return f"{_HASH_SCHEME}${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(derived)}"


def _verify_hash(pepper: bytes, stored: str, presented_key: str) -> bool:
    """Constant-time check of ``presented_key`` against a stored scrypt hash."""
    try:
        scheme, n, r, p, salt_text, derived_text = stored.split("$")
        if scheme != _HASH_SCHEME:
            return False
        salt, expected = _unb64(salt_text), _unb64(derived_text)
        derived = _scrypt(pepper, salt, presented_key, int(n), int(r), int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived, expected)


def api_key_lookup_id(full_key: str) -> str:
    """Non-secret, indexed lookup id for ``full_key`` (its first 16 chars)."""
    return full_key[:API_KEY_LOOKUP_LENGTH]


def hash_api_key(full_key: str) -> str:
    """Salted, peppered scrypt hash of ``full_key`` in PHC-like text form.

    Production-like environments without a pepper fail closed.
    """
    pepper = _api_key_hash_pepper()
    if pepper is None:
        raise APIKeyHashingUnavailable(
            f"{API_KEY_HASH_PEPPER_ENV} is not set; refusing to hash API keys."
        )
    salt = secrets.token_bytes(_SALT_BYTES)
    return _encode_hash(
        salt, _scrypt(pepper, salt, full_key, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
    )


class _VerifiedKeyCache:
    """Bounded TTL cache of recent successful scrypt verifications.

    Entries are keyed by the stored row hash (already non-reversible) and hold
    a cheap verifier of the presented key: scrypt at minimal cost salted with
    a per-process random secret that is never persisted or logged. A hit
    still requires the presented key to match that verifier and the same
    pepper to be in use, and the row is still read from the database on every
    request, so revocation, expiry and use limits apply immediately. Only the
    expensive KDF is skipped.
    """

    def __init__(self, max_entries: int = 4096, ttl_seconds: float = 60.0) -> None:
        self._max = max_entries
        self._ttl = ttl_seconds
        self._secret = secrets.token_bytes(32)
        self._entries: OrderedDict[str, tuple[float, bytes, bytes]] = OrderedDict()
        self._lock = threading.Lock()

    def _fast(self, presented_key: str) -> bytes:
        return hashlib.scrypt(
            presented_key.encode("utf-8"), salt=self._secret, n=2, r=1, p=1, dklen=32
        )

    def hit(self, stored_hash: str, pepper: bytes, presented_key: str) -> bool:
        with self._lock:
            entry = self._entries.get(stored_hash)
            if entry is None:
                return False
            expires, cached_pepper, verifier = entry
            if expires < time.monotonic():
                del self._entries[stored_hash]
                return False
        return hmac.compare_digest(cached_pepper, pepper) and hmac.compare_digest(
            verifier, self._fast(presented_key)
        )

    def remember(self, stored_hash: str, pepper: bytes, presented_key: str) -> None:
        verifier = self._fast(presented_key)
        with self._lock:
            self._entries[stored_hash] = (
                time.monotonic() + self._ttl,
                pepper,
                verifier,
            )
            self._entries.move_to_end(stored_hash)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_verified_keys = _VerifiedKeyCache()


def clear_verified_key_cache() -> None:
    _verified_keys.clear()


async def _verify_presented_key(pepper: bytes, stored: str, presented_key: str) -> bool:
    if _verified_keys.hit(stored, pepper, presented_key):
        return True
    # hashlib.scrypt releases the GIL; keep the event loop free while it runs.
    ok = await asyncio.to_thread(_verify_hash, pepper, stored, presented_key)
    if ok:
        _verified_keys.remember(stored, pepper, presented_key)
    return ok


def generate_api_key() -> tuple[str, str, str]:
    """
    Generate a new API key.

    Returns:
        tuple: (full_key, key_hash, key_prefix)
    """
    full_key = f"b2a_{secrets.token_urlsafe(API_KEY_LENGTH)}"
    key_hash = hash_api_key(full_key)
    key_prefix = full_key[:API_KEY_PREFIX_LENGTH]
    return full_key, key_hash, key_prefix


def mask_key(key: str) -> str:
    """Mask an API key for safe display."""
    if len(key) <= 12:
        return "*" * len(key)
    return f"{key[:6]}...{key[-4:]}"


def _key_is_live(key: APIKeyModel, now: datetime) -> bool:
    """True if ``key`` could authenticate at ``now``.

    The single liveness rule: ACTIVE status, use budget not spent, and not
    past ``expires_at``. Status alone is not enough — nothing sweeps expired
    or exhausted keys to a non-active status.
    """
    if key.status != APIKeyStatus.ACTIVE.value:
        return False
    if key.max_uses is not None and key.use_count >= key.max_uses:
        return False
    return not key.expires_at or key.expires_at >= now


class APIKeyService:
    """
    Manages API keys for wallet authentication and key rotation.

    Features:
    - Create new API keys with optional expiration
    - Rotate keys manually or automatically
    - Emergency revocation for compromised wallets
    - Usage tracking and audit logging
    """

    def __init__(self):
        self._session_factory = get_session_factory

    async def create_key(
        self,
        wallet_id: str,
        key_name: str = "default",
        expires_in_days: int | None = None,
        max_uses: int | None = None,
        session: AsyncSession | None = None,
        allowed_tools: list[str] | None = None,
        tenant: str | None = None,
        attribution: dict[str, str] | None = None,
    ) -> dict:
        """
        Create a new API key for a wallet.

        Args:
            wallet_id: Wallet to create key for
            key_name: Human-readable name for the key
            expires_in_days: Optional expiration in days
            max_uses: Optional cap on successful authentications (None = unlimited)
            session: When given, must already be inside a transaction the
                CALLER owns end to end (an open ``async with
                session.begin():`` block) — this method only adds/flushes
                on it, never begins or commits. That lets a caller compose
                key issuance with other wallet operations into one atomic
                unit of work (see app/services/pods.py). Omit it (the
                default) for the original standalone behavior: opens,
                commits, and closes its own session for the lookup and for
                the insert.

        Returns:
            {
                "key_id": str,
                "wallet_id": str,
                "api_key": str,
                "key_prefix": str,
                "status": str,
                "key_name": str,
                "created_at": datetime,
                "expires_at": datetime | None,
                "max_uses": int | None,
            }
        """
        if session is not None:
            result = await session.execute(
                select(WalletModel).where(col(WalletModel.wallet_id) == wallet_id)
            )
            wallet = result.scalar_one_or_none()
            if wallet is None:
                raise WalletNotFoundError(wallet_id)
            if wallet.tenant:
                tenant = wallet.tenant
        else:
            async with self._session_factory()() as check_session:
                result = await check_session.execute(
                    select(WalletModel).where(col(WalletModel.wallet_id) == wallet_id)
                )
                wallet = result.scalar_one_or_none()
                if wallet is None:
                    raise WalletNotFoundError(wallet_id)
                if wallet.tenant:
                    tenant = wallet.tenant

        full_key, key_hash, key_prefix = await asyncio.to_thread(generate_api_key)
        key_id = f"key_{uuid4().hex[:12]}"
        now = utc_now()
        expires_at = None

        if expires_in_days:
            expires_at = now + timedelta(days=expires_in_days)

        api_key = APIKeyModel(
            key_id=key_id,
            wallet_id=wallet_id,
            key_hash=key_hash,
            key_lookup=api_key_lookup_id(full_key),
            key_prefix=key_prefix,
            status=APIKeyStatus.ACTIVE.value,
            metadata_json=json.dumps({"name": key_name}),
            expires_at=(to_naive_utc(expires_at) if expires_at else None),
            max_uses=max_uses,
            allowed_tools_json=(
                json.dumps(list(allowed_tools)) if allowed_tools is not None else None
            ),
            tenant=tenant,
            utm_source=_demo_attribution_field(attribution, "utm_source"),
            utm_medium=_demo_attribution_field(attribution, "utm_medium"),
            utm_campaign=_demo_attribution_field(attribution, "utm_campaign"),
            utm_content=_demo_attribution_field(attribution, "utm_content"),
            utm_term=_demo_attribution_field(attribution, "utm_term"),
            referrer=_demo_attribution_field(attribution, "referrer"),
        )

        if session is not None:
            session.add(api_key)
            await session.flush()
        else:
            async with self._session_factory()() as write_session:
                write_session.add(api_key)
                await write_session.commit()

        logger.info(f"Created API key {key_id} for wallet {wallet_id}")

        return {
            "key_id": key_id,
            "wallet_id": wallet_id,
            "api_key": full_key,
            "key_prefix": key_prefix,
            "status": APIKeyStatus.ACTIVE.value,
            "key_name": key_name,
            "created_at": now,
            "expires_at": expires_at,
            "max_uses": max_uses,
            "allowed_tools": list(allowed_tools) if allowed_tools is not None else None,
            "tenant": tenant,
        }

    async def get_key_record(self, key_id: str) -> APIKeyModel | None:
        """Load a key row after a derived credential has been validated."""
        async with self._session_factory()() as session:
            result = await session.execute(
                select(APIKeyModel).where(col(APIKeyModel.key_id) == key_id)
            )
            return result.scalar_one_or_none()

    async def get_keys(self, wallet_id: str) -> dict:
        """
        Get all API keys for a wallet (masked).

        Args:
            wallet_id: Wallet to get keys for

        Returns:
            {
                "wallet_id": str,
                "keys": list[APIKeyResponse],
                "total_active": int,
                "total_revoked": int,
            }
        """
        async with self._session_factory()() as session:
            wallet_result = await session.execute(
                select(WalletModel).where(col(WalletModel.wallet_id) == wallet_id)
            )
            if not wallet_result.scalar_one_or_none():
                raise WalletNotFoundError(wallet_id)

            result = await session.execute(
                select(APIKeyModel).where(col(APIKeyModel.wallet_id) == wallet_id)
            )
            keys = list(result.scalars().all())

        response_keys = []
        total_active = 0
        total_revoked = 0
        now = utc_now()

        for key in keys:
            metadata = {}
            if key.metadata_json:
                try:
                    metadata = json.loads(key.metadata_json)
                except json.JSONDecodeError:
                    pass

            response_keys.append(
                {
                    "key_id": key.key_id,
                    "wallet_id": key.wallet_id,
                    "key_prefix": key.key_prefix,
                    "masked_key": f"{key.key_prefix}...****",
                    "status": key.status,
                    "key_name": metadata.get("name", "default"),
                    "rotation_count": key.rotation_count,
                    "last_used_at": key.last_used_at,
                    "created_at": key.created_at,
                    "expires_at": key.expires_at,
                    "max_uses": key.max_uses,
                    "use_count": key.use_count,
                }
            )

            # total_active counts keys that could authenticate now, not keys
            # whose status still reads "active" after expiry or exhaustion.
            if _key_is_live(key, now):
                total_active += 1
            elif key.status == APIKeyStatus.REVOKED.value:
                total_revoked += 1

        return {
            "wallet_id": wallet_id,
            "keys": response_keys,
            "total_active": total_active,
            "total_revoked": total_revoked,
        }

    async def has_live_key(self, wallet_id: str) -> bool:
        """True if the wallet holds at least one key that could authenticate now.

        Deliberately mirrors ``validate_key``'s liveness rule — ACTIVE status,
        budget not spent, *and* not past ``expires_at`` (``_key_is_live``).
        Nothing sweeps expired keys to a non-active status, so a status-only
        check reports a wallet as credentialed after every one of its keys has
        timed out, and any gate built on it would outlive the credentials it is
        meant to track.
        """
        now = utc_now()
        async with self._session_factory()() as session:
            result = await session.execute(
                select(APIKeyModel).where(
                    col(APIKeyModel.wallet_id) == wallet_id,
                    col(APIKeyModel.status) == APIKeyStatus.ACTIVE.value,
                )
            )
            return any(_key_is_live(key, now) for key in result.scalars().all())

    async def is_key_live(self, key_id: str) -> bool:
        """True if this specific key could authenticate right now.

        Wallet-level liveness is too coarse for revocation containment: a wallet
        holding several keys stays "live" after the compromised one is revoked,
        and auto_rotate_on_suspicious_activity revokes the suspect key while
        issuing a replacement, so the wallet is never keyless. Checking the
        originating key directly is what makes revoking one credential actually
        invalidate what that credential minted. An unknown key_id is not live.
        """
        async with self._session_factory()() as session:
            result = await session.execute(
                select(APIKeyModel).where(col(APIKeyModel.key_id) == key_id)
            )
            key = result.scalar_one_or_none()
        return key is not None and _key_is_live(key, utc_now())

    async def consume_derived_key_use(self, key_id: str, wallet_id: str) -> bool:
        """Atomically authenticate derived authority against its origin's budget.

        The signed JWT proves the key identity. This guarded write binds its
        wallet, status, expiry and remaining uses without needing the raw key.
        """
        now = to_naive_utc(utc_now())
        async with self._session_factory()() as session:
            consumed = await session.execute(
                update(APIKeyModel)
                .where(
                    col(APIKeyModel.key_id) == key_id,
                    col(APIKeyModel.wallet_id) == wallet_id,
                    col(APIKeyModel.status) == APIKeyStatus.ACTIVE.value,
                    or_(
                        col(APIKeyModel.expires_at).is_(None),
                        col(APIKeyModel.expires_at) >= now,
                    ),
                    or_(
                        col(APIKeyModel.max_uses).is_(None),
                        col(APIKeyModel.use_count) < col(APIKeyModel.max_uses),
                    ),
                )
                .values(
                    use_count=case(
                        (col(APIKeyModel.max_uses).is_(None), APIKeyModel.use_count),
                        else_=APIKeyModel.use_count + 1,
                    ),
                    last_used_at=now,
                    first_used_at=func.coalesce(APIKeyModel.first_used_at, now),
                )
                .execution_options(synchronize_session=False)
            )
            await session.commit()
            return (cast(Any, consumed).rowcount or 0) == 1

    async def is_key_bounded(self, key_id: str | None) -> bool:
        """True if this key carries a use budget (max_uses) or an expiry.

        A bounded key must not mint fresh credentials for its wallet: a new
        key takes only the bounds its request names, so a capped or expiring
        key could otherwise outlive its own limits through an unlimited
        sibling. A missing or unknown key_id counts as bounded (fail closed).
        """
        if key_id is None:
            return True
        async with self._session_factory()() as session:
            result = await session.execute(
                select(APIKeyModel).where(col(APIKeyModel.key_id) == key_id)
            )
            key = result.scalar_one_or_none()
        if key is None:
            return True
        return (
            key.max_uses is not None
            or key.expires_at is not None
            or key.allowed_tools_json is not None
            or key.tenant is not None
        )

    async def validate_key(
        self, api_key: str, *, consume: bool = True
    ) -> Optional[APIKeyModel]:
        """
        Validate an API key and return the key model if valid.

        Args:
            api_key: The API key to validate
            consume: Set False only for the demo policy preflight. It matches
                the digest, prefix, and active status without checking expiry
                or remaining uses or recording usage. Authenticate with the
                default True afterward.

        Returns:
            Live APIKeyModel if valid, None if invalid. The read-only preflight
            may return an expired or exhausted key for policy classification.
        """
        if not api_key or len(api_key) < 8:
            return None

        key_prefix = api_key[:API_KEY_PREFIX_LENGTH]
        pepper = _api_key_hash_pepper()
        if pepper is None:
            # Fail closed: a production-like process without a pepper
            # authenticates nothing (startup also refuses to boot).
            logger.error("API key hashing is not configured; rejecting key")
            return None
        if len(api_key) <= API_KEY_LOOKUP_LENGTH:
            return None
        lookup_id = api_key_lookup_id(api_key)

        async with self._session_factory()() as session:
            # A slow salted hash cannot be searched, so find candidate rows by
            # the indexed, non-secret lookup id and verify each with scrypt.
            # The id carries 72 random bits, so more than one live row is
            # practically impossible; checking all candidates keeps a
            # collision from turning into a 500.
            result = await session.execute(
                select(APIKeyModel).where(
                    col(APIKeyModel.key_lookup) == lookup_id,
                    col(APIKeyModel.key_prefix) == key_prefix,
                    col(APIKeyModel.status) == APIKeyStatus.ACTIVE.value,
                )
            )
            key = None
            for candidate in result.scalars().all():
                if await _verify_presented_key(pepper, candidate.key_hash, api_key):
                    key = candidate
                    break

            if not key:
                return None

            if not consume:
                return key

            now = utc_now()
            expires_at = key.expires_at
            if expires_at and expires_at < now:
                return None

            if key.max_uses is not None:
                # Guarded increment: the WHERE clause makes concurrent
                # requests race for the remaining budget instead of all
                # reading the same stale count, so max_uses cannot be
                # overshot. rowcount 0 means the budget is spent.
                # The expiry predicate is re-read here (not reused from the
                # check above) so a key that passes the read and then times
                # out before this write lands consumes nothing: expiry is
                # enforced at write time, mirroring consume_derived_key_use.
                consumed = await session.execute(
                    update(APIKeyModel)
                    .where(
                        col(APIKeyModel.key_id) == key.key_id,
                        col(APIKeyModel.status) == APIKeyStatus.ACTIVE.value,
                        col(APIKeyModel.use_count) < col(APIKeyModel.max_uses),
                        or_(
                            col(APIKeyModel.expires_at).is_(None),
                            col(APIKeyModel.expires_at) >= to_naive_utc(utc_now()),
                        ),
                    )
                    .values(
                        use_count=APIKeyModel.use_count + 1,
                        last_used_at=to_naive_utc(now),
                        first_used_at=func.coalesce(
                            APIKeyModel.first_used_at, to_naive_utc(now)
                        ),
                    )
                    .execution_options(synchronize_session=False)
                )
                await session.commit()
                if (cast(Any, consumed).rowcount or 0) == 0:
                    return None
            else:
                used = await session.execute(
                    update(APIKeyModel)
                    .where(
                        col(APIKeyModel.key_id) == key.key_id,
                        col(APIKeyModel.status) == APIKeyStatus.ACTIVE.value,
                        or_(
                            col(APIKeyModel.expires_at).is_(None),
                            col(APIKeyModel.expires_at) >= to_naive_utc(utc_now()),
                        ),
                    )
                    .values(
                        last_used_at=to_naive_utc(now),
                        first_used_at=func.coalesce(
                            APIKeyModel.first_used_at, to_naive_utc(now)
                        ),
                    )
                    .execution_options(synchronize_session=False)
                )
                await session.commit()
                if (cast(Any, used).rowcount or 0) == 0:
                    return None

        return key

    async def rotate_key(
        self,
        wallet_id: str,
        key_id: str | None = None,
        revoke_old: bool = False,
        reason: str = "manual_rotation",
        triggered_by: str = "user",
        ip_address: str | None = None,
    ) -> dict:
        """
        Rotate an API key.

        Args:
            wallet_id: Wallet owning the key
            key_id: Specific key to rotate (None = create new key only)
            revoke_old: Whether to revoke the old key
            reason: Reason for rotation
            triggered_by: What triggered the rotation
            ip_address: IP address of the requester

        Returns:
            {
                "rotation_id": str,
                "wallet_id": str,
                "old_key_id": str | None,
                "new_key": dict | None,
                "rotation_type": str,
                "revoked_keys": list[str],
                "created_at": datetime,
            }
        """
        if revoke_old and not key_id:
            raise InvalidRotationRequestError(
                "key_id is required when revoke_old is true"
            )

        old_key_id = None
        now = utc_now()
        persisted_now = to_naive_utc(now)
        rotation_id = f"rot_{uuid4().hex[:12]}"
        rotation_type = (
            RotationType.MANUAL.value
            if triggered_by == "user"
            else RotationType.AUTOMATIC.value
        )

        async with self._session_factory()() as session:
            wallet_result = await session.execute(
                select(WalletModel).where(col(WalletModel.wallet_id) == wallet_id)
            )
            wallet = wallet_result.scalar_one_or_none()
            if wallet is None:
                raise WalletNotFoundError(wallet_id)

            inherited_expires_at = None
            inherited_max_uses = None
            inherited_name = "rotated_key"
            inherited_allowed_tools_json = None
            inherited_tenant = wallet.tenant

            if key_id:
                # FOR UPDATE so a concurrent validate_key cannot consume a
                # use between this snapshot and the revocation commit — the
                # remaining budget we transfer must be the final one. SQLite
                # ignores the lock but serializes writers anyway.
                result = await session.execute(
                    select(APIKeyModel)
                    .where(
                        col(APIKeyModel.key_id) == key_id,
                        col(APIKeyModel.wallet_id) == wallet_id,
                    )
                    .with_for_update()
                )
                old_key = result.scalar_one_or_none()

                if not old_key:
                    raise KeyNotFoundError(key_id)

                if old_key.status != APIKeyStatus.ACTIVE.value:
                    # Rotating a revoked key would re-mint authority that
                    # revocation (including emergency revocation) removed —
                    # and it is also how two concurrent rotations of the same
                    # key are serialized: the second one finds the source
                    # already revoked and fails here instead of duplicating
                    # the transferred budget.
                    raise InvalidRotationRequestError(
                        "cannot rotate a key that is not active"
                    )

                if not revoke_old and old_key.max_uses is not None:
                    # Keeping the old key active while the new one carries the
                    # same remaining budget would double a finite max_uses —
                    # and rotate is reachable with the wallet's own key, so a
                    # capped key could fork its budget indefinitely.
                    raise InvalidRotationRequestError(
                        "revoke_old is required when rotating a key with a "
                        "use budget (max_uses): keeping the old key active "
                        "would duplicate its remaining uses"
                    )

                old_key_id = old_key.key_id

                # Rotation replaces the credential, not its authority: the new
                # key carries the old key's original expiry and its remaining
                # use budget, so a wallet-scoped caller cannot widen its own
                # bounds by rotating (rotate is reachable with the wallet's
                # own key, not just bootstrap). Operators wanting fresh bounds
                # mint a new key explicitly instead.
                inherited_expires_at = old_key.expires_at
                if old_key.max_uses is not None:
                    inherited_max_uses = max(old_key.max_uses - old_key.use_count, 0)
                inherited_allowed_tools_json = old_key.allowed_tools_json
                inherited_tenant = wallet.tenant or old_key.tenant
                if old_key.metadata_json:
                    try:
                        inherited_name = json.loads(old_key.metadata_json).get(
                            "name", inherited_name
                        )
                    except json.JSONDecodeError:
                        pass

            full_key, key_hash, key_prefix = await asyncio.to_thread(generate_api_key)
            new_key_id = f"key_{uuid4().hex[:12]}"
            new_key_prefix = key_prefix

            new_key = APIKeyModel(
                key_id=new_key_id,
                wallet_id=wallet_id,
                key_hash=key_hash,
                key_lookup=api_key_lookup_id(full_key),
                key_prefix=new_key_prefix,
                status=APIKeyStatus.ACTIVE.value,
                expires_at=inherited_expires_at,
                max_uses=inherited_max_uses,
                metadata_json=json.dumps({"name": inherited_name}),
                allowed_tools_json=inherited_allowed_tools_json,
                tenant=inherited_tenant,
            )
            session.add(new_key)

            if old_key_id:
                rotation_update = (
                    update(APIKeyModel)
                    .where(col(APIKeyModel.key_id) == old_key_id)
                    .values(
                        rotation_count=APIKeyModel.rotation_count + 1,
                        last_rotated_at=persisted_now,
                    )
                )
                if revoke_old:
                    rotation_update = rotation_update.values(
                        status=APIKeyStatus.REVOKED.value,
                        revoked_at=persisted_now,
                        revoke_reason=reason,
                    )
                await session.execute(rotation_update)

            log_entry = KeyRotationLogModel(
                log_id=rotation_id,
                key_id=new_key_id,
                wallet_id=wallet_id,
                rotation_type=rotation_type,
                old_key_id=old_key_id,
                new_key_id=new_key_id,
                trigger_reason=reason,
                triggered_by=triggered_by,
                ip_address=ip_address,
                created_at=persisted_now,
            )
            session.add(log_entry)

            await session.commit()

        new_key_data = {
            "key_id": new_key_id,
            "wallet_id": wallet_id,
            "api_key": full_key,
            "key_prefix": new_key_prefix,
            "status": APIKeyStatus.ACTIVE.value,
            "key_name": inherited_name,
            "created_at": now,
            "expires_at": inherited_expires_at,
            "max_uses": inherited_max_uses,
        }

        logger.info(
            f"Rotated API key for wallet {wallet_id}: "
            f"old={old_key_id}, new={new_key_id}, reason={reason}"
        )

        return {
            "rotation_id": rotation_id,
            "wallet_id": wallet_id,
            "old_key_id": old_key_id,
            "new_key": new_key_data,
            "rotation_type": rotation_type,
            "revoked_keys": [old_key_id] if (old_key_id and revoke_old) else [],
            "created_at": now,
        }

    async def revoke_key(
        self,
        wallet_id: str,
        key_id: str,
        reason: str = "user_request",
    ) -> bool:
        """
        Revoke an API key.

        The write matches only a key that is not already revoked. A repeat
        call returns True and leaves the stored ``revoked_at`` and
        ``revoke_reason`` in place. A missing key still raises
        ``KeyNotFoundError``.

        Args:
            wallet_id: Wallet owning the key
            key_id: Key to revoke
            reason: Reason for revocation

        Returns:
            True when the key is revoked, including when it already was.
        """
        async with self._session_factory()() as session:
            updated = await session.execute(
                update(APIKeyModel)
                .where(
                    col(APIKeyModel.key_id) == key_id,
                    col(APIKeyModel.wallet_id) == wallet_id,
                    col(APIKeyModel.status) != APIKeyStatus.REVOKED.value,
                )
                .values(
                    status=APIKeyStatus.REVOKED.value,
                    revoked_at=utc_now(),
                    revoke_reason=reason,
                )
                .execution_options(synchronize_session=False)
            )
            wrote = (cast(Any, updated).rowcount or 0) == 1
            if not wrote:
                existing = await session.execute(
                    select(APIKeyModel).where(
                        col(APIKeyModel.key_id) == key_id,
                        col(APIKeyModel.wallet_id) == wallet_id,
                    )
                )
                if existing.scalar_one_or_none() is None:
                    raise KeyNotFoundError(key_id)
            # Commit whenever the key exists. rowcount decides only whether
            # this call performed the revocation; a false 0 must not roll
            # the guarded write back and report success.
            await session.commit()

        if wrote:
            logger.warning(f"Revoked API key {key_id} for wallet {wallet_id}: {reason}")
        return True

    async def emergency_revocation(
        self,
        wallet_id: str,
        reason: str = "security_incident",
        create_new_key: bool = True,
        bounding_key_id: str | None = None,
    ) -> dict:
        """
        Immediately revoke all keys for a wallet and optionally create new ones.

        Args:
            wallet_id: Wallet to revoke keys for
            reason: Reason for emergency revocation
            create_new_key: Whether to create a new emergency key
            bounding_key_id: The authenticating caller's own key. When set,
                the replacement takes its bounds from this key alone, and no
                replacement is minted if it is not a non-expired active key
                of the wallet. None (bootstrap admins) keeps the wallet-wide
                donor rule.

        Returns:
            {
                "wallet_id": str,
                "revoked_keys": list[str],
                "new_key": dict | None,
                "created_at": datetime,
            }
        """
        now = utc_now()
        persisted_now = to_naive_utc(now)

        async with self._session_factory()() as session:
            wallet_result = await session.execute(
                select(WalletModel).where(col(WalletModel.wallet_id) == wallet_id)
            )
            wallet = wallet_result.scalar_one_or_none()
            if wallet is None:
                raise WalletNotFoundError(wallet_id)

            # FOR UPDATE so a concurrent validate_key cannot consume a use
            # between this snapshot and the revocation commit — the bounds
            # derived below must reflect final use counts. SQLite ignores the
            # lock but serializes writers anyway.
            result = await session.execute(
                select(APIKeyModel)
                .where(
                    col(APIKeyModel.wallet_id) == wallet_id,
                    col(APIKeyModel.status) == APIKeyStatus.ACTIVE.value,
                )
                .with_for_update()
            )
            active_keys = list(result.scalars().all())

            revoked_key_ids = []
            for key in active_keys:
                key.status = APIKeyStatus.REVOKED.value
                key.revoked_at = persisted_now
                key.revoke_reason = f"EMERGENCY: {reason}"
                session.add(key)
                revoked_key_ids.append(key.key_id)

            # Revoking the API keys alone does not contain a compromise: a JWT
            # minted from a stolen key before revocation keeps working, and
            # POST /v1/auth/refresh re-issues access tokens from it for the
            # refresh token's full lifetime without ever re-checking that the
            # wallet still has a live key. Emergency revocation must therefore
            # kill the derived credentials too, not just the keys they came from.
            refresh_result = await session.execute(
                select(RefreshTokenModel).where(
                    col(RefreshTokenModel.wallet_id) == wallet_id,
                    col(RefreshTokenModel.revoked).is_(False),
                )
            )
            revoked_refresh_tokens = 0
            for token in refresh_result.scalars().all():
                token.revoked = True
                session.add(token)
                revoked_refresh_tokens += 1

            log_entry = KeyRotationLogModel(
                log_id=f"rot_{uuid4().hex[:12]}",
                key_id="all",
                wallet_id=wallet_id,
                rotation_type=RotationType.EMERGENCY.value,
                trigger_reason=reason,
                triggered_by="emergency_system",
                created_at=persisted_now,
            )
            session.add(log_entry)

            new_key_data = None
            if create_new_key:
                # The replacement must not exceed the authority the wallet
                # could exercise when this call was authorized
                # (emergency-revoke is reachable with the wallet's own key,
                # so an unbounded replacement would let a capped key launder
                # itself into an unlimited one). The basis is every
                # non-expired ACTIVE key: an exhausted key still counts,
                # contributing zero remaining budget, because the caller may
                # have spent that key's last use authenticating this very
                # request — filtering it out would hand back an unbounded
                # replacement. With no non-expired active key at all, no
                # wallet credential could have authenticated, so the caller
                # is a bootstrap admin and an unbounded emergency key is not
                # an escalation.
                bounding_keys = [
                    key
                    for key in active_keys
                    if not key.expires_at or key.expires_at >= now
                ]
                if bounding_key_id is not None:
                    # A wallet-scoped caller may only carry its own key's
                    # authority over: picking the loosest sibling would let a
                    # capped key come out of the incident with an unbounded
                    # replacement it never held.
                    bounding_keys = [
                        key for key in bounding_keys if key.key_id == bounding_key_id
                    ]
                    if not bounding_keys:
                        create_new_key = False

            if create_new_key:
                emergency_expires_at = None
                emergency_max_uses = None
                emergency_allowed_tools_json = None
                emergency_tenant = wallet.tenant
                if bounding_keys:
                    # Inherit BOTH bounds from one donor credential rather
                    # than combining the loosest budget and loosest expiry
                    # across keys: independent maxima could yield authority
                    # (say, a big budget with unlimited lifetime) that no
                    # revoked key actually had. The donor is the key with the
                    # largest remaining budget (unlimited first), tie-broken
                    # by latest expiry (never-expiring counts as latest).
                    def _donor_rank(key: APIKeyModel) -> tuple:
                        if key.max_uses is None:
                            budget = (1, 0)
                        else:
                            budget = (0, max(key.max_uses - key.use_count, 0))
                        if key.expires_at is None:
                            expiry = (1, datetime.min)
                        else:
                            expiry = (0, key.expires_at)
                        return (budget, expiry)

                    donor = max(bounding_keys, key=_donor_rank)
                    emergency_expires_at = donor.expires_at
                    if donor.max_uses is not None:
                        emergency_max_uses = max(donor.max_uses - donor.use_count, 0)
                    emergency_allowed_tools_json = donor.allowed_tools_json
                    emergency_tenant = wallet.tenant or donor.tenant

                full_key, key_hash, key_prefix = await asyncio.to_thread(
                    generate_api_key
                )
                new_key_id = f"key_{uuid4().hex[:12]}"
                emergency_key = APIKeyModel(
                    key_id=new_key_id,
                    wallet_id=wallet_id,
                    key_hash=key_hash,
                    key_lookup=api_key_lookup_id(full_key),
                    key_prefix=key_prefix,
                    status=APIKeyStatus.ACTIVE.value,
                    metadata_json=json.dumps({"name": "emergency_key"}),
                    expires_at=emergency_expires_at,
                    max_uses=emergency_max_uses,
                    allowed_tools_json=emergency_allowed_tools_json,
                    tenant=emergency_tenant,
                )
                session.add(emergency_key)
                new_key_data = {
                    "key_id": new_key_id,
                    "wallet_id": wallet_id,
                    "api_key": full_key,
                    "key_prefix": key_prefix,
                    "status": APIKeyStatus.ACTIVE.value,
                    "key_name": "emergency_key",
                    "created_at": now,
                    "expires_at": emergency_expires_at,
                    "max_uses": emergency_max_uses,
                }

            await session.commit()

        logger.critical(
            f"EMERGENCY revocation for wallet {wallet_id}: "
            f"revoked {len(revoked_key_ids)} keys and "
            f"{revoked_refresh_tokens} refresh tokens, reason={reason}"
        )

        from ..services.notifications import get_notification_service

        notifications = get_notification_service()
        await notifications.send_security_alert(
            wallet_id=wallet_id,
            alert_type="emergency_key_revocation",
            message=f"All API keys revoked for wallet {wallet_id}. Reason: {reason}",
        )

        return {
            "wallet_id": wallet_id,
            "revoked_keys": revoked_key_ids,
            "revoked_refresh_tokens": revoked_refresh_tokens,
            "new_key": new_key_data,
            "created_at": now,
        }

    async def get_rotation_logs(
        self,
        wallet_id: str,
        limit: int = 50,
    ) -> list[dict]:
        """
        Get rotation audit logs for a wallet.

        Args:
            wallet_id: Wallet to get logs for
            limit: Maximum number of logs to return

        Returns:
            list of rotation log entries
        """
        async with self._session_factory()() as session:
            result = await session.execute(
                select(KeyRotationLogModel)
                .where(col(KeyRotationLogModel.wallet_id) == wallet_id)
                .order_by(col(KeyRotationLogModel.created_at).desc())
                .limit(limit)
            )
            logs = list(result.scalars().all())

        return [
            {
                "log_id": log.log_id,
                "key_id": log.key_id,
                "wallet_id": log.wallet_id,
                "rotation_type": log.rotation_type,
                "old_key_id": log.old_key_id,
                "new_key_id": log.new_key_id,
                "trigger_reason": log.trigger_reason,
                "triggered_by": log.triggered_by,
                "created_at": log.created_at,
            }
            for log in logs
        ]

    async def auto_rotate_on_suspicious_activity(
        self,
        wallet_id: str,
        reason: str,
    ) -> dict:
        """
        Automatically rotate keys when suspicious activity is detected.

        Args:
            wallet_id: Wallet to rotate keys for
            reason: Reason for automatic rotation

        Returns:
            Rotation result dict

        Raises:
            InvalidRotationRequestError: the wallet holds no live key. There
                is no credential to contain, and falling back to
                ``rotate_key(key_id=None)`` would mint a fresh key with no
                bounds at all.
        """
        now = utc_now()
        async with self._session_factory()() as session:
            result = await session.execute(
                select(APIKeyModel).where(
                    col(APIKeyModel.wallet_id) == wallet_id,
                    col(APIKeyModel.status) == APIKeyStatus.ACTIVE.value,
                )
            )
            # Pick a key that could still authenticate: an expired or
            # exhausted ACTIVE key is not the suspect credential, and
            # rotating it would leave the live one untouched.
            live_key = next(
                (key for key in result.scalars().all() if _key_is_live(key, now)),
                None,
            )

        if live_key is None:
            raise InvalidRotationRequestError(
                f"wallet {wallet_id} has no live API key to rotate"
            )

        rotation_result = await self.rotate_key(
            wallet_id=wallet_id,
            key_id=live_key.key_id,
            revoke_old=True,
            reason=f"AUTOMATIC: {reason}",
            triggered_by="security_system",
        )
        rotation_result["rotation_type"] = RotationType.AUTOMATIC.value

        return rotation_result


_api_key_service: Optional[APIKeyService] = None


def get_api_key_service() -> APIKeyService:
    """Get or create the APIKeyService singleton."""
    global _api_key_service
    if _api_key_service is None:
        _api_key_service = APIKeyService()
    return _api_key_service
