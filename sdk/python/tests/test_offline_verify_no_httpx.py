"""The offline receipt verifier must work with only ``cryptography`` installed.

The ``--keys`` verification path is the trust plane's promise that anyone can
check a signed receipt with no network access and no account. That promise is
only real if the code path also carries no *network dependency*: a partner who
``pip install cryptography`` and runs the verifier must not hit a
``ModuleNotFoundError`` for httpx.

Regression guard: importing ``b2a_sdk`` used to eagerly import the httpx-backed
HTTP client, so ``python -m b2a_sdk.verify_cli --keys ...`` died at import time
when httpx was absent — even though the verifier itself never touches httpx.
These tests run the offline path in a subprocess with httpx forced unavailable.

Collection must not require ``cryptography`` (the SDK wheel job installs the
bare wheel), so the signing helper imports it lazily and the real-verification
test skips when it is absent, matching ``test_receipt_verifier.py``.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SDK_SRC = Path(__file__).resolve().parents[1] / "src"

# A preamble that makes ``import httpx`` fail even when httpx is installed, so
# the test proves independence from httpx on any machine (the wheel job has
# httpx present as a base dependency).
_BLOCK_HTTPX = "import sys; sys.modules['httpx'] = None\n"


def _run(code: str, *, block_httpx: bool) -> subprocess.CompletedProcess:
    # Prepend to (not replace) PYTHONPATH: keep any inherited PYTHONPATH intact
    # while ensuring the SDK source wins on import.
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{SDK_SRC}{os.pathsep}{existing}" if existing else str(SDK_SRC)
    return subprocess.run(
        [sys.executable, "-c", (_BLOCK_HTTPX if block_httpx else "") + code],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _write_signed_bundle(tmp_path: Path) -> tuple[Path, Path]:
    """Produce a genuinely-signed bundle and its public key document.

    Signs with ``cryptography`` only — the same dependency the verifier uses —
    so the fixture needs nothing the offline path itself would not have.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private_key = Ed25519PrivateKey.generate()
    public_raw = private_key.public_key().public_bytes_raw()
    kid = "offline-test-ed25519"

    signing_input = json.dumps(
        {"kid": kid, "outcome": "success", "receipt_id": "rcpt-offline-test"},
        separators=(",", ":"),
        sort_keys=True,
    )
    signature = private_key.sign(signing_input.encode("utf-8"))

    bundle = {
        "schema_version": "1.0",
        "receipt_id": "rcpt-offline-test",
        "alg": "Ed25519",
        "kid": kid,
        "canonicalization": "awi-canonical-json/1",
        "signing_input": signing_input,
        "signature": base64.b64encode(signature).decode(),
    }
    key_doc = {
        "schema_version": "1.0",
        "alg": "Ed25519",
        "keys": [
            {
                "kid": kid,
                "alg": "Ed25519",
                "status": "active",
                "public_key_b64": base64.b64encode(public_raw).decode(),
            }
        ],
    }

    bundle_path = tmp_path / "receipt-bundle.json"
    keys_path = tmp_path / "trust-keys.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    keys_path.write_text(json.dumps(key_doc), encoding="utf-8")
    return bundle_path, keys_path


