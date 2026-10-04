"""BE-100: integration guidance must disclose incomplete terminal evidence."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _assert_scoped_retry_guidance(relative_path: str) -> None:
    text = " ".join((ROOT / relative_path).read_text().split())
    assert "at most one debit" in text
    assert "same accepted idempotency key" in text
    assert "configured upstream MCP" in text
    assert "manual_review_required" in text
    assert "no receipt" in text
    assert "Do not retry with a new idempotency key" in text
    assert "Local governed tools have no dispatch state machine" in text
    assert "failure-semantics.md" in text
    assert "one authorization, one debit, one finalized receipt" not in text
    assert "The governance path is identical" not in text
    assert "exactly-once dispatch" not in text
    assert "one gateway dispatch, one debit, one receipt" not in text
    assert "charge exactly once" not in text


def test_be100_self_credentialing_scopes_retry_guarantees() -> None:
    _assert_scoped_retry_guidance("docs/agent-self-credentialing.md")


def test_be100_tool_interface_scopes_retry_guarantees() -> None:
    _assert_scoped_retry_guidance("docs/tool-interface-authority.md")
