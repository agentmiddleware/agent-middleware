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
    """VelocityMonitor freezes only after VELOCITY_FREEZE_THRESHOLD alerts.

    The check reads velocity_alerts_triggered before it increments. With the
    default threshold of 3 and a counter at zero, the first three charges
    that go strictly over a cap are allowed. The fourth freezes the wallet,
    and billing does not debit that charge. A closed or suspended wallet is
    not rewritten to frozen.
    """
    text = _llm()
    folded = " ".join(text.split())

    assert "Wallets are auto-frozen if spending exceeds" not in text
    assert "strictly over the cap" in folded
    assert "still allows that charge" in folded
    assert "VELOCITY_FREEZE_THRESHOLD" in text
    assert "default 3" in folded
    assert "fourth crossing charge" in folded
    assert "is not debited" in folded
    assert "not changed to frozen" in folded


def test_llm_txt_production_redis_outage_is_503_not_the_memory_limiter():
    """Production-like boots with REDIS_URL fail closed in RateLimitMiddleware."""
    text = _llm()
    folded = " ".join(text.split())

    assert "rate_limiter_unavailable" in text
    assert "HTTP 503" in text
    assert "does not use that in-memory limiter" in folded


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
    assert "wallet_frozen" in text
    assert "idempotency_in_progress" in text
    assert "-32005" in text
    assert "internal_error" in text
    assert "HTTP 200" in text
    assert "Do not branch on HTTP 402 or HTTP 409 for that route" in text
    assert "does not report either condition as HTTP 409" in folded
    assert "| 402 | Insufficient credits |" not in text


def test_dashboard_catalog_auth_depends_on_the_boot_mode():
    """GET /mcp/tools.json is anonymous only on a local-compatible boot."""
    page = " ".join(_dashboard().split())

    assert "Tool catalogs and invoke require an operator-issued key." not in page
    assert "production-like boot" in page
    assert "local quickstart may leave the catalog anonymous" in page
    assert "Invoke still requires a key." in page


def test_dashboard_me_routes_stay_inside_the_calling_key():
    """/v1/me refuses a bootstrap admin key and does not take a wallet id."""
    page = _dashboard()
    folded = " ".join(page.split())

    assert "wallet_key_required" in page
    assert "HTTP 403" in page
    assert "bootstrap admin key" in page
    assert "do not accept another wallet id" in folded
    assert "Permits and audit events are limited to this key." in page
    assert "Receipts are every receipt on this key's wallet." in page


@pytest.mark.anyio
async def test_served_static_docs_keep_the_corrected_claims():
    """The routes substitute PUBLIC_URL and must still carry the claims."""
    from app.routers.static import get_human_dashboard, get_llm_txt

    llm = (await get_llm_txt()).body.decode()
    page = (await get_human_dashboard()).body.decode()

    assert "{{PUBLIC_URL}}" not in llm
    assert "does not freeze the wallet on the first crossing" in " ".join(llm.split())
    assert "rate_limiter_unavailable" in llm
    assert "idempotency_key_reused" in llm
    assert "wallet_key_required" in page
    assert "local quickstart may leave the catalog anonymous" in page
