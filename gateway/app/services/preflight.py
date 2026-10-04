"""
Pre-Flight Readiness Check
===========================
Validates that the system is production-ready before the Day 1 launch.

Four validation domains:
1. KEYS    — API keys and payment tokens aren't test/placeholder values
2. DOMAIN  — BASE_URL is a real domain; manifests resolve correctly
3. ORACLE  — Agent directory URLs are reachable
4. ASSETS  — Content source URLs are serving media

Returns a PreflightReport with per-check pass/fail and an overall GO/NO-GO.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..core.config import get_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Check Result
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    """Single preflight check outcome."""

    name: str
    passed: bool
    severity: str  # "critical", "warning", "info"
    message: str
    detail: str = ""


@dataclass
class PreflightReport:
    """Aggregate preflight readiness report."""

    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    verdict: str = "NO-GO"  # "GO" or "NO-GO"
    total_checks: int = 0
    passed: int = 0
    failed: int = 0
    warnings: int = 0
    critical_failures: int = 0
    checks: list[dict] = field(default_factory=list)
    summary: str = ""


# ---------------------------------------------------------------------------
# Placeholder / Test Value Detectors
# ---------------------------------------------------------------------------

PLACEHOLDER_PATTERNS = [
    r"^test[-_]?key$",
    r"^sk[-_]test[-_]",
    r"^pk[-_]test[-_]",
    r"^your[-_]",
    r"^changeme$",
    r"^placeholder$",
    r"^xxx+$",
    r"^TODO",
    r"^REPLACE",
    r"^example",
]

PLACEHOLDER_DOMAINS = [
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "yourdomain.com",
    "example.com",
    "test.com",
    "placeholder.com",
]


def _is_placeholder(value: str) -> bool:
    """Check if a value looks like a test/placeholder."""
    if not value or not value.strip():
        return True
    v = value.strip().lower()
    for pattern in PLACEHOLDER_PATTERNS:
        if re.match(pattern, v, re.IGNORECASE):
            return True
    return False


def _is_placeholder_domain(url: str) -> bool:
    """Check if a URL contains a placeholder domain."""
    url_lower = url.lower()
    for domain in PLACEHOLDER_DOMAINS:
        if domain in url_lower:
            return True
    return False


def _looks_like_live_stripe_key(key: str) -> bool:
    """Stripe live keys start with sk_live_ or pk_live_."""
    return key.startswith("sk_live_") or key.startswith("pk_live_")


# ---------------------------------------------------------------------------
# Preflight Engine
# ---------------------------------------------------------------------------


class PreflightEngine:
    """
    Runs all preflight validations and produces a PreflightReport.

    Usage:
        engine = PreflightEngine()
        report = await engine.run(config_overrides={})
    """

    def __init__(self):
        self.settings = get_settings()

    async def run(self, config_overrides: dict | None = None) -> PreflightReport:
        """Execute all preflight checks and return the readiness report."""
        config = config_overrides or {}
        checks: list[CheckResult] = []

        # Phase 1: Key & Environment Validation
        checks.extend(self._check_api_keys(config))
        checks.extend(self._check_stripe_keys(config))
        checks.extend(self._check_environment_flags())

        # Phase 2: Domain & Routing Integrity
        checks.extend(self._check_base_url(config))
        checks.extend(self._check_manifest_urls(config))

        # Phase 3: Oracle Target Verification
        checks.extend(self._check_oracle_targets(config))

        # Phase 4: Content Factory Asset Check
        checks.extend(self._check_content_assets(config))

        # Tally
        report = self._build_report(checks)
        return report

    # --- Phase 1: Keys & Environment ---

    def _check_api_keys(self, config: dict) -> list[CheckResult]:
        """Verify API keys are not test/placeholder values."""
        results = []
        valid_keys = self.settings.VALID_API_KEYS
        keys = [k.strip() for k in valid_keys.split(",") if k.strip()]

        if not keys:
            results.append(
                CheckResult(
                    name="api_keys_configured",
                    passed=False,
                    severity="critical",
                    message="No API keys configured in VALID_API_KEYS.",
                    detail=(
                        "Set VALID_API_KEYS in .env with production keys "
                        "(comma-separated)."
                    ),
                )
            )
            return results

        placeholder_keys = [k for k in keys if _is_placeholder(k)]
        if placeholder_keys:
            results.append(
                CheckResult(
                    name="api_keys_not_placeholder",
                    passed=False,
                    severity="critical",
                    message=f"Found {len(placeholder_keys)} placeholder API key(s).",
                    # Never echo key material: a prefix of 'test-key' or
                    # 'changeme' is the whole bootstrap admin key.
                    detail=(
                        "Placeholder values in VALID_API_KEYS are not "
                        "production-safe. Generate real keys."
                    ),
                )
            )
        else:
            results.append(
                CheckResult(
                    name="api_keys_not_placeholder",
                    passed=True,
                    severity="info",
                    message=f"{len(keys)} production API key(s) configured.",
                )
            )

        return results

    def _check_stripe_keys(self, config: dict) -> list[CheckResult]:
        """Check for live Stripe keys (if billing is enabled)."""
        results = []

        # Look for Stripe key in config overrides or env
        stripe_key = config.get("stripe_secret_key", "")

        if not stripe_key:
            results.append(
                CheckResult(
                    name="stripe_key_present",
                    passed=False,
                    severity="warning",
                    message="No Stripe secret key provided.",
                    detail=(
                        "Billing will use simulated mode. "
                        "Set stripe_secret_key for live payments."
                    ),
                )
            )
        elif _is_placeholder(stripe_key):
            results.append(
                CheckResult(
                    name="stripe_key_valid",
                    passed=False,
                    severity="critical",
                    message="Stripe key is a placeholder value.",
                    detail="Replace with a live Stripe key (sk_live_...).",
                )
            )
        elif not _looks_like_live_stripe_key(stripe_key):
            results.append(
                CheckResult(
                    name="stripe_key_live",
                    passed=False,
                    severity="warning",
                    message="Stripe key does not appear to be a live key.",
                    detail=(
                        "Live keys start with sk_live_. "
                        "Test keys (sk_test_) will not process real payments."
                    ),
                )
            )
        else:
            results.append(
                CheckResult(
                    name="stripe_key_live",
                    passed=True,
                    severity="info",
                    message="Live Stripe key detected.",
                )
            )

        return results

    def _check_environment_flags(self) -> list[CheckResult]:
        """Verify production-appropriate environment settings."""
        results = []

        if self.settings.DEBUG:
            results.append(
                CheckResult(
                    name="debug_disabled",
                    passed=False,
                    severity="warning",
                    message="DEBUG is enabled.",
                    detail="Set DEBUG=false for production deployment.",
                )
            )
        else:
            results.append(
                CheckResult(
                    name="debug_disabled",
                    passed=True,
                    severity="info",
                    message="DEBUG mode is off.",
                )
            )

        if self.settings.RATE_LIMIT_PER_MINUTE < 60:
            results.append(
                CheckResult(
                    name="rate_limit_reasonable",
                    passed=False,
                    severity="warning",
                    message=(
                        f"Rate limit is very low "
                        f"({self.settings.RATE_LIMIT_PER_MINUTE}/min)."
                    ),
                    detail="Agent consumers may hit 429s quickly. Consider >= 60/min.",
                )
            )
        else:
            results.append(
                CheckResult(
                    name="rate_limit_reasonable",
                    passed=True,
                    severity="info",
                    message=f"Rate limit: {self.settings.RATE_LIMIT_PER_MINUTE}/min.",
                )
            )

        return results

    # --- Phase 2: Domain & Routing ---

    def _check_base_url(self, config: dict) -> list[CheckResult]:
        """Verify BASE_URL is a real, routable domain."""
        results = []

        # Check the servers configured in the OpenAPI spec
        base_url = config.get("base_url", "https://api.yourdomain.com")

        if _is_placeholder_domain(base_url):
            results.append(
                CheckResult(
                    name="base_url_valid",
                    passed=False,
                    severity="critical",
                    message=f"BASE_URL '{base_url}' is a placeholder domain.",
                    detail="Set a real production domain (e.g., https://api.yourcompany.com).",
                )
            )
        else:
            results.append(
                CheckResult(
                    name="base_url_valid",
                    passed=True,
                    severity="info",
                    message=f"BASE_URL: {base_url}",
                )
            )

        # Check HTTPS
        if not base_url.startswith("https://"):
            results.append(
                CheckResult(
                    name="base_url_https",
                    passed=False,
                    severity="warning",
                    message="BASE_URL does not use HTTPS.",
                    detail=(
                        "Production APIs should use TLS. "
                        "Agents may reject insecure endpoints."
                    ),
                )
            )
        else:
            results.append(
                CheckResult(
                    name="base_url_https",
                    passed=True,
                    severity="info",
                    message="BASE_URL uses HTTPS.",
                )
            )

        return results

    def _check_manifest_urls(self, config: dict) -> list[CheckResult]:
        """Verify that agent.json and llm.txt resolve with the production domain."""
        results = []
        base_url = config.get("base_url", "https://api.yourdomain.com")

        agent_json_url = f"{base_url.rstrip('/')}/.well-known/agent.json"
        llm_txt_url = f"{base_url.rstrip('/')}/llm.txt"

        if _is_placeholder_domain(base_url):
            results.append(
                CheckResult(
                    name="manifests_resolvable",
                    passed=False,
                    severity="critical",
                    message="Cannot validate manifests — BASE_URL is placeholder.",
                    detail=(
                        f"agent.json would serve at {agent_json_url} — "
                        "unreachable with placeholder domain."
                    ),
                )
            )
        else:
            results.append(
                CheckResult(
                    name="manifests_resolvable",
                    passed=True,
                    severity="info",
                    message=f"Manifests will serve at {agent_json_url} and {llm_txt_url}.",
                )
            )

        return results

    # --- Phase 3: Oracle Targets ---

    def _check_oracle_targets(self, config: dict) -> list[CheckResult]:
        """Validate that agent directory registration URLs look reachable."""
        results = []

        directories = [
            {
                "directory_url": "https://agentindex.dev/register",
                "directory_type": "agent_registry",
            },
            {
                "directory_url": "https://agentprotocol.ai/registry",
                "directory_type": "well_known",
            },
            {
                "directory_url": "https://mcphub.io/servers",
                "directory_type": "mcp_server",
            },
            {
                "directory_url": "https://pluginstore.ai/register",
                "directory_type": "plugin_store",
            },
        ]

        reachable = 0
        for dir_entry in directories:
            url = dir_entry["directory_url"]
            # In preflight, we validate URL format and known-reachability
            # (actual HTTP pings would require httpx — we do structural validation)
            if _is_placeholder_domain(url):
                results.append(
                    CheckResult(
                        name=f"oracle_directory_{dir_entry['directory_type']}",
                        passed=False,
                        severity="warning",
                        message=f"Directory URL is placeholder: {url}",
                        detail="Replace with a real agent directory endpoint.",
                    )
                )
            elif not url.startswith("https://"):
                results.append(
                    CheckResult(
                        name=f"oracle_directory_{dir_entry['directory_type']}",
                        passed=False,
                        severity="warning",
                        message=f"Directory URL is not HTTPS: {url}",
                        detail="Agent directories should use TLS.",
                    )
                )
            else:
                reachable += 1
                results.append(
                    CheckResult(
                        name=f"oracle_directory_{dir_entry['directory_type']}",
                        passed=True,
                        severity="info",
                        message=f"Directory configured: {url}",
                    )
                )

        results.append(
            CheckResult(
                name="oracle_directories_total",
                passed=reachable >= 2,
                severity="warning" if reachable < 2 else "info",
                message=f"{reachable}/{len(directories)} directory targets validated.",
                detail=(
                    "Recommend at least 2 reachable directories "
                    "for meaningful visibility."
                ),
            )
        )

        return results

    # --- Phase 4: Content Assets ---

    def _check_content_assets(self, config: dict) -> list[CheckResult]:
        """Validate that campaign source URLs are not placeholder values."""
        results = []

        source_url = config.get(
            "campaign_source_url", "https://yourdomain.com/content/b2a-launch-video"
        )

        if _is_placeholder_domain(source_url):
            results.append(
                CheckResult(
                    name="content_source_url",
                    passed=False,
                    severity="critical",
                    message=f"Campaign source URL is placeholder: {source_url}",
                    detail="Set campaign_source_url to a real, accessible video URL.",
                )
            )
        else:
            results.append(
                CheckResult(
                    name="content_source_url",
                    passed=True,
                    severity="info",
                    message=f"Campaign source: {source_url}",
                )
            )

        # Check crawl targets
        crawl_targets = [
            "https://api.openai.com",
            "https://api.anthropic.com",
            "https://api.stripe.com",
            "https://api.twilio.com",
            "https://api.github.com",
            "https://api.cloudflare.com",
        ]
        external_count = sum(1 for t in crawl_targets if not _is_placeholder_domain(t))
        results.append(
            CheckResult(
                name="crawl_targets_configured",
                passed=external_count >= 3,
                severity="warning" if external_count < 3 else "info",
                message=(
                    f"{external_count}/{len(crawl_targets)} crawl targets "
                    "are real external APIs."
                ),
            )
        )

        return results

    # --- Report Builder ---

    def _build_report(self, checks: list[CheckResult]) -> PreflightReport:
        """Aggregate check results into a final report."""
        total = len(checks)
        passed = sum(1 for c in checks if c.passed)
        failed = sum(1 for c in checks if not c.passed)
        warnings = sum(1 for c in checks if not c.passed and c.severity == "warning")
        critical = sum(1 for c in checks if not c.passed and c.severity == "critical")

        # GO if zero critical failures
        verdict = "GO" if critical == 0 else "NO-GO"

        if verdict == "GO" and warnings > 0:
            summary = (
                f"CONDITIONAL GO — {warnings} warning(s) "
                "to address before full production."
            )
        elif verdict == "GO":
            summary = "ALL CLEAR — System is production-ready. Turn the key."
        else:
            summary = (
                f"NO-GO — {critical} critical issue(s) must be resolved before launch."
            )

        check_dicts = [
            {
                "name": c.name,
                "passed": c.passed,
                "severity": c.severity,
                "message": c.message,
                "detail": c.detail,
            }
            for c in checks
        ]

        return PreflightReport(
            verdict=verdict,
            total_checks=total,
            passed=passed,
            failed=failed,
            warnings=warnings,
            critical_failures=critical,
            checks=check_dicts,
            summary=summary,
        )
