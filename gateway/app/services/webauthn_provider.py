"""
WebAuthn Provider — Phase 9
===========================

FIDO2/WebAuthn passkey authentication for high-risk AWI actions.

Based on arXiv:2506.10953v1 — Access control for agents section.
Enables biometric/passkey verification before executing sensitive operations
like checkout, payment, account deletion, etc.

Uses py_webauthn for real cryptographic verification:
- Signature verification against stored public key
- Challenge freshness validation
- RP ID and origin validation
- Authenticator counter checking (prevents cloned credentials)

Architecture:
1. Client calls POST /v1/awi/passkey/challenge → creates challenge
2. Client uses navigator.credentials.get() with challenge → gets credential
3. Client calls POST /v1/awi/passkey/verify → verifies credential (cryptographic!)
4. Subsequent AWI action executions check verification status
"""

import base64
import logging
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from app.core.time import utc_now

logger = logging.getLogger(__name__)

PY_WEBAUTHN_AVAILABLE = False

try:
    from webauthn import (
        generate_authentication_options,
        verify_authentication_response,
        options_to_json,
        base64url_to_bytes,
        generate_challenge,
    )
    from webauthn.helpers import parse_credential_id

    PY_WEBAUTHN_AVAILABLE = True
except ImportError:
    pass  # py_webauthn not installed - see fail-closed behavior below


def _mock_verification_allowed() -> bool:
    """
    Whether mock WebAuthn verification is explicitly permitted.

    Defaults to False. Production MUST fail closed when py_webauthn is
    absent: a security provider that silently degrades to mock
    verification emits a false "verified" signal, which is worse than
    having no provider at all. Test config may opt in by setting
    WEBAUTHN_ALLOW_MOCK=true. Production-like boots refuse that flag via
    validate_trust_mode_guardrails.
    """
    # Prefer live env so test monkeypatch.setenv works even when
    # get_settings() is already cached.
    env = os.getenv("WEBAUTHN_ALLOW_MOCK")
    if env is not None and env.strip() != "":
        return env.strip().lower() in ("1", "true", "yes")

    from app.core.config import get_settings

    return bool(get_settings().WEBAUTHN_ALLOW_MOCK)


class ChallengeStatus(str, Enum):
    """Status of a WebAuthn challenge."""

    PENDING = "pending"
    VERIFIED = "verified"
    FAILED = "failed"
    EXPIRED = "expired"


@dataclass
class WebAuthnChallenge:
    """Represents a WebAuthn authentication challenge."""

    challenge_id: str
    session_id: str
    action: str
    challenge_bytes: bytes
    challenge_b64: str  # Base64-encoded challenge for py_webauthn
    status: ChallengeStatus
    created_at: datetime
    expires_at: datetime
    rp_id: str
    rp_name: str
    # Challenges exist only for HIGH_RISK_ACTIONS, so user verification
    # (PIN/biometric), not mere presence, is always required. See
    # _verify_authenticator_assertion, which enforces it.
    user_verification: str = "required"
    attestations: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class VerificationRecord:
    """Record of a successful passkey verification."""

    session_id: str
    action: str
    verified_at: datetime
    expires_at: datetime
    credential_id: str


@dataclass
class CredentialRecord:
    """Record of a registered WebAuthn credential (public key)."""

    credential_id: str
    user_id: str
    public_key: bytes
    sign_count: int = 0
    created_at: datetime = field(default_factory=utc_now)
    name: str = ""


