"""Tests for the runnable example scripts.

Import side effects are part of the contract: each script inserts the repo
SDK source on sys.path, and the dry-run and MCP demos report failure with a
non-zero status instead of looking successful.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path

import dry_run_example
import mcp_tool_example
import pytest

_REPO_ROOT = Path(os.path.abspath(dry_run_example.__file__)).parents[1]
_SDK_SRC = _REPO_ROOT / "sdk" / "python" / "src"
_RETRY_PATH = _REPO_ROOT / "examples" / "mcp-client" / "call_with_retry.py"
_retry_spec = importlib.util.spec_from_file_location("call_with_retry", _RETRY_PATH)
assert _retry_spec is not None and _retry_spec.loader is not None
call_with_retry = importlib.util.module_from_spec(_retry_spec)
sys.modules[_retry_spec.name] = call_with_retry
_retry_spec.loader.exec_module(call_with_retry)


def test_scripts_insert_the_repo_sdk_source() -> None:
    """A clean clone must import b2a_sdk from sdk/python/src, not a missing directory."""
    assert (_SDK_SRC / "b2a_sdk" / "__init__.py").is_file()
    assert str(_SDK_SRC) in sys.path
    missing = _REPO_ROOT / "b2a_sdk" / "src"
    assert str(missing) not in sys.path


def test_register_replays_tools_through_the_callback(capsys: pytest.CaptureFixture[str]) -> None:
    """--register must show the callback output. Decoration already ran at import."""
    asyncio.run(mcp_tool_example.register_tools())
    out = capsys.readouterr().out
    assert "Registered tool: data-processor" in out
    assert "Registered tool: url-summarizer" in out
    assert "Registered tool: image-generator" in out


def test_dry_run_failure_returns_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed demo must not exit as if the simulation completed."""

    async def boom() -> None:
        raise RuntimeError("api down")

    monkeypatch.setattr(dry_run_example, "demo_simulation", boom)
    assert asyncio.run(dry_run_example.main()) == 1


def test_equal_workflow_costs_are_not_called_cheaper() -> None:
    """Equal totals are a tie. The lower total is the one called cheaper."""
    assert "same" in dry_run_example._cheaper_label(5, 5).lower()
    assert dry_run_example._cheaper_label(1, 2).startswith("Workflow A")
    assert dry_run_example._cheaper_label(3, 1).startswith("Workflow B")


def test_receipt_id_reads_meta_or_alias() -> None:
    """The MCP SDK dumps the field as meta. Some callers still send _meta."""
    receipt = {"receipt_id": "rcpt-1", "outcome": "success"}
    meta_result = {"meta": {call_with_retry.RECEIPT_META_KEY: receipt}}
    alias_result = {"_meta": {call_with_retry.RECEIPT_META_KEY: receipt}}
    assert call_with_retry.receipt_id(meta_result) == "rcpt-1"
    assert call_with_retry._outcome(meta_result) == "success"
    assert call_with_retry.receipt_id(alias_result) == "rcpt-1"
    assert call_with_retry.receipt_id({}) is None
    assert call_with_retry.receipt_id({"meta": {}}) is None


def test_retry_mismatch_returns_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    """The script's job is one receipt reused on retry. A second id is a failure."""
    calls = {"n": 0}

    async def fake(_api_key: str, _key: str) -> dict:
        calls["n"] += 1
        return {
            "meta": {
                call_with_retry.RECEIPT_META_KEY: {
                    "receipt_id": f"rcpt-{calls['n']}",
                    "outcome": "success",
                }
            }
        }

    monkeypatch.setenv("AGENT_API_KEY", "local-test-key")
    monkeypatch.setattr(call_with_retry, "call_once", fake)
    assert asyncio.run(call_with_retry.main()) == 1


def test_retry_same_receipt_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Matching receipt ids are the successful demo."""

    async def fake(_api_key: str, _key: str) -> dict:
        return {
            "meta": {
                call_with_retry.RECEIPT_META_KEY: {
                    "receipt_id": "rcpt-same",
                    "outcome": "success",
                }
            }
        }

    monkeypatch.setenv("AGENT_API_KEY", "local-test-key")
    monkeypatch.setattr(call_with_retry, "call_once", fake)
    assert asyncio.run(call_with_retry.main()) == 0
