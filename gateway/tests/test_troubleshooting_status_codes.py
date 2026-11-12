"""TROUBLESHOOTING.md states the status the gateway actually returns (#350).

The REST invoke route answers ``idempotency_key_reused`` with 409; the server
side is pinned in ``tests/test_mcp_idempotency_key_validation.py``. This guards
the documentation side so the two cannot drift apart again.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_troubleshooting_gives_409_for_reused_idempotency_key() -> None:
    text = (ROOT / "TROUBLESHOOTING.md").read_text(encoding="utf-8")
    assert "`idempotency_key_reused` (`-32603`; REST `409`)" in text
    assert "`idempotency_key_reused` (`-32603`; REST `400`)" not in text


def test_self_credential_doc_gives_409_for_key_conflicts() -> None:
    text = (ROOT / "docs" / "agent-self-credentialing.md").read_text(encoding="utf-8")
    assert "same key, different body → 409" in text
    assert "→ 400" not in text
