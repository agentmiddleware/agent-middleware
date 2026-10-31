"""Pin the claims in gateway/static against the gateway that serves them.

Agents read static/llm.txt and static/dashboard.html before they call the
API. These checks fail when those files describe a control the code does
not implement.
"""

from pathlib import Path

import pytest

_STATIC = Path(__file__).resolve().parents[1] / "static"


def _llm() -> str:
    return (_STATIC / "llm.txt").read_text(encoding="utf-8")


def _dashboard() -> str:
    return (_STATIC / "dashboard.html").read_text(encoding="utf-8")


def test_llm_txt_does_not_freeze_a_wallet_on_the_first_cap_crossing():
    """Keep velocity assessments distinct from the hard daily billing cap."""
    text = _llm()
    folded = " ".join(text.split())

    assert "Wallets are auto-frozen if spending exceeds" not in text
    assert "billing also enforces it as a hard cap" in folded
    assert "`daily_limit` is zero" in folded
    assert "strictly over a velocity threshold" in folded
    assert "records proposed spend before checking" in folded
    assert "prior alert count before adding the new alert" in folded
    assert "VELOCITY_FREEZE_THRESHOLD" in text
    assert "default 3" in folded
    assert "fourth over-limit assessment" in folded
    assert "not necessarily the fourth paid charge" in folded
    assert "another billing check can still deny" in folded
    assert "freeze assessment is not debited" in folded
    assert "alert count does not" in folded
    assert "not changed to frozen" in folded
    assert "governed database transaction rolls back" in folded
    assert "uncommitted velocity and billing mutations roll back with it" in folded
    assert "governed refusal can still commit an alert" in folded
    assert "post-dispatch `delivery_uncertain` outcome retains the debit" in folded
    assert "Failed governed transactions roll back" not in text


def test_llm_txt_production_redis_outage_is_503_not_the_memory_limiter():
    """Production-like boots with REDIS_URL fail closed in RateLimitMiddleware."""
    text = _llm()
    folded = " ".join(text.split())

    assert "rate_limiter_unavailable" in text
    assert "HTTP 503" in text
    assert "does not use that in-memory limiter" in folded
    assert "Requests subject to rate limiting" in text
    assert "exempt public documentation and health routes" in folded


def test_llm_txt_mcp_messages_governed_errors_are_http_200_jsonrpc():
    """POST /mcp/messages reports these failures in the JSON-RPC body.

    Auth and the rate limiter still use HTTP status. A governed tools/call
    failure does not. idempotency_key_reused shares code -32603 with
    internal_error, so the message is the signal.
    """
    text = _llm()
    folded = " ".join(text.split())

    assert "idempotency_key_reused" in text
    assert "insufficient_funds" in text
    assert "-32004" in text
    assert "including insufficient credits or a spending cap" in folded
    assert "wallet_frozen" in text
    assert "idempotency_in_progress" in text
    assert "delivery_uncertain" in text
    assert "-32005" in text
    assert "internal_error" in text
    assert "HTTP 200" in text
    assert "Do not branch on HTTP 402 or HTTP 409 for that route" in text
    assert "does not report either idempotency condition as HTTP 409" in folded
    assert "do not treat it as an in-progress retry" in folded
    assert "shared per-client pre-authentication" in folded
    assert "including unauthenticated requests and rejected credentials" in folded
    assert "public MCP client/global bucket" in folded
    assert "These codes do not describe every governed failure" in text
    assert "or an HTTP route denied" in text
    assert (
        "Checks that run before the handler use HTTP status on every route" not in text
    )
    assert "| 402 | Insufficient credits |" not in text


def test_dashboard_catalog_auth_depends_on_the_boot_mode():
    """GET /mcp/tools.json is anonymous only on a local-compatible boot."""
    page = " ".join(_dashboard().split())

    assert "Tool catalogs and invoke require an operator-issued key." not in page
    assert "production-like boot" in page
    assert "local-compatible boot may leave the catalog anonymous" in page
    assert "Governed invocation still requires authentication" in page
    assert "API key or bearer credential" in page
    assert "read-only public MCP endpoint" in page
    assert "tool catalog requires authentication on production-like boots" in page
    assert "local-compatible boots allow anonymous catalog reads" in page
    assert "Tool lists on this origin require a key" not in page


def test_dashboard_me_routes_stay_inside_the_calling_key():
    """/v1/me refuses a bootstrap admin key and does not take a wallet id."""
    page = _dashboard()
    folded = " ".join(page.split())

    assert "wallet_key_required" in page
    assert "HTTP 403" in page
    assert "bootstrap admin key" in page
    assert "do not accept another wallet id" in folded
    assert "Permits and audit events are limited to this key." in page
    assert "Receipts are wallet-wide, returned in pages" in folded
    assert "optional permit, tool, and outcome filters" in folded


@pytest.mark.anyio
async def test_served_static_docs_keep_the_corrected_claims():
    """The routes substitute PUBLIC_URL and must still carry the claims."""
    from app.routers.static import get_human_dashboard, get_llm_txt

    llm = (await get_llm_txt()).body.decode()
    page = (await get_human_dashboard()).body.decode()

    assert "{{PUBLIC_URL}}" in _llm()
    assert "{{PUBLIC_URL}}" not in llm
    assert "first crossing does not itself freeze the wallet" in " ".join(llm.split())
    assert "rate_limiter_unavailable" in llm
    assert "idempotency_key_reused" in llm
    assert "wallet_key_required" in page
    assert "local-compatible boot may leave the catalog anonymous" in page