class WebAuthnProvider:
    """
    WebAuthn/Passkey flow for high-risk AWI actions.

    This provider implements the FIDO2 WebAuthn specification to enable
    biometric authentication (TouchID, FaceID, Windows Hello, etc.) for
    critical operations.

    High-risk actions that require passkey verification:
    - checkout, payment, transfer_funds
    - delete_account, change_password
    - modify_billing, add_payment_method
    - submit_pii, export_user_data

    Verification is valid for 5 minutes by default, reducing friction
    while maintaining security for multi-step flows.
    """

    HIGH_RISK_ACTIONS: set[str] = {
        "checkout",
        "payment",
        "transfer_funds",
        "delete_account",
        "change_password",
        "modify_billing",
        "add_payment_method",
        "submit_pii",
        "export_user_data",
        "remove_payment_method",
        "close_account",
        "transfer_ownership",
    }

    def __init__(
        self,
        rp_id: str = "localhost",
        rp_name: str = "Agent Middleware API",
        timeout_ms: int = 60000,
        challenge_expiry_seconds: int = 300,
        verification_validity_seconds: int = 300,
        allowed_origins: list[str] | None = None,
    ):
        """
        Initialize the WebAuthn provider.

        Args:
            rp_id: Relying Party ID (domain name). Must match the domain serving the app.
            rp_name: Human-readable name for the Relying Party.
            timeout_ms: Challenge timeout in milliseconds (default 60s).
            challenge_expiry_seconds: How long a challenge remains valid (default 5min).
            verification_validity_seconds: How long a verified action remains valid (default 5min).
            allowed_origins: List of allowed origins for verification (e.g., ["https://example.com"]).
        """
        self._rp_id = rp_id
        self._rp_name = rp_name
        self._timeout_ms = timeout_ms
        self._challenge_expiry = challenge_expiry_seconds
        self._verification_validity = verification_validity_seconds
        self._allowed_origins = allowed_origins or [f"https://{rp_id}"]

        self._challenges: dict[str, WebAuthnChallenge] = {}
        self._verifications: dict[str, VerificationRecord] = {}
        self._credentials: dict[
            str, CredentialRecord
        ] = {}  # credential_id -> CredentialRecord

    async def requires_passkey(self, session_id: str, action: str) -> bool:
        """
        Check if an action requires passkey verification.

        Args:
            session_id: AWI session ID.
            action: The action being attempted.

        Returns:
            True if passkey verification is required.
        """
        return action.lower() in self.HIGH_RISK_ACTIONS

    async def create_challenge(
        self,
        session_id: str,
        action: str,
        user_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Create a new WebAuthn authentication challenge.

        Generates a cryptographically random challenge and returns the
        options needed by the client to call navigator.credentials.get().

        Args:
            session_id: AWI session requiring verification.
            action: The action that requires verification.
            user_id: Optional user identifier for the credential.

        Returns:
            Dict with challenge options for the WebAuthn API.

        Raises:
            ValueError: If the action doesn't require verification.
        """
        if not await self.requires_passkey(session_id, action):
            raise ValueError(f"Action '{action}' does not require passkey verification")

        challenge_id = str(uuid4())
        if PY_WEBAUTHN_AVAILABLE:
            challenge_bytes = generate_challenge()  # 64 random bytes from py_webauthn
        else:
            challenge_bytes = secrets.token_bytes(32)  # Fallback

        # Also keep base64 version for our storage
        challenge_b64 = (
            base64.urlsafe_b64encode(challenge_bytes).decode("ascii").rstrip("=")
        )

        now = utc_now()
        challenge = WebAuthnChallenge(
            challenge_id=challenge_id,
            session_id=session_id,
            action=action,
            challenge_bytes=challenge_bytes,
            challenge_b64=challenge_b64,
            status=ChallengeStatus.PENDING,
            created_at=now,
            expires_at=now + timedelta(seconds=self._challenge_expiry),
            rp_id=self._rp_id,
            rp_name=self._rp_name,
        )

        self._challenges[challenge_id] = challenge

        logger.info(
            f"Created WebAuthn challenge {challenge_id} for session {session_id}, "
            f"action: {action}"
        )

        # Build allow_credentials list from registered credentials
        allow_credentials = []
        for cred_id, cred in self._credentials.items():
            if user_id is None or cred.user_id == user_id:
                allow_credentials.append(
                    {
                        "type": "public-key",
                        "id": cred_id,
                        "transports": ["usb", "nfc", "ble"],
                    }
                )

        # Use py_webauthn to generate proper options if available
        if PY_WEBAUTHN_AVAILABLE:
            try:
                from webauthn.helpers.structs import (
                    PublicKeyCredentialDescriptor,
                    UserVerificationRequirement,
                )

                options = generate_authentication_options(
                    rp_id=self._rp_id,
                    challenge=challenge_bytes,
                    timeout=self._timeout_ms / 1000,
                    allow_credentials=[
                        PublicKeyCredentialDescriptor(
                            id=base64url_to_bytes(cred_id),
                            transports=["usb", "nfc", "ble"],
                        )
                        for cred_id in self._credentials.keys()
                    ]
                    if self._credentials
                    else None,
                    user_verification=UserVerificationRequirement.REQUIRED,
                )

                # Convert to JSON-compatible dict
                options_dict = options_to_json(options)

                return {
                    "challenge_id": challenge_id,
                    **options_dict,
                    "timeout": self._timeout_ms,
                }
            except Exception as e:
                logger.warning(
                    f"py_webauthn options generation failed, using manual: {e}"
                )

        # Fallback to manual options
        return {
            "challenge_id": challenge_id,
            "challenge": challenge_b64,
            "rp_id": self._rp_id,
            "rp_name": self._rp_name,
            "timeout": self._timeout_ms,
            "user_verification": challenge.user_verification,
            "public_key_cred_params": [
                {"alg": -7, "type": "public-key"},  # ES256
                {"alg": -257, "type": "public-key"},  # RS256
            ],
            "allow_credentials": allow_credentials,
            "authenticator_selection": {
                "authenticator_attachment": "platform",
                "resident_key": "preferred",
                "user_verification": challenge.user_verification,
            },
        }

    async def verify_response(
        self,
        challenge_id: str,
        credential: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Verify a WebAuthn credential response from the client.

        Args:
            challenge_id: The challenge ID from create_challenge.
            credential: The credential response from navigator.credentials.get().

        Returns:
            Verification result with session_id, action, and expiry info.

        Raises:
            ValueError: If challenge is invalid, expired, or credential verification fails.
        """
        challenge = self._challenges.get(challenge_id)

        if not challenge:
            raise ValueError("Challenge not found")

        if challenge.status == ChallengeStatus.VERIFIED:
            raise ValueError("Challenge already verified")

        if challenge.status == ChallengeStatus.EXPIRED:
            raise ValueError("Challenge expired")

        if challenge.status == ChallengeStatus.FAILED:
            raise ValueError("Challenge verification previously failed")

        if utc_now() > challenge.expires_at:
            challenge.status = ChallengeStatus.EXPIRED
            raise ValueError("Challenge expired")

        credential_id = credential.get("id", "")
        if not credential_id:
            raise ValueError("Missing credential ID")

        verified, new_sign_count = await self._verify_authenticator_assertion(
            challenge, credential
        )

        if not verified:
            challenge.status = ChallengeStatus.FAILED
            raise ValueError("Credential verification failed")

        challenge.status = ChallengeStatus.VERIFIED

        now = utc_now()
        verification_key = self._make_verification_key(
            challenge.session_id, challenge.action
        )

        verification = VerificationRecord(
            session_id=challenge.session_id,
            action=challenge.action,
            verified_at=now,
            expires_at=now + timedelta(seconds=self._verification_validity),
            credential_id=credential_id,
        )

        self._verifications[verification_key] = verification

        logger.info(
            f"WebAuthn verification successful for session {challenge.session_id}, "
            f"action: {challenge.action}"
        )

        return {
            "verified": True,
            "challenge_id": challenge_id,
            "session_id": challenge.session_id,
            "action": challenge.action,
            "verified_at": now.isoformat(),
            "expires_in_seconds": self._verification_validity,
        }

    async def is_action_verified(
        self,
        session_id: str,
        action: str,
    ) -> bool:
        """
        Check if a session:action pair has a valid passkey verification.

        Args:
            session_id: The AWI session ID.
            action: The action being attempted.

        Returns:
            True if the action is currently verified and not expired.
        """
        verification_key = self._make_verification_key(session_id, action)
        verification = self._verifications.get(verification_key)

        if not verification:
            return False

        if utc_now() > verification.expires_at:
            del self._verifications[verification_key]
            return False

        return True

    async def get_verification_status(
        self,
        session_id: str,
        action: str,
    ) -> dict[str, Any]:
        """
        Get detailed verification status for a session:action pair.

        Args:
            session_id: The AWI session ID.
            action: The action to check.

        Returns:
            Dict with verification status, timestamps, and expiry.
        """
        verification_key = self._make_verification_key(session_id, action)
        verification = self._verifications.get(verification_key)

        if not verification:
            return {
                "session_id": session_id,
                "action": action,
                "is_verified": False,
                "verified_at": None,
                "expires_in_seconds": None,
            }

        now = utc_now()
        remaining = (verification.expires_at - now).total_seconds()

        if remaining <= 0:
            del self._verifications[verification_key]
            return {
                "session_id": session_id,
                "action": action,
                "is_verified": False,
                "verified_at": None,
                "expires_in_seconds": None,
            }

        return {
            "session_id": session_id,
            "action": action,
            "is_verified": True,
            "verified_at": verification.verified_at.isoformat(),
            "expires_in_seconds": int(remaining),
        }

    async def invalidate_verification(
        self,
        session_id: str,
        action: Optional[str] = None,
    ) -> int:
        """
        Invalidate passkey verifications for a session.

        Args:
            session_id: The AWI session ID.
            action: Optional specific action to invalidate. If None, invalidates all.

        Returns:
            Number of verifications invalidated.
        """
        if action:
            verification_key = self._make_verification_key(session_id, action)
            if verification_key in self._verifications:
                del self._verifications[verification_key]
                return 1
            return 0

        keys_to_remove = [
            key for key in self._verifications if key.startswith(f"{session_id}:")
        ]

        for key in keys_to_remove:
            del self._verifications[key]

        return len(keys_to_remove)

    def get_high_risk_actions(self) -> list[str]:
        """Get list of actions that require passkey verification."""
        return sorted(self.HIGH_RISK_ACTIONS)

    def get_challenge(self, challenge_id: str) -> WebAuthnChallenge | None:
        """Get a stored challenge for route-level ownership checks."""
        return self._challenges.get(challenge_id)

    async def register_credential(
        self,
        user_id: str,
        credential_id: str,
        public_key: bytes,
        sign_count: int = 0,
        name: str = "",
    ) -> bool:
        """
        Register a new WebAuthn credential for a user.

        This stores the credential's public key for later verification.
        In production, credentials would be stored in a persistent database.

        Args:
            user_id: The user identifier.
            credential_id: The credential ID (from registration response).
            public_key: The credential's public key bytes.
            sign_count: Initial sign count (usually 0).
            name: Optional human-readable name for the credential.

        Returns:
            True if registered successfully.
        """
        self._credentials[credential_id] = CredentialRecord(
            credential_id=credential_id,
            user_id=user_id,
            public_key=public_key,
            sign_count=sign_count,
            name=name,
        )
        logger.info(f"Registered credential {credential_id[:20]}... for user {user_id}")
        return True

    def get_registered_credentials(
        self, user_id: str | None = None
    ) -> list[dict[str, Any]]:
        """
        Get list of registered credentials.

        Args:
            user_id: Optional filter by user ID.

        Returns:
            List of credential info dicts.
        """
        creds = []
        for cred_id, cred in self._credentials.items():
            if user_id is None or cred.user_id == user_id:
                creds.append(
                    {
                        "credential_id": cred_id,
                        "user_id": cred.user_id,
                        "name": cred.name,
                        "created_at": cred.created_at.isoformat(),
                    }
                )
        return creds

    def cleanup_expired(self) -> dict[str, int]:
        """
        Remove expired challenges and verifications.

        Returns:
            Dict with counts of removed items.
        """
        now = utc_now()

        expired_challenges = [
            cid for cid, c in self._challenges.items() if now > c.expires_at
        ]
        for cid in expired_challenges:
            del self._challenges[cid]

        expired_verifications = [
            key for key, v in self._verifications.items() if now > v.expires_at
        ]
        for key in expired_verifications:
            del self._verifications[key]

        return {
            "challenges_removed": len(expired_challenges),
            "verifications_removed": len(expired_verifications),
        }

    def _make_verification_key(self, session_id: str, action: str) -> str:
        """Generate a unique key for storing verifications."""
        return f"{session_id}:{action}"

    async def _verify_authenticator_assertion(
        self,
        challenge: WebAuthnChallenge,
        credential: dict[str, Any],
    ) -> tuple[bool, int]:
        """
        Verify the authenticator assertion using py_webauthn.

        Performs real cryptographic verification:
        - The challenge matches
        - The RP ID hash matches
        - The signature is valid for the public key
        - The authenticator data counter hasn't been used before (prevents cloned credentials)

        Args:
            challenge: The original challenge.
            credential: The credential response from the client.

        Returns:
            Tuple of (success: bool, new_sign_count: int).
        """
        if not PY_WEBAUTHN_AVAILABLE:
            if _mock_verification_allowed():
                logger.warning(
                    "py_webauthn not available; WEBAUTHN_ALLOW_MOCK is set - "
                    "using MOCK verification (test-only path)"
                )
                return True, 0
            logger.error(
                "py_webauthn not available and WEBAUTHN_ALLOW_MOCK is not set - "
                "failing closed: WebAuthn verification cannot succeed without "
                "the real cryptographic library"
            )
            return False, 0

        credential_id = credential.get("id", "")
        stored_credential = self._credentials.get(credential_id)

        if not stored_credential:
            logger.warning(f"Credential {credential_id[:20]}... not registered")
            return False, 0

        # Get the expected origin from allowed_origins
        expected_origin = (
            self._allowed_origins[0]
            if self._allowed_origins
            else f"https://{challenge.rp_id}"
        )

        try:
            # Validate the credential ID is well-formed base64 before verifying;
            # verify_authentication_response takes the raw dict, not this value.
            parse_credential_id(credential_id)

            # Verify using py_webauthn
            result = verify_authentication_response(
                credential=credential,
                expected_challenge=challenge.challenge_bytes,
                expected_rp_id=challenge.rp_id,
                expected_origin=expected_origin,
                credential_public_key=stored_credential.public_key,
                credential_current_sign_count=stored_credential.sign_count,
                # Every challenge guards a HIGH_RISK_ACTIONS entry. Without the
                # UV flag an assertion proves only that someone touched the
                # authenticator, not that its owner approved the action, so
                # py_webauthn must reject an assertion whose UV flag is unset.
                require_user_verification=True,
            )

            # Update stored sign count to prevent cloned credential attacks
            new_sign_count = result.new_sign_count
            stored_credential.sign_count = new_sign_count

            logger.info(
                f"WebAuthn verification successful for credential {credential_id[:20]}..."
            )
            return True, new_sign_count

        except Exception as e:
            logger.warning(f"WebAuthn verification failed: {e}")
            return False, 0


_webauthn_provider: Optional[WebAuthnProvider] = None


def get_webauthn_provider() -> WebAuthnProvider:
    """Get or create the WebAuthnProvider singleton."""
    global _webauthn_provider
    if _webauthn_provider is None:
        from ..core.config import get_settings

        settings = get_settings()

        _webauthn_provider = WebAuthnProvider(
            rp_id=settings.WEBAUTHN_RP_ID,
            rp_name=settings.WEBAUTHN_RP_NAME,
            timeout_ms=settings.WEBAUTHN_TIMEOUT_MS,
            challenge_expiry_seconds=settings.WEBAUTHN_CHALLENGE_EXPIRY,
            verification_validity_seconds=settings.WEBAUTHN_VERIFICATION_VALIDITY,
        )

    return _webauthn_provider
