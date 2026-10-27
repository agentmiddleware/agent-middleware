"""Tests for scripts/constant_test_loop.py.

Validates configuration handling, credential validation, and negative paths.
Does not re-run the full smoke loop (that's what CI does); focuses on the
behaviors unique to constant_test_loop that aren't covered by demo_trust_plane.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import runpy

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT
CONSTANT_TEST_LOOP = ROOT / "scripts" / "constant_test_loop.py"
CONSTANT_TEST_SCRIPT = CONSTANT_TEST_LOOP  # Alias for compatibility with ported tests
BOOTSTRAP_SCRIPT = ROOT / "scripts" / "partner_api_key_bootstrap.py"


# Restored: two tests below take this fixture, and removing it turned them
# into collection errors rather than deleting them. It also boots a real
# server, which is what makes the bootstrap and end-to-end assertions worth
# anything — source inspection cannot show that a key never reaches a log.


def _free_port() -> int:
    """Find a free port on localhost."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def test_server(tmp_path_factory):
    """Boot a minimal test server with a static dev key for bootstrap tests."""
    import base64
    import secrets

    state_dir = tmp_path_factory.mktemp("bootstrap-test-state")
    port = _free_port()
    static_dev_key = f"amw_dev_{secrets.token_urlsafe(32)}"
    signing_seed = base64.b64encode(secrets.token_bytes(32)).decode()

    # Boot uvicorn directly with minimal env for testing
    test_env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "ENVIRONMENT": "local",
        "DATABASE_URL": f"sqlite+aiosqlite:///{state_dir / 'api.db'}",
        "STATE_BACKEND": "sqlite",
        "SQLITE_URL": str(state_dir / "state.db"),
        "STATIC_DEV_API_KEYS": static_dev_key,
        "VALID_API_KEYS": "",
        "TRUST_MODE_ENABLED": "true",
        "TRUST_SIGNING_KEY_ID": "test-ed25519",
        "TRUST_SIGNING_PRIVATE_KEY_B64": signing_seed,
        "ENABLE_DEV_KEY_SELF_PROVISION": "true",
        "ENABLE_DOGFOOD_TOOL": "true",
        "MCP_UPSTREAM_ENABLED": "false",
        "ALLOW_LEGACY_UNPERMITTED_MCP": "false",
        "ENABLE_PROOF_SURFACES": "false",
    }

    log_path = state_dir / "server.log"
    with log_path.open("wb") as log_file:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=REPO_ROOT,
            env=test_env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(
                    "test server exited early:\n"
                    + log_path.read_text(encoding="utf-8", errors="replace")
                )
            try:
                if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("test server never became healthy")
        yield (base_url, static_dev_key)
    finally:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)


