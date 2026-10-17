"""Smoke tests for the buyer-visible examples.

Every script under examples/ is something a prospect may copy-paste, and CI
waves through packages with zero tests, so these examples used to rot
silently. These tests pin the cheap, stdlib-only properties: each script
compiles, none relies on a sys.path hack, the MCP client config is a valid
template, the retry demo's receipt parsing works on sample payloads, and the
README documents every script with the golden-path versus legacy split.

Only stdlib plus pytest is used so the file also runs with --noconftest on a
bare interpreter; under CI it runs inside the gateway suite like the rest.
"""

from __future__ import annotations

import importlib.util
import json
import py_compile
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
EXAMPLES = REPO / "examples"
MCP_CLIENT = EXAMPLES / "mcp-client"
README = EXAMPLES / "README.md"

SCRIPTS = [
    EXAMPLES / "dry_run_example.py",
    EXAMPLES / "mcp_tool_example.py",
    EXAMPLES / "acta_receipt_interop.py",
    MCP_CLIENT / "call_with_retry.py",
]


def test_all_example_scripts_compile() -> None:
    """Each example must at least parse, so edits cannot rot silently."""
    for script in SCRIPTS:
        assert script.is_file(), f"missing example script: {script.name}"
        py_compile.compile(str(script), doraise=True)


def test_no_sys_path_hacks_in_examples() -> None:
    """Examples run from the documented pip install, not path shims.

    The old shims pointed at <root>/b2a_sdk/src, which does not exist (the
    SDK lives at sdk/python/src), so they never worked on a fresh checkout.
    """
    for script in SCRIPTS:
        text = script.read_text(encoding="utf-8")
        assert "sys.path" not in text, f"{script.name} still mutates sys.path"


def test_sdk_imports_fail_with_install_hint() -> None:
    """SDK-backed examples name the documented install when not installed."""
    for name in ("dry_run_example.py", "mcp_tool_example.py"):
        text = (EXAMPLES / name).read_text(encoding="utf-8")
        assert "python -m pip install -e './sdk/python[dev]'" in text


def _load_retry_module() -> types.ModuleType:
    """Load call_with_retry without the third-party MCP SDK installed."""
    inserted: list[str] = []
    if "mcp" not in sys.modules:
        mcp = types.ModuleType("mcp")
        client_pkg = types.ModuleType("mcp.client")
        stream_pkg = types.ModuleType("mcp.client.streamable_http")

        class _ClientSession:  # pragma: no cover - import-time stub only
            def __init__(self, *args, **kwargs):
                raise AssertionError("network stub must not be used in tests")

        def _streamablehttp_client(*args, **kwargs):  # pragma: no cover
            raise AssertionError("network stub must not be used in tests")

        mcp.ClientSession = _ClientSession
        stream_pkg.streamablehttp_client = _streamablehttp_client
        for name, mod in (
            ("mcp", mcp),
            ("mcp.client", client_pkg),
            ("mcp.client.streamable_http", stream_pkg),
        ):
            sys.modules[name] = mod
            inserted.append(name)
    try:
        spec = importlib.util.spec_from_file_location(
            "example_call_with_retry", MCP_CLIENT / "call_with_retry.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name in inserted:
            sys.modules.pop(name, None)


def _sample_result(receipt_id: str, outcome: str = "success") -> dict:
    return {
        "_meta": {
            "io.agentmiddleware/receipt": {
                "receipt_id": receipt_id,
                "outcome": outcome,
            }
        }
    }


def test_retry_receipt_parsing_returns_identical_ids() -> None:
    """Same idempotency key means the same receipt id on first call and retry."""
    module = _load_retry_module()
    first = _sample_result("rcpt-9f2c41a7")
    retry = _sample_result("rcpt-9f2c41a7")
    assert module.receipt_id(first) == "rcpt-9f2c41a7"
    assert module.receipt_id(retry) == module.receipt_id(first)
    assert module._outcome(first) == "success"


def test_retry_receipt_parsing_tolerates_meta_shapes() -> None:
    """The gateway may spell the envelope meta or _meta; both must parse."""
    module = _load_retry_module()
    via_meta = {"meta": {"io.agentmiddleware/receipt": {"receipt_id": "r1"}}}
    assert module.receipt_id(via_meta) == "r1"
    assert module.receipt_id({}) is None


def test_recorded_sample_output_matches_script_format() -> None:
    """The checked-in sample output must look like the script's own prints."""
    sample = (MCP_CLIENT / "expected_output.txt").read_text(encoding="utf-8")
    lines = [
        line for line in sample.splitlines() if line.startswith(("first", "retry"))
    ]
    assert len(lines) == 2, "sample must show the first call and the retry"
    ids = [line.split("receipt:")[1].split()[0] for line in lines]
    assert ids[0] == ids[1], "retry must return the same receipt id, not a new charge"
    for line in lines:
        assert "outcome:" in line


def test_mcp_json_is_a_valid_local_template() -> None:
    """The client config must parse and point at the local gateway path."""
    config = json.loads((MCP_CLIENT / "mcp.json").read_text(encoding="utf-8"))
    server = config["mcpServers"]["agent-middleware"]
    assert server["type"] == "http"
    assert server["url"].endswith("/mcp")
    assert "X-API-Key" in server["headers"]
    assert "Idempotency-Key" in server["headers"]


def test_readme_documents_every_script_with_status() -> None:
    """README must name each script and split golden path from legacy."""
    text = README.read_text(encoding="utf-8")
    for script in SCRIPTS:
        assert script.name in text, f"README does not mention {script.name}"
    assert "mcp.json" in text
    assert "expected_output" in text
    lowered = text.lower()
    assert "golden" in lowered
    assert "legacy" in lowered
    assert "research" in lowered
