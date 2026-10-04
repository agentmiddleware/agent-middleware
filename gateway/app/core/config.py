"""
Central configuration for Agent Middleware API.
All settings are loaded from environment variables for zero-GUI deployment.
"""

from decimal import Decimal
from enum import Enum
from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class DuplicateGuardMode(str, Enum):
    """Cross-key duplicate detection enforcement mode."""

    OFF = "off"
    LOG = "log"
    ENFORCE = "enforce"


class Settings(BaseSettings):
    """
    Application settings. All values sourced from environment variables.
    Agents authenticate via API keys passed in the X-API-Key header.
    """

    # --- Application ---
    APP_NAME: str = "Agent Middleware API"
    APP_VERSION: str = "1.3.0"
    # Public build provenance. Railway's RAILWAY_GIT_COMMIT_SHA is also
    # recognized by app.core.build_metadata when this explicit value is empty.
    BUILD_COMMIT_SHA: str = ""
    ENVIRONMENT: str = "local"
    DEBUG: bool = False

    # --- Server ---
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    CORS_ORIGINS: str = "*"

    # --- Public URL ---
    # Used for agent manifests and documentation links
    # Set to the deployment's absolute public origin. Keep empty for local
    # instances so receipts and manifests never claim the hosted issuer.
    PUBLIC_URL: str = ""

    # --- Public operator identity ---
    # OpenAPI contact metadata is omitted until real, monitored values are set.
    # Never ship placeholder names or addresses on a public deployment.
    PUBLIC_CONTACT_NAME: str = ""
    PUBLIC_CONTACT_EMAIL: str = ""
    PUBLIC_CONTACT_URL: str = ""

    # --- Durable Runtime State ---
    # Backends: auto, postgres, redis, sqlite, memory
    STATE_BACKEND: str = "auto"
    STATE_NAMESPACE: str = "agent_middleware"
    DATABASE_URL: str = ""
    REDIS_URL: str = ""
    SQLITE_URL: str = ""

    # --- Database Pool Settings ---
    # Used for SQLModel/SQLAlchemy async sessions
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10

    # --- Authentication ---
    API_KEY_HEADER: str = "X-API-Key"
    # Comma-separated list of valid API keys (use a secrets manager in production)
    VALID_API_KEYS: str = ""
    # Comma-separated static development/training keys for local testing.
    # Deliberately exempt from the rotation runbook (docs/api-key-rotation.md)
    # so recorded trainings, notebooks, and local fixtures keep working.
    # Honored only in local-compatible environments; production-like
    # environments refuse to boot when this is set (see
    # app.core.trust_mode.validate_trust_mode_guardrails). Entries must carry
    # the amw_dev_ prefix — a rotated amw_live_ key pasted here never
    # authenticates. See docs/static-dev-api-keys.md.
    STATIC_DEV_API_KEYS: str = ""
    # Self-serve dev key provisioning (POST /v1/dev-keys/self-provision):
    # lets an agent against a local instance mint its own wallet-scoped key
    # with no pre-shared secret. Default off (route answers 404). Local-only:
    # production-like environments refuse to boot when this is true, and the
    # handler independently fails closed there. See docs/static-dev-api-keys.md.
    ENABLE_DEV_KEY_SELF_PROVISION: bool = False

    # --- Enterprise IGA bridge (OIDC -> PolicyBundle) ---
    # JSON object mapping a trusted enterprise OIDC issuer URL to its pinned
    # verification material, e.g.
    #   {"https://example.okta.com/oauth2/default": {
    #      "audience": "api://agent-middleware",
    #      "algorithms": ["RS256"],
    #      "provider": "okta",            # optional; inferred from the host
    #      "jwks": {"keys": [...]}}}      # or "public_key_pem": "-----BEGIN..."
    # Keys are pinned here on purpose: there is NO network JWKS fetch, so a
    # compromised IdP hostname cannot rotate keys under us — rotation is an
    # explicit config change. Parsed lazily and fail-closed by
    # app.core.oidc_iga (malformed JSON raises at use time, never at import).
    # Empty string (the default) disables the IGA layer entirely: no bearer
    # token is ever treated as an enterprise token and every existing auth
    # path is untouched.
    IGA_TRUSTED_ISSUERS: str = ""
    # JSON object mapping an enterprise group/role name to a PolicyBundle
    # grant with optional runtime caps, e.g.
    #   {"payments-ops": {"policy_id": "polb-...", "max_uses": 100,
    #     "velocity_window_seconds": 60, "velocity_max_calls": 10}}
    # Caps are nullable (null = uncapped) and enforced by in-process,
    # per-instance counters in app.core.oidc_iga — PolicyBundle rows carry no
    # such columns by design. Empty string (the default) grants nothing:
    # every enterprise principal is blocked with iga_no_matching_role.
    IGA_GROUP_POLICY_MAP: str = ""

    # --- Proof surfaces ---
    # When false, only CORE_TRUST_ROUTERS (+ MCP) are mounted. Production-like
    # environments must set this false — see validate_trust_mode_guardrails.
    ENABLE_PROOF_SURFACES: bool = False

    # --- Dogfood tool (opt-in executable partner.notes.write) ---
    # When true, registers a safe local notes-write MCP tool so live
    # /mcp/tools.json is non-empty for permit→invoke→receipt dogfood.
    # Default false: no stub pollution. Independent of ENABLE_PROOF_SURFACES.
    ENABLE_DOGFOOD_TOOL: bool = False

    # --- Second dogfood tool (opt-in read-only partner.notes.count) ---
    # When true, registers a second harmless read-only tool for CI to test
    # out-of-scope denial (permit allows first tool, denies second).
    # Default false: only enable in test/CI environments.
    ENABLE_DOGFOOD_SECOND_TOOL: bool = False

    # --- Governed upstream MCP partner tool ---
    # One explicitly configured Streamable HTTP server/tool for the
    # design-partner pilot. The adapter fails closed at startup when enabled
    # with incomplete or unsafe configuration.
    MCP_UPSTREAM_ENABLED: bool = False
    MCP_UPSTREAM_URL: str = ""
    MCP_UPSTREAM_TOOL_NAME: str = ""
    MCP_UPSTREAM_PUBLIC_TOOL_ID: str = ""
    MCP_UPSTREAM_BEARER_TOKEN: SecretStr = SecretStr("")
    MCP_UPSTREAM_CREDITS_PER_CALL: Decimal = Decimal("0")
    MCP_UPSTREAM_CONNECT_TIMEOUT_SECONDS: float = 5.0
    MCP_UPSTREAM_CALL_TIMEOUT_SECONDS: float = 30.0
    MCP_UPSTREAM_MAX_RESPONSE_BYTES: int = 1_048_576

    # Cross-key duplicate detection for upstream tools. Modes:
    # - "off": disabled (new-key retries with identical request hashes are allowed)
    # - "log": detect and log duplicates but allow them (observe mode, default)
    # - "enforce": refuse duplicate requests with duplicate_request_new_key
    MCP_UPSTREAM_DUPLICATE_GUARD: DuplicateGuardMode = DuplicateGuardMode.LOG
    # Probabilistic advisory check; enabling it sends redacted state to TypeSafe.
    JEV_RISK_GUARD: DuplicateGuardMode = DuplicateGuardMode.OFF
    JEV_RISK_GUARD_MODEL: str = "jev-1.13.0"
    JEV_RISK_GUARD_TIMEOUT_SECONDS: float = 1.5
    JEV_RISK_GUARD_TIERS: str = "medium,high"
    TYPESAFE_API_KEY: SecretStr = SecretStr("")
    TYPESAFE_BASE_URL: str = "https://api.typesafe.ai"
    # Default repeat detection window in seconds. Duplicates outside this window
    # are not detected.
    MCP_UPSTREAM_DUPLICATE_WINDOW_SECONDS: int = 86400  # 24 hours
    # Enable only after all workers can verify and enforce repeat-window permits.
    # Disabling issuance never disables validation of already-issued permits.
    ENABLE_PERMIT_REPEAT_WINDOW_ISSUANCE: bool = False

    # --- Inbound request size ---
    # Ceiling on any request body the API will buffer, enforced for every
    # route by `app.middleware.request_body_limit`. Mirrors the 1 MiB bound
    # already placed on upstream MCP *responses* above: the gateway should not
    # accept more from a caller than it is willing to relay from a tool.
    # The opt-in MCP transports keep their own tighter caps (256 KiB public,
    # 64 KiB partner); this is the floor under everything, `/mcp/messages`
    # included. Raise it only for an operator who has a route that needs it.
    MAX_REQUEST_BODY_BYTES: int = 1_048_576

    # --- Phase 9: WebAuthn mock (tests/local only) ---
    # When true and py_webauthn is absent, verification can short-circuit.
    # Production-like environments must keep this false.
    WEBAUTHN_ALLOW_MOCK: bool = False

    # --- Trust Plane ---
    # Strict trust mode is the default and the only supported production
    # posture: every governed MCP invocation must present a signed permit,
    # legacy unpermitted calls are denied with `permit_required`, and a
    # signing private key is required.
    #
    # To run a local demo or a legacy integration without permits, set both
    # `TRUST_MODE_ENABLED=false` and `ALLOW_LEGACY_UNPERMITTED_MCP=true`
    # explicitly in the environment. The startup banner in
    # `app.core.trust_mode.warn_if_trust_mode_permissive` makes it loud when
    # this opt-out is in effect, and the
    # `app.core.trust_mode.validate_trust_mode_guardrails` check refuses to
    # boot a production-like environment under any permissive combination.
    TRUST_MODE_ENABLED: bool = True
    ALLOW_LEGACY_UNPERMITTED_MCP: bool = False
    TRUST_SIGNING_KEY_ID: str = "local-dev-ed25519"
    TRUST_SIGNING_PRIVATE_KEY_B64: str = ""

    # --- Standard MCP endpoint (POST /mcp) ---
    # Spec-compliant stateless Streamable HTTP surface for standard MCP
    # clients. tools/call mints a bounded single-tool permit from the
    # caller's wallet before entering the governed invoke path, so the
    # permit -> meter -> receipt -> audit loop is unchanged. Off by default:
    # do not advertise (or registry-publish) a transport that is not
    # deliberately enabled.
    ENABLE_STANDARD_MCP_ENDPOINT: bool = False
    STANDARD_MCP_PERMIT_TTL_SECONDS: int = 120

    # --- Public MCP endpoint (POST /mcp/public) ---
    # Unauthenticated, read-only Streamable HTTP surface for MCP clients
    # that cannot hold an API key (public plugin runtimes such as ChatGPT
    # apps). Serves only verification and discovery tools; nothing behind
    # it can create signing authority, mint permits, debit wallets, change
    # governed tool registration, or reach the governed invoke path. Off by
    # default. Production-like environments refuse to boot with this true
    # (and the handler 404s there even if the flag is set): a stranger must
    # not list governed tools. Receipt verification stays on
    # /.well-known/trust-keys.json.
    ENABLE_PUBLIC_MCP_ENDPOINT: bool = False

    # --- Stripe Payment Processing ---
    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    STRIPE_PUBLISHABLE_KEY: str = ""

    # --- KYC Verification ---
    # Require KYC verification before allowing fiat top-ups (default: false for dev)
    KYC_REQUIRED_FOR_TOPUP: bool = False

    # --- Credit Exchange Rate ---
    # Credits minted per $1.00 USD settled (1000 credits = $1.00, 1 credit =
    # $0.001). This is the single source of truth: Stripe settlement mints
    # credits at this rate and /v1/billing/pricing advertises it. Do not
    # re-declare it as a module constant — a second copy silently lets the
    # advertised rate drift from the rate real money converts at.
    EXCHANGE_RATE: Decimal = Decimal("1000.0")

    @field_validator("EXCHANGE_RATE")
    @classmethod
    def _validate_exchange_rate(cls, value: Decimal) -> Decimal:
        """Refuse a rate that cannot convert settled fiat into credits.

        Credit issuance multiplies settled fiat by this rate, so a zero or
        negative value converts a real payment into no credits (or negative
        ones). Fail at construction rather than at the first settlement.
        NaN and infinities need no check here: pydantic's Decimal parsing
        rejects them before this runs.
        """
        if value <= 0:
            raise ValueError("EXCHANGE_RATE must be a positive decimal")
        return value

    # --- Notification Service ---
    RESEND_API_KEY: str = ""
    SLACK_WEBHOOK_URL: str = ""
    # Sender for Resend email. Empty by default so no placeholder sender ships;
    # set it, to an address the operator controls, together with RESEND_API_KEY.
    ALERT_FROM_EMAIL: str = ""

    # --- Sentinel Human Approval (pauseapi.app) ---
    # Backs the per-permit requires_human_approval gate on governed invokes.
    # Real mode needs both URL and key; used only when
    # SIMULATION_MODE_HUMAN_APPROVAL=false.
    SENTINEL_API_URL: str = ""
    SENTINEL_API_KEY: str = ""
    # Forwarded to Sentinel as timeout_seconds (its magic-link expiry, 1..86400)
    # and enforced locally as the approval's expiry — Sentinel itself never
    # expires a pending approval.
    SENTINEL_APPROVAL_TIMEOUT_SECONDS: int = 300
    # >0: on first invoke, long-poll Sentinel this many seconds for an instant
    # decision before returning human_approval_pending (max 300).
    SENTINEL_WAIT_SECONDS: float = 0.0
    # Comma-separated approver list (email, mailto:, or sms:+E164). Empty defers
    # to the Sentinel tenant's default approvers.
    SENTINEL_APPROVERS: str = ""
    # Sentinel risk_level attached to approval requests: low|medium|high|critical.
    SENTINEL_RISK_LEVEL: str = "high"
    # --- Signed quotes ---
    # How long a signed price quote stays valid. A quote locks the price for
    # exactly one invoke inside this window; long enough for a human hop,
    # short enough to bound exposure to price drift. Clamped to 30..3600.
    QUOTE_TTL_SECONDS: int = 600

    # How long a human has to decide a permit REQUEST (agent asking for
    # authority) before it expires locally. Longer than the invoke gate's
    # window: the agent polls rather than holding an invoke open. Clamped to
    # Sentinel's 1..86400 bound.
    PERMIT_REQUEST_TIMEOUT_SECONDS: int = 3600

    # --- Velocity Monitoring ---
    VELOCITY_HOURLY_LIMIT: Decimal = Decimal("1000.0")
    VELOCITY_DAILY_LIMIT: Decimal = Decimal("10000.0")
    # Reserved: read into VelocityMonitor but used by no check today (there is
    # no standard-deviation detection). Changing it has no effect.
    VELOCITY_ALERT_THRESHOLD: int = 2
    VELOCITY_FREEZE_THRESHOLD: int = 3

    # --- IoT Protocol Bridge ---
    MQTT_BROKER_URL: str = "mqtt://localhost:1883"
    MQTT_DEFAULT_QOS: int = 1
    # Enforce topic-level ACLs to prevent the DJI Romo-style breach pattern
    MQTT_ENFORCE_TOPIC_ACL: bool = True

    # --- Telemetry / Autonomous PM ---
    TELEMETRY_RETENTION_HOURS: int = 168  # 7 days
    AUTO_PR_ENABLED: bool = False
    GIT_REMOTE_URL: str = ""
    GIT_BRANCH_PREFIX: str = "auto-pm/"

    # --- Media Engine ---
    MAX_UPLOAD_SIZE_MB: int = 500
    SUPPORTED_VIDEO_FORMATS: str = "mp4,mov,webm,mkv"
    CAPTION_LANGUAGE: str = "en"

    # --- Rate Limiting ---
    # Authenticated routes use the API key. The unauthenticated public MCP
    # route uses a canonical client IP plus a deployment-wide backstop.
    RATE_LIMIT_PER_MINUTE: int = 120

    # --- LLM / AI Agent Intelligence ---
    # Provider: openai, azure, anthropic, ollama
    LLM_PROVIDER: str = "openai"
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "gpt-4o"
    LLM_BASE_URL: str = "https://api.openai.com/v1"
    LLM_MAX_TOKENS: int = 4096
    LLM_TEMPERATURE: float = 0.7
    # Azure OpenAI specific
    AZURE_OPENAI_ENDPOINT: str = ""
    AZURE_OPENAI_DEPLOYMENT: str = ""
    # Ollama (local) specific
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "llama3"

    # --- Phase 9: WebAuthn / Passkey ---
    # Relying Party configuration for FIDO2/WebAuthn
    WEBAUTHN_RP_ID: str = "localhost"
    WEBAUTHN_RP_NAME: str = "Agent Middleware API"
    WEBAUTHN_TIMEOUT_MS: int = 60000
    WEBAUTHN_CHALLENGE_EXPIRY: int = 300
    WEBAUTHN_VERIFICATION_VALIDITY: int = 300
    WEBAUTHN_ALLOWED_ORIGINS: str = (
        "https://localhost,http://localhost:8000"  # Comma-separated
    )

    # --- Phase 9: Playwright Bridge ---
    PLAYWRIGHT_HEADLESS: bool = True
    PLAYWRIGHT_BROWSER_TYPE: str = "chromium"
    PLAYWRIGHT_TIMEOUT_MS: int = 30000
    PLAYWRIGHT_MAX_SESSIONS: int = 8
    PLAYWRIGHT_SESSION_TTL_SECONDS: int = 900

    # --- Behavioral Sandbox ---
    # Python execution backend: disabled, docker, or unsafe_host. Docker is the
    # only built-in backend intended to provide an actual process boundary.
    BEHAVIORAL_SANDBOX_PYTHON_BACKEND: str = "disabled"
    BEHAVIORAL_SANDBOX_DOCKER_IMAGE: str = "python:3.12-slim"

    # Legacy local-development escape hatch. Host Python execution is not a
    # production sandbox; keep this false unless an external isolation layer
    # such as a container runtime, gVisor, or Firecracker owns the boundary.
    ALLOW_UNSAFE_HOST_PYTHON_SANDBOX: bool = False

    # Local-development escape hatch for the outbound-URL guard
    # (app.core.url_guard): allows agent-supplied navigation/proxy targets to
    # reach loopback/RFC1918/link-local addresses (e.g. a mock server on
    # localhost). Never enable in production — it re-opens SSRF against
    # cloud metadata endpoints and internal services. Non-http(s) schemes
    # stay blocked regardless.
    ALLOW_PRIVATE_NETWORK_TARGETS: bool = False

    # Directory that AWI upload_file actions may read from. Empty (the
    # default) disables file uploads entirely; when set, requested paths are
    # resolved and must stay inside this directory, so an agent can only
    # upload files that were deliberately staged for it — not arbitrary
    # host files like credentials or keys.
    AWI_UPLOAD_DIR: str = ""

    # --- Phase 9: RAG Engine ---
    RAG_VECTOR_STORE_PATH: str = "./data/awi_vectors"
    RAG_EMBEDDING_MODEL: str = "text-embedding-3-small"
    RAG_EMBEDDING_DIMENSION: int = 1536

    # --- Simulation Mode ---
    # Per-service flag. True = use the frozen mock/synthetic implementation.
    # False is reserved for an explicitly approved real adapter and otherwise
    # raises NotImplementedError. Production-like deployments also disable the
    # proof-surface routers. See docs/PROOF_SURFACES.md.
    SIMULATION_MODE_ORACLE: bool = True
    SIMULATION_MODE_RED_TEAM: bool = True
    SIMULATION_MODE_RTAAS: bool = True
    SIMULATION_MODE_MEDIA_ENGINE: bool = True
    SIMULATION_MODE_IOT_BRIDGE: bool = True
    SIMULATION_MODE_TELEMETRY_PM: bool = True
    SIMULATION_MODE_AGENT_COMMS: bool = True
    SIMULATION_MODE_CONTENT_FACTORY: bool = True
    # Frozen proof-surface categories whose only registered tools are preview
    # stubs (app/services/mcp_phase9_tools.py returns a contract-only response
    # and wires no side effects). They sit in pricing.PROOF_SURFACE_CATEGORIES
    # and their routers are in PROOF_SURFACE_ROUTERS, so the runtime has to
    # report them as simulated: without a flag here a require_real_effects
    # wallet policy would let one of those stubs through as a real effect.
    SIMULATION_MODE_PROTOCOL_GEN: bool = True
    SIMULATION_MODE_SANDBOX: bool = True
    # Human-approval gate. Simulated approvals auto-approve (marked simulated)
    # in local/dev only; production-like environments fail closed instead of
    # honoring a simulated approval. False requires SENTINEL_API_URL + KEY.
    SIMULATION_MODE_HUMAN_APPROVAL: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )


@lru_cache()
def get_settings() -> Settings:
    return Settings()


def public_api_origin() -> str:
    """Absolute public API origin from PUBLIC_URL (empty when unset).

    Returns empty rather than guessing localhost: a manifest or an exported
    receipt that names the wrong issuer is worse than one that names none.
    """
    return (get_settings().PUBLIC_URL or "").strip().rstrip("/")