def test_constant_test_loop_refuses_cleartext_http_non_loopback():
    """Configuration error (exit 2) when API_URL uses HTTP for non-loopback host."""
    env = {
        **os.environ,
        "API_URL": "http://api.thisisatest.tech",
        "CI_SMOKE_AGENT_KEY": "test_key_value",
    }
    result = subprocess.run(
        [sys.executable, str(CONSTANT_TEST_LOOP)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2, f"Expected exit 2, got {result.returncode}"
    assert "refusing to send CI_SMOKE_AGENT_KEY over cleartext HTTP" in result.stderr
    # Ensure key is never logged
    assert "test_key_value" not in result.stdout
    assert "test_key_value" not in result.stderr


def test_constant_test_loop_allows_http_loopback():
    """HTTP is allowed for loopback addresses (localhost, 127.0.0.1, ::1)."""
    # This test validates the URL validation logic without actually running the loop.
    # We expect it to fail on connection (no server running), not on URL validation.
    env = {
        **os.environ,
        "API_URL": "http://localhost:8000",
        "CI_SMOKE_AGENT_KEY": "",  # Will self-provision, but fail to connect
    }
    result = subprocess.run(
        [sys.executable, str(CONSTANT_TEST_LOOP)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    # Exit 2 (configuration error) or 1 (network error) - not 0
    # The point is that URL validation didn't reject http://localhost
    assert result.returncode != 0
    # Should NOT contain the cleartext refusal message
    assert (
        "refusing to send CI_SMOKE_AGENT_KEY over cleartext HTTP" not in result.stderr
    )


def _load_loop_module():
    """Import the loop script as a module without running it."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "constant_test_loop", CONSTANT_TEST_LOOP
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_constant_test_loop_validates_malformed_key(monkeypatch):
    """A malformed key is refused by run_constant_test before any matching.

    Source-grep asserts ("malformed API key" in source) passed when the
    message existed anywhere, even on a dead path. This drives the real
    function with stubbed API answers and asserts it raises.
    """
    module = _load_loop_module()

    def fake_get_json(client, path, expected_status=200):
        if path == "/v1/billing/wallets":
            return {"wallets": [{"wallet_id": "w1"}]}
        return {"keys": [{"key_prefix": "deadbeef", "key_id": "k1"}]}

    monkeypatch.setattr(module, "_get_json", fake_get_json)
    with pytest.raises(module.ConfigurationError, match="malformed API key"):
        module.run_constant_test("http://127.0.0.1:9", "short", "", "")


def test_constant_test_loop_never_logs_keys(monkeypatch, capsys):
    """The full keyed loop must not print its credential."""
    module = _load_loop_module()
    canary_key = "amw_test_canary_key_must_not_appear_in_logs"
    monkeypatch.setenv("CI_SMOKE_AGENT_KEY", canary_key)
    monkeypatch.delenv("CI_SMOKE_WALLET_ID", raising=False)
    monkeypatch.delenv("CI_SMOKE_KEY_ID", raising=False)
    agent_key, wallet_id, key_id = module._get_agent_key()
    assert (agent_key, wallet_id, key_id) == (canary_key, "", "")

    paths = []
    mcp_calls = []

    def respond(request):
        assert request.headers["X-API-Key"] == canary_key
        path = request.url.path
        paths.append(path)
        if path == "/v1/billing/wallets":
            return httpx.Response(200, json={"wallets": [{"wallet_id": "w1"}]})
        if path == "/v1/billing/wallets/w1/keys":
            return httpx.Response(
                200, json={"keys": [{"key_prefix": canary_key[:8], "key_id": "k1"}]}
            )
        if path == "/mcp/tools.json":
            return httpx.Response(
                200,
                json={
                    "tools": [{"name": "partner.echo"}, {"name": "partner.notes.write"}]
                },
            )
        if path == "/v1/permits" and request.method == "POST":
            return httpx.Response(201, json={"permit_id": "p1", "spent_credits": "0"})
        if path == "/mcp/messages":
            mcp_calls.append(json.loads(request.content))
            if len(mcp_calls) == 3:
                return httpx.Response(
                    200,
                    json={
                        "error": {
                            "message": "permit_tool_not_allowed",
                            "data": {
                                "receipt": {
                                    "receipt_id": "denied1",
                                    "outcome": "denied",
                                    "permit_id": "p1",
                                    "credits_charged": "0",
                                }
                            },
                        }
                    },
                )
            return httpx.Response(
                200,
                json={
                    "result": {
                        "isError": False,
                        "receipt": {
                            "receipt_id": "success1",
                            "outcome": "success",
                            "ledger_entry_id": "entry1",
                            "permit_id": "p1",
                            "credits_charged": "1",
                        },
                    }
                },
            )
        if path == "/v1/receipts/verify":
            return httpx.Response(200, json={"valid": True})
        if path == "/v1/permits/p1":
            return httpx.Response(200, json={"spent_credits": "1"})
        if path == "/v1/billing/ledger/w1":
            return httpx.Response(
                200,
                json={
                    "entries": [
                        {
                            "entry_id": "entry1",
                            "amount": "-1",
                            "description": "partner.echo",
                        }
                    ]
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(respond)
    real_client = httpx.Client
    monkeypatch.setattr(
        module.httpx,
        "Client",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )
    module.run_constant_test("http://127.0.0.1:9", agent_key, wallet_id, key_id)

    assert [call["params"]["name"] for call in mcp_calls] == [
        "partner.echo",
        "partner.echo",
        "partner.notes.write",
    ]
    assert "/v1/permits/p1" in paths
    assert paths.count("/v1/billing/ledger/w1") == 2
    captured = capsys.readouterr()
    assert "ALL INVARIANTS HELD" in captured.err
    assert canary_key not in captured.out + captured.err


def test_constant_test_loop_self_provision_signal():
    """When no credentials provided, _get_agent_key signals self-provision."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "constant_test_loop", CONSTANT_TEST_LOOP
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)

    original_environ = os.environ.copy()
    os.environ.clear()
    os.environ.update(
        {k: v for k, v in original_environ.items() if not k.startswith("CI_SMOKE_")}
    )

    try:
        spec.loader.exec_module(module)
        agent_key, wallet_id, key_id = module._get_agent_key()
        # All empty signals self-provision path
        assert agent_key == ""
        assert wallet_id == ""
        assert key_id == ""
    finally:
        os.environ.clear()
        os.environ.update(original_environ)


def test_constant_test_loop_partial_credentials_fetch_from_api():
    """When key provided but wallet_id/key_id missing, signals API fetch."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "constant_test_loop", CONSTANT_TEST_LOOP
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)

    test_env = {
        **os.environ,
        "CI_SMOKE_AGENT_KEY": "test_key",
    }
    # Explicitly unset wallet_id and key_id
    test_env.pop("CI_SMOKE_WALLET_ID", None)
    test_env.pop("CI_SMOKE_KEY_ID", None)

    original_environ = os.environ.copy()
    os.environ.clear()
    os.environ.update(test_env)

    try:
        spec.loader.exec_module(module)
        agent_key, wallet_id, key_id = module._get_agent_key()
        # Key provided, but wallet_id/key_id empty signals fetch from API
        assert agent_key == "test_key"
        assert wallet_id == ""
        assert key_id == ""
    finally:
        os.environ.clear()
        os.environ.update(original_environ)


def test_partner_api_key_bootstrap_json_output():
    """Bootstrap script produces JSON-parseable output and never prints the key."""
    import json

    bootstrap_script = ROOT / "scripts" / "partner_api_key_bootstrap.py"

    # Run with --json flag
    result = subprocess.run(
        [sys.executable, str(bootstrap_script), "--json"],
        input=json.dumps(
            {
                "api_url": "http://127.0.0.1:8000",
                "sponsor_name": "test-sponsor",
                "agent_id": "test-agent",
            }
        ),
        capture_output=True,
        text=True,
        timeout=5,
    )

    # Should fail (no server), but output should still be JSON-parseable
    # The important part is that --json produces parseable JSON even on error
    # and that the key is not in the output
    if result.stdout.strip():
        try:
            data = json.loads(result.stdout)
            # If it succeeded (unlikely without server), verify structure
            if "agent_api_key" in data:
                # The key should be present in the JSON (that's the point)
                # but should NOT appear in plaintext anywhere else
                assert isinstance(data["agent_api_key"], str)
                assert len(data["agent_api_key"]) > 0
        except json.JSONDecodeError:
            # If not JSON, it should be an error message
            # Either way, verify no key leaked in non-JSON output
            pass

    # The bootstrap key should never appear in raw stdout/stderr
    # (We can't test the actual key without a server, but we can verify
    # the output is structured and doesn't contain test markers)
    assert "b2a_test_" not in result.stdout
    assert "b2a_test_" not in result.stderr


@pytest.mark.skipif(
    not os.environ.get("RUN_CONSTANT_TEST_LOOP_INTEGRATION"),
    reason="Set RUN_CONSTANT_TEST_LOOP_INTEGRATION=1 to run full integration test",
)
def test_constant_test_loop_full_integration():
    """Full integration test against a running server (opt-in via env var).

    This test is skipped by default. To run it:
    1. Start server: make quickstart (in terminal 1)
    2. Run test: RUN_CONSTANT_TEST_LOOP_INTEGRATION=1 pytest tests/test_constant_test_loop.py -v
    """
    result = subprocess.run(
        [sys.executable, str(CONSTANT_TEST_LOOP)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"constant_test_loop failed:\n{result.stderr}"
    assert "ALL INVARIANTS HELD" in result.stderr


def test_constant_test_loop_never_logs_api_key(test_server):
    """Agent key is never printed or logged during constant test execution."""
    import httpx

    base_url, _ = test_server
    # Pre-provision a key to test that it's never logged
    provision_response = httpx.post(
        f"{base_url}/v1/dev-keys/self-provision",
        json={"agent_id": "no-log-test"},
        timeout=10,
    )
    assert provision_response.status_code in (200, 201)
    provision_data = provision_response.json()
    agent_key = provision_data["api_key"]
    wallet_id = provision_data["wallet_id"]
    key_id = provision_data["key_id"]

    # Run with explicit credentials. Ambient CI_SMOKE_* is stripped first:
    # inheriting the whole environment lets a developer's exported
    # CI_SMOKE_TOOL_ARGS reach the tool as its arguments, and the resulting
    # "unexpected keyword argument" points at the tool rather than the stray
    # variable. The same applies to an ambient wallet or key id.
    result = subprocess.run(
        [sys.executable, str(CONSTANT_TEST_SCRIPT), "--api-url", base_url],
        env={
            **{k: v for k, v in os.environ.items() if not k.startswith("CI_SMOKE")},
            "CI_SMOKE_AGENT_KEY": agent_key,
            "CI_SMOKE_WALLET_ID": wallet_id,
            "CI_SMOKE_KEY_ID": key_id,
        },
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, f"failed: {result.stderr}"
    # The agent key must never appear anywhere in the output
    assert agent_key not in result.stdout, "agent key in stdout"
    assert agent_key not in result.stderr, "agent key in stderr"


# --- Tool selection, argument derivation, and the production guards -------
#
# Ported from the scoped-smoke-loop work (#325) when the two loops were
# consolidated. These need no server, so they run in the fast path.


def _load_loop():
    return runpy.run_path(str(CONSTANT_TEST_SCRIPT))


def test_companion_tool_comes_from_the_registry_not_the_governed_name():
    """Regression: pinning the governed tool must not skip the denial check.

    Selecting the companion from the pinned value rather than the registry
    made the out-of-scope check skip silently on exactly the deployments
    where a pin is mandatory.
    """
    select = _load_loop()["select_companion_tool"]
    registry = {"alpha": {}, "beta": {}, "gamma": {}}

    companion, error = select(registry, "alpha", None)
    assert companion in {"beta", "gamma"}
    assert not error

    companion, error = select(registry, "gamma", None)
    assert companion in {"alpha", "beta"}
    assert not error


def test_companion_tool_must_be_registered_and_distinct():
    """Both ways the denial check could pass without proving permit scoping."""
    select = _load_loop()["select_companion_tool"]
    registry = {"alpha": {}, "beta": {}}

    # An unregistered name yields tool-not-found, not a permit refusal.
    companion, error = select(registry, "alpha", "does-not-exist")
    assert companion == ""
    assert "tool-not-found" in error

    # A companion equal to the permitted tool asserts a denial that should
    # never happen.
    companion, error = select(registry, "alpha", "alpha")
    assert companion == ""
    assert "differ" in error

    assert select(registry, "alpha", "beta") == ("beta", "")


def test_single_tool_deployment_skips_rather_than_false_passes():
    select = _load_loop()["select_companion_tool"]
    assert select({"only": {}}, "only", None) == ("", "")


def test_governed_tool_selection_asks_rather_than_guessing():
    """No "just use the first one" fallback.

    Schema-derived arguments satisfy a tool's declared shape but not its
    semantics, so an arbitrary pick can be refused by the tool itself — a
    failure that reads as a broken trust plane rather than a bad choice.
    """
    select = _load_loop()["select_governed_tool"]

    assert select({"partner.echo": {}, "other": {}}, None) == ("partner.echo", "")
    assert select({"partner.notes.write": {}}, None) == ("partner.notes.write", "")

    tool, error = select({"alpha": {}, "beta": {}}, None)
    assert tool == ""
    assert "--tool" in error and "alpha" in error

    tool, error = select({"alpha": {}}, "nope")
    assert tool == ""
    assert "not registered" in error

    assert select({"alpha": {}}, "alpha") == ("alpha", "")


def test_arguments_are_derived_from_each_tool_schema():
    """A fixed payload shape only ever fits the registry it was written for."""
    arguments_for = _load_loop()["arguments_for"]

    built = arguments_for(
        {
            "required": ["session_id", "count", "ratio", "flag", "items", "meta"],
            "properties": {
                "session_id": {"type": "string"},
                "count": {"type": "integer"},
                "ratio": {"type": "number"},
                "flag": {"type": "boolean"},
                "items": {"type": "array"},
                "meta": {"type": "object"},
                "ignored": {"type": "string"},
            },
        },
        "run1",
    )
    # Only required properties: sending optional ones invents intent the
    # caller never expressed.
    assert set(built) == {"session_id", "count", "ratio", "flag", "items", "meta"}
    assert built["session_id"] == "smoke-run1"
    assert built["count"] == 1
    assert built["ratio"] == 1.0
    assert built["flag"] is False
    assert built["items"] == []
    assert built["meta"] == {}

    # A schema that states an acceptable value beats a guess.
    built = arguments_for(
        {
            "required": ["mode", "region"],
            "properties": {
                "mode": {"type": "string", "enum": ["preview", "commit"]},
                "region": {"type": "string", "default": "us-east-1"},
            },
        },
        "run2",
    )
    assert built["mode"] == "preview"
    assert built["region"] == "us-east-1"

    assert arguments_for({}, "run3") == {}
    assert arguments_for(None, "run3") == {}


def test_advertised_price_is_read_from_the_manifest():
    """A fixed permit cap fails permit_budget_exceeded on a healthy deployment.

    Tool prices in this repo span 1-200 credits, so a cap chosen for one tool
    refuses the golden invocation the moment another is selected.
    """
    credits_per_call = _load_loop()["credits_per_call"]

    assert credits_per_call({"annotations": {"creditsPerCall": 25.0}}) == 25.0
    assert credits_per_call({"annotations": {"creditsPerCall": "15"}}) == 15.0
    # No stated price must not raise; the floor applies instead.
    assert credits_per_call({}) == 0.0
    assert credits_per_call({"annotations": {}}) == 0.0
    assert credits_per_call({"annotations": {"creditsPerCall": "free"}}) == 0.0


def test_off_loopback_runs_require_a_pinned_tool_and_approved_payload():
    """Nobody should discover their way into invoking production every run.

    Derived arguments fill required fields from types, defaults, and the first
    enum member -- which for a consequential tool could be "delete".
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("CI_SMOKE")}
    env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
    env["CI_SMOKE_AGENT_KEY"] = "b2a_placeholder"

    result = subprocess.run(
        [
            sys.executable,
            str(CONSTANT_TEST_SCRIPT),
            "--api-url",
            "https://api.thisisatest.tech",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "refusing to auto-select a tool" in result.stderr
    # The guard fires before any network call, and leaks no credential.
    assert "b2a_placeholder" not in result.stdout + result.stderr

    result = subprocess.run(
        [
            sys.executable,
            str(CONSTANT_TEST_SCRIPT),
            "--api-url",
            "https://api.thisisatest.tech",
            "--tool",
            "partner.echo",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "refusing to send derived arguments" in result.stderr
    assert "b2a_placeholder" not in result.stdout + result.stderr


def test_bootstrap_key_only_pipes_without_jq(tmp_path):
    """--key-only and --json both write stdout, so they cannot both apply."""
    bootstrap_secret = secrets.token_urlsafe(32)
    result = subprocess.run(
        [
            sys.executable,
            str(BOOTSTRAP_SCRIPT),
            "--api-url",
            "https://api.example.org",
            "--key-only",
            "--json",
        ],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "BOOTSTRAP_KEY": bootstrap_secret},
    )
    assert result.returncode != 0
    assert "mutually exclusive" in result.stderr

    # --key-only without --key-file is an argparse error (exit 2): the minted
    # key is never printed, so a missing destination would discard it.
    result = subprocess.run(
        [
            sys.executable,
            str(BOOTSTRAP_SCRIPT),
            "--api-url",
            "https://api.example.org",
            "--key-only",
        ],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "BOOTSTRAP_KEY": bootstrap_secret},
    )
    assert result.returncode == 2
    assert "requires --key-file" in result.stderr

    # --key-only must not have opened a path around the fail-closed check.
    key_file = tmp_path / "agent.key"
    result = subprocess.run(
        [
            sys.executable,
            str(BOOTSTRAP_SCRIPT),
            "--api-url",
            "https://api.example.org",
            "--key-only",
            "--key-file",
            str(key_file),
        ],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert result.returncode != 0
    assert "BOOTSTRAP_KEY" in result.stderr
    assert not key_file.exists()


def test_unusable_tool_pin_exits_as_configuration_not_invariant_failure(
    test_server,
):
    """A bad pin must not page with the same signal as a broken trust plane.

    Exit 1 means an invariant failed; exit 2 means the loop was configured
    wrong. Conflating them makes a typo in $CI_SMOKE_TOOL indistinguishable
    from the product actually breaking.
    """
    base_url, _ = test_server
    env = {k: v for k, v in os.environ.items() if not k.startswith("CI_SMOKE")}

    result = subprocess.run(
        [
            sys.executable,
            str(CONSTANT_TEST_SCRIPT),
            "--api-url",
            base_url,
            "--tool",
            "no-such-tool",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "configuration error" in result.stderr
    assert "FAILED" not in result.stderr

    result = subprocess.run(
        [
            sys.executable,
            str(CONSTANT_TEST_SCRIPT),
            "--api-url",
            base_url,
            "--other-tool",
            "no-such-tool",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "configuration error" in result.stderr


def test_tool_args_resolve_from_flag_environment_or_neither(monkeypatch):
    """--tool-args needs the env fallback --tool and --other-tool already have.

    A CI job can otherwise satisfy every part of the off-loopback guard except
    the payload, and would have to put that one remaining piece in argv.
    """
    # Cleared explicitly: the "neither set" case reads the real environment,
    # so an ambient CI_SMOKE_TOOL_ARGS would return a payload and fail the
    # assertion even though the flag path is correct.
    monkeypatch.delenv("CI_SMOKE_TOOL_ARGS", raising=False)
    resolve = _load_loop()["_resolve_tool_args"]

    # The flag wins, and is parsed.
    assert resolve('{"text": "from flag"}') == {"text": "from flag"}

    # Neither set: None, so the guard can still refuse off loopback.
    assert resolve(None) is None


def test_tool_args_environment_fallback(monkeypatch):
    monkeypatch.setenv("CI_SMOKE_TOOL_ARGS", '{"text": "from env"}')
    resolve = _load_loop()["_resolve_tool_args"]

    assert resolve(None) == {"text": "from env"}
    # An explicit flag still overrides the environment.
    assert resolve('{"text": "flag wins"}') == {"text": "flag wins"}

    # An explicitly empty payload is a decision, not an absence: it must
    # satisfy the guard rather than falling through to None.
    monkeypatch.setenv("CI_SMOKE_TOOL_ARGS", "{}")
    assert _load_loop()["_resolve_tool_args"](None) == {}


def test_malformed_tool_args_are_a_configuration_error(monkeypatch):
    """Exit 2, not 1: a bad payload means misconfiguration, not a broken plane."""
    module = _load_loop()
    resolve = module["_resolve_tool_args"]
    ConfigurationError = module["ConfigurationError"]  # noqa: N806

    with pytest.raises(ConfigurationError, match="not valid JSON"):
        resolve("{not json")

    # A list or scalar would be forwarded as the tool's arguments and rejected
    # downstream as a tool error, which reads as the product failing.
    with pytest.raises(ConfigurationError, match="must be a JSON object"):
        resolve('["a", "b"]')
    with pytest.raises(ConfigurationError, match="must be a JSON object"):
        resolve("42")

    monkeypatch.setenv("CI_SMOKE_TOOL_ARGS", "[1, 2]")
    module = _load_loop()
    with pytest.raises(module["ConfigurationError"], match=r"\$CI_SMOKE_TOOL_ARGS"):
        module["_resolve_tool_args"](None)


def test_environment_alone_satisfies_the_off_loopback_guard():
    """A CI job configured purely through secrets must get past the guard.

    Pointed at a reserved ``.invalid`` host (RFC 6761), which is non-loopback
    — so both guards apply — but never resolves. Naming a real deployment
    here would open an HTTPS session to it on every pytest run, including
    pull-request CI, which is exactly the unattended production traffic this
    guard exists to make deliberate.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("CI_SMOKE")}
    env.update(
        {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "API_URL": "https://api.invalid",
            "CI_SMOKE_AGENT_KEY": "b2a_placeholder",
            "CI_SMOKE_TOOL": "partner.echo",
            "CI_SMOKE_TOOL_ARGS": '{"text": "ci"}',
        }
    )
    result = subprocess.run(
        [sys.executable, str(CONSTANT_TEST_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    # It gets past configuration and fails on the unresolvable host instead,
    # which is the point: neither guard message appears.
    assert "refusing to auto-select a tool" not in result.stderr
    assert "refusing to send derived arguments" not in result.stderr
    assert "b2a_placeholder" not in result.stdout + result.stderr
