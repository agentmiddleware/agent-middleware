"""JWT access token creation and verification.

Uses Ed25519 for signing (reuse existing trust-plane signing key).
Short-lived: 15 min access, 7 day refresh.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from app.core.config import get_settings
from app.services.signing_keys import _decode_private_key


JWT_ALGORITHM = "EdDSA"
JWT_ISSUER = "agent-middleware-api"
JWT_AUDIENCE = "agent-middleware-api"
JWT_ACCESS_EXPIRY = 900  # 15 minutes
JWT_REFRESH_EXPIRY = 604800  # 7 days


@dataclass(frozen=True)
class JWTPayload:
    sub: str  # wallet_id
    key_id: str | None
    scopes: list[str]
    # Epoch seconds, as PyJWT decodes them. Callers that need a datetime
    # convert explicitly (see app.routers.auth._exp_to_datetime).
    iat: int
    exp: int
    iss: str
    aud: str
    jti: str  # unique token id
    type: str  # access | refresh


class JWTError(Exception):
    pass


class JWTService:
    """Create and verify JWT access tokens signed with Ed25519."""

    def _load_keys(self) -> tuple[Ed25519PrivateKey, str]:
        """Load private key and return (key, key_id)."""
        settings = get_settings()
        b64_key = settings.TRUST_SIGNING_PRIVATE_KEY_B64
        if not b64_key:
            raise JWTError("signing_key_not_configured")

        private_key = _decode_private_key(b64_key)

        # Derive key_id from public key hash (consistent with signing_keys.py)
        public_key = private_key.public_key()
        raw_public = public_key.public_bytes(
            encoding=Encoding.Raw,
            format=PublicFormat.Raw,
        )
        key_id = "ed25519-" + hashlib.sha256(raw_public).hexdigest()[:16]

        return private_key, key_id

    def create_access_token(
        self,
        wallet_id: str,
        key_id: str | None,
        scopes: list[str],
    ) -> str:
        """Create a short-lived access token."""
        private_key, signing_key_id = self._load_keys()
        now = datetime.now(timezone.utc)
        jti = f"jwt-{uuid.uuid4().hex[:16]}"

        payload = {
            "sub": wallet_id,
            "key_id": key_id,
            "scopes": scopes,
            "iat": now,
            "exp": now + timedelta(seconds=JWT_ACCESS_EXPIRY),
            "iss": JWT_ISSUER,
            "aud": JWT_AUDIENCE,
            "jti": jti,
            "type": "access",
            "kid": signing_key_id,
        }

        return jwt.encode(payload, private_key, algorithm=JWT_ALGORITHM)

    def create_refresh_token(
        self, wallet_id: str, scopes: list[str] | None = None
    ) -> str:
        """Create a long-lived refresh token."""
        private_key, signing_key_id = self._load_keys()
        now = datetime.now(timezone.utc)
        jti = f"jwt-refresh-{uuid.uuid4().hex[:16]}"

        payload = {
            "sub": wallet_id,
            "iat": now,
            "exp": now + timedelta(seconds=JWT_REFRESH_EXPIRY),
            "iss": JWT_ISSUER,
            "aud": JWT_AUDIENCE,
            "jti": jti,
            "type": "refresh",
            "scopes": scopes if scopes is not None else [],
            "kid": signing_key_id,
        }

        return jwt.encode(payload, private_key, algorithm=JWT_ALGORITHM)

    def _get_public_key(self) -> Ed25519PublicKey:
        """Derive public key from configured private key."""
        private_key, _ = self._load_keys()
        return private_key.public_key()

    def verify_token(self, token: str, token_type: str = "access") -> JWTPayload:
        """Verify a JWT and return its payload."""
        public_key = self._get_public_key()

        try:
            payload = jwt.decode(
                token,
                public_key,
                algorithms=[JWT_ALGORITHM],
                issuer=JWT_ISSUER,
                audience=JWT_AUDIENCE,
                # PyJWT checks exp only when it is present, so a signed token
                # without one would never expire; the rest are read below and
                # would otherwise surface as a KeyError, not a refused token.
                # Every token this service mints carries all of them.
                options={"require": ["exp", "iat", "iss", "aud", "sub", "jti"]},
            )
        except jwt.ExpiredSignatureError:
            raise JWTError("token_expired")
        except jwt.InvalidTokenError as e:
            raise JWTError(f"invalid_token: {e}")

        if payload.get("type") != token_type:
            raise JWTError(f"token_type_mismatch: expected {token_type}")

        scopes = payload.get("scopes")
        if not isinstance(scopes, list) or not all(
            isinstance(scope, str) for scope in scopes
        ):
            raise JWTError("invalid_or_missing_scopes")

        return JWTPayload(
            sub=payload["sub"],
            key_id=payload.get("key_id"),
            scopes=scopes,
            iat=payload["iat"],
            exp=payload["exp"],
            iss=payload["iss"],
            aud=payload["aud"],
            jti=payload["jti"],
            type=payload["type"],
        )

    def verify_access_token(self, token: str) -> JWTPayload:
        return self.verify_token(token, token_type="access")

    def verify_refresh_token(self, token: str) -> JWTPayload:
        return self.verify_token(token, token_type="refresh")


# Module-level singleton
_jwt_svc: JWTService | None = None


def get_jwt_service() -> JWTService:
    global _jwt_svc
    if _jwt_svc is None:
        _jwt_svc = JWTService()
    return _jwt_svc