@pytest.mark.parametrize("sdk_name", ["b2a_sdk", "amw"])
def test_package_and_verifier_import_without_httpx(sdk_name):
    # Import only — needs neither httpx nor cryptography. This is the exact
    # step that used to fail: importing b2a_sdk pulled in the httpx client.
    result = _run(
        f"import {sdk_name}\n"
        f"from {sdk_name} import verify_bundle, key_set_from_document\n"
        "import b2a_sdk.verify_cli\n"
        "print('ok')\n",
        block_httpx=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout


@pytest.mark.parametrize(
    "imports",
    ["import b2a_sdk", "import amw\nimport b2a_sdk"],
    ids=["legacy-first", "new-package-first"],
)
def test_legacy_import_emits_deprecation_warning(imports):
    result = _run(
        "import warnings\n"
        "with warnings.catch_warnings(record=True) as caught:\n"
        "    warnings.simplefilter('always', DeprecationWarning)\n"
        + "\n".join(f"    {line}" for line in imports.splitlines())
        + "\n"
        "assert len(caught) == 1, caught\n"
        "assert caught[0].category is DeprecationWarning\n"
        "assert 'b2a_sdk is deprecated; import amw instead' in str(caught[0].message)\n",
        block_httpx=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_amw_import_and_offline_exports_do_not_warn():
    result = _run(
        "import warnings\n"
        "with warnings.catch_warnings(record=True) as caught:\n"
        "    warnings.simplefilter('always', DeprecationWarning)\n"
        "    import amw\n"
        "    from amw import Receipt, request_key, verify_bundle\n"
        "assert not caught, caught\n",
        block_httpx=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_amw_client_import_does_not_warn():
    result = _run(
        "import warnings\n"
        "with warnings.catch_warnings(record=True) as caught:\n"
        "    warnings.simplefilter('always', DeprecationWarning)\n"
        "    from amw import Client\n"
        "assert Client is not None\n"
        "assert not caught, caught\n",
        block_httpx=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_amw_reexports_every_public_sdk_name():
    import amw
    import b2a_sdk

    assert amw.__all__ == b2a_sdk.__all__
    assert amw.__version__ == b2a_sdk.__version__
    for name in b2a_sdk.__all__:
        assert getattr(amw, name) is getattr(b2a_sdk, name), name


@pytest.mark.parametrize(
    ("cli_module", "command"),
    [("b2a_sdk.verify_cli", []), ("amw.cli", ["verify"])],
    ids=["b2a-verify-receipt", "amw verify"],
)
def test_verify_cli_keys_path_runs_without_httpx(tmp_path, cli_module, command):
    pytest.importorskip("cryptography")
    bundle_path, keys_path = _write_signed_bundle(tmp_path)
    # Drive the CLI's main() exactly as `python -m b2a_sdk.verify_cli` would,
    # which imports the b2a_sdk package first — the step that used to fail.
    args = [*command, "--bundle", str(bundle_path), "--keys", str(keys_path)]
    result = _run(
        f"from {cli_module} import main\nraise SystemExit(main({args!r}))\n",
        block_httpx=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "VERIFIED" in result.stdout


def test_verify_cli_aliases_match_default_output(tmp_path):
    pytest.importorskip("cryptography")
    bundle_path, keys_path = _write_signed_bundle(tmp_path)

    for path, expected_code in [(bundle_path, 0), (tmp_path / "missing.json", 2)]:
        args = ["--bundle", str(path), "--keys", str(keys_path)]
        legacy = _run(
            f"from b2a_sdk.verify_cli import main\nraise SystemExit(main({args!r}))\n",
            block_httpx=True,
        )
        new = _run(
            f"from amw.cli import main\nraise SystemExit(main({['verify', *args]!r}))\n",
            block_httpx=True,
        )
        assert (legacy.returncode, legacy.stdout, legacy.stderr) == (
            new.returncode,
            new.stdout,
            new.stderr,
        )
        assert legacy.returncode == expected_code


@pytest.mark.parametrize(
    ("cli_module", "command"),
    [("b2a_sdk.verify_cli", []), ("amw.cli", ["verify"])],
    ids=["b2a-verify-receipt", "amw verify"],
)
def test_tampered_bundle_rejected_without_httpx(tmp_path, cli_module, command):
    """The dependency-minimal path must still REJECT a forged bundle.

    VERIFY.md tells partners to branch on exit code 1; prove that contract
    holds in the same httpx-free configuration, not just the accept path.
    """
    pytest.importorskip("cryptography")
    bundle_path, keys_path = _write_signed_bundle(tmp_path)
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    bundle["signing_input"] = bundle["signing_input"].replace(
        '"outcome":"success"', '"outcome":"denied"'
    )
    assert '"outcome":"denied"' in bundle["signing_input"]  # the edit must bite
    forged = tmp_path / "forged-bundle.json"
    forged.write_text(json.dumps(bundle), encoding="utf-8")
    args = [*command, "--bundle", str(forged), "--keys", str(keys_path)]
    result = _run(
        f"from {cli_module} import main\nraise SystemExit(main({args!r}))\n",
        block_httpx=True,
    )
    # 1 = well-formed but does not verify; must not be 0 and must not crash (2).
    assert result.returncode == 1, result.stdout + result.stderr
    assert "INVALID" in result.stderr


def test_unknown_attribute_raises_attribute_error():
    """The lazy __getattr__ must raise AttributeError for unknown names, not
    ImportError -- wrong type breaks hasattr/getattr-default/pickle probing."""
    result = _run(
        "import b2a_sdk\n"
        "try:\n"
        "    b2a_sdk.NotAThing\n"
        "except AttributeError:\n"
        "    print('attribute-error-ok')\n"
        "else:\n"
        "    raise SystemExit('expected AttributeError')\n",
        block_httpx=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "attribute-error-ok" in result.stdout


def test_lazy_http_client_still_importable_when_httpx_present():
    # The lazy PEP 562 surface must not have broken the real client import.
    pytest.importorskip("httpx")
    result = _run(
        "import b2a_sdk\n"
        "assert b2a_sdk.B2AClient is not None\n"
        "assert b2a_sdk.billable is not None\n"
        "from b2a_sdk import AgentMiddlewareClient\n"
        "assert AgentMiddlewareClient is not None\n"
        "print('client-ok')\n",
        block_httpx=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "client-ok" in result.stdout
