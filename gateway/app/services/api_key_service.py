"""
API Key Rotation Service for wallet security.

Handles API key creation, rotation, and revocation for compromised wallets.
Supports automatic rotation on suspicious activity detection.

Architecture:
1. Keys are stored hashed (SHA-256) in the database
2. Only the key prefix and masked key are ever shown
3. Rotation creates new key and optionally revokes old ones
4. Emergency revocation immediately invalidates all keys
"""

import hashlib
import hmac
import json
import secrets
import logging
from datetime import datetime, timedelta
from typing import Any, Optional, cast
from uuid import uuid4

from sqlalchemy import case, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from ..core.time import to_naive_utc, utc_now
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


def generate_api_key() -> tuple[str, str, str]:
    """
    Generate a new API key.

    Returns:
        tuple: (full_key, key_hash, key_prefix)
    """
    full_key = f"b2a_{secrets.token_urlsafe(API_KEY_LENGTH)}"
    key_hash = hashlib.sha256(full_key.encode()).hexdigest()
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
            if result.scalar_one_or_none() is None:
                raise WalletNotFoundError(wallet_id)
        else:
            async with self._session_factory()() as check_session:
                result = await check_session.execute(
                    select(WalletModel).where(col(WalletModel.wallet_id) == wallet_id)
                )
                if result.scalar_one_or_none() is None:
                    raise WalletNotFoundError(wallet_id)

        full_key, key_hash, key_prefix = generate_api_key()
        key_id = f"key_{uuid4().hex[:12]}"
        now = utc_now()
        expires_at = None

        if expires_in_days:
            expires_at = now + timedelta(days=expires_in_days)

        api_key = APIKeyModel(
            key_id=key_id,
            wallet_id=wallet_id,
            key_hash=key_hash,
            key_prefix=key_prefix,
            status=APIKeyStatus.ACTIVE.value,
            metadata_json=json.dumps({"name": key_name}),
            expires_at=(to_naive_utc(expires_at) if expires_at else None),
            max_uses=max_uses,
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
        }

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
        return key.max_uses is not None or key.expires_at is not None

    async def validate_key(self, api_key: str) -> Optional[APIKeyModel]:
        """
        Validate an API key and return the key model if valid.

        Args:
            api_key: The API key to validate

        Returns:
            APIKeyModel if valid, None if invalid
        """
        if not api_key or len(api_key) < 8:
            return None

        key_prefix = api_key[:API_KEY_PREFIX_LENGTH]
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()

        async with self._session_factory()() as session:
            # Look up by the indexed full digest, not the prefix alone: the
            # prefix is "b2a_" plus four random characters and is not unique,
            # so two live keys sharing it are expected at a few thousand keys
            # (and can be ground out deliberately by any caller able to mint
            # keys). A prefix-only lookup then returned several rows and
            # failed every request for both keys with a 500.
            result = await session.execute(
                select(APIKeyModel).where(
                    col(APIKeyModel.key_hash) == key_hash,
                    col(APIKeyModel.key_prefix) == key_prefix,
                    col(APIKeyModel.status) == APIKeyStatus.ACTIVE.value,
                )
            )
            key = result.scalar_one_or_none()

            if not key:
                return None

            if not hmac.compare_digest(key.key_hash, key_hash):
                return None

            now = utc_now()
            expires_at = key.expires_at
            if expires_at and expires_at < now:
                return None

            if key.max_uses is not None:
                # Guarded increment: the WHERE clause makes concurrent
                # requests race for the remaining budget instead of all
                # reading the same stale count, so max_uses cannot be
                # overshot. rowcount 0 means the budget is spent.
                consumed = await session.execute(
                    update(APIKeyModel)
                    .where(
                        col(APIKeyModel.key_id) == key.key_id,
                        col(APIKeyModel.status) == APIKeyStatus.ACTIVE.value,
                        col(APIKeyModel.use_count) < col(APIKeyModel.max_uses),
                    )
                    .values(
                        use_count=APIKeyModel.use_count + 1,
                        last_used_at=to_naive_utc(now),
                    )
                    .execution_options(synchronize_session=False)
                )
                await session.commit()
                if (cast(Any, consumed).rowcount or 0) == 0:
                    return None
            else:
                key.last_used_at = to_naive_utc(now)
                session.add(key)
                await session.commit()

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
            if not wallet_result.scalar_one_or_none():
                raise WalletNotFoundError(wallet_id)

            inherited_expires_at = None
            inherited_max_uses = None
            inherited_name = "rotated_key"

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
                if old_key.metadata_json:
                    try:
                        inherited_name = json.loads(old_key.metadata_json).get(
                            "name", inherited_name
                        )
                    except json.JSONDecodeError:
                        pass

            full_key, key_hash, key_prefix = generate_api_key()
            new_key_id = f"key_{uuid4().hex[:12]}"
            new_key_prefix = key_prefix

            new_key = APIKeyModel(
                key_id=new_key_id,
                wallet_id=wallet_id,
                key_hash=key_hash,
                key_prefix=new_key_prefix,
                status=APIKeyStatus.ACTIVE.value,
                expires_at=inherited_expires_at,
                max_uses=inherited_max_uses,
                metadata_json=json.dumps({"name": inherited_name}),
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

        Args:
            wallet_id: Wallet owning the key
            key_id: Key to revoke
            reason: Reason for revocation

        Returns:
            True if revoked successfully
        """
        async with self._session_factory()() as session:
            result = await session.execute(
                select(APIKeyModel).where(
                    col(APIKeyModel.key_id) == key_id,
                    col(APIKeyModel.wallet_id) == wallet_id,
                )
            )
            key = result.scalar_one_or_none()

            if not key:
                raise KeyNotFoundError(key_id)

            key.status = APIKeyStatus.REVOKED.value
            key.revoked_at = utc_now()
            key.revoke_reason = reason
            session.add(key)
            await session.commit()

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
            if not wallet_result.scalar_one_or_none():
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

                full_key, key_hash, key_prefix = generate_api_key()
                new_key_id = f"key_{uuid4().hex[:12]}"
                emergency_key = APIKeyModel(
                    key_id=new_key_id,
                    wallet_id=wallet_id,
                    key_hash=key_hash,
                    key_prefix=key_prefix,
                    status=APIKeyStatus.ACTIVE.value,
                    metadata_json=json.dumps({"name": "emergency_key"}),
                    expires_at=emergency_expires_at,
                    max_uses=emergency_max_uses,
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
