"""A downloaded archive must retain checkable signatures without asserting trust."""

import argparse
import base64
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from failure_lab.cli import command_verify
from failure_lab.evidence import (
    SecretLeakError,
    build_evidence_bundle,
    verify_bundle_integrity,
)


@pytest.fixture
def signed_receipt():
    private = Ed25519PrivateKey.generate()
    kid = "synthetic-public-proof-key"
    signing_input = json.dumps(
        {"kid": kid, "alg": "Ed25519", "receipt_id": "receipt-1", "outcome": "success"},
        sort_keys=True,
        separators=(",", ":"),
    )
    receipt = {
        "receipt_id": "receipt-1",
        "kid": kid,
        "signing_input": signing_input,
        "signature": base64.b64encode(private.sign(signing_input.encode())).decode(),
    }
    keys = {
        "keys": [
            {
                "kid": kid,
                "alg": "Ed25519",
                "public_key_b64": base64.b64encode(
                    private.public_key().public_bytes_raw()
                ).decode(),
            }
        ]
    }
    return receipt, keys


def test_downloaded_archive_checks_signatures_without_establishing_issuer_trust(
    tmp_path, capsys, signed_receipt
):
    receipt, keys = signed_receipt
    bundle = build_evidence_bundle(
        tmp_path / "run" / "evidence",
        [],
        receipts=[receipt],
        trust_keys=keys,
        archive=True,
        include_environment_secrets=False,
    )
    with zipfile.ZipFile(bundle.archive_path) as archive:
        archive.extractall(tmp_path / "downloaded")
    downloaded = tmp_path / "downloaded" / "evidence"
    assert not (downloaded.parent / "trust-keys.json").exists()
    assert (
        command_verify(argparse.Namespace(bundle=downloaded, keys=None, as_json=True))
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["signatures_valid"] == 1
    claims = {
        entry["claim"]: entry["status"] for entry in report["verification"][0]["claims"]
    }
    assert claims["ISSUER_TRUST_ESTABLISHED"] == "NOT_ESTABLISHED"
    assert claims["DOWNSTREAM_EXECUTION_ESTABLISHED"] == "NOT_ESTABLISHED"

    (downloaded / "trust-keys.json").write_text('{"keys": []}')
    assert not verify_bundle_integrity(downloaded).ok
    assert (
        command_verify(argparse.Namespace(bundle=downloaded, keys=None, as_json=True))
        == 1
    )


def test_public_key_document_is_subject_to_the_written_bytes_secret_scan(tmp_path):
    secret = "correct horse battery staple"
    with pytest.raises(SecretLeakError):
        build_evidence_bundle(
            tmp_path / "evidence",
            [],
            trust_keys={"annotation": secret},
            secret_values=[secret],
            include_environment_secrets=False,
            archive=True,
        )
    assert not list(tmp_path.rglob("trust-keys.json"))
    assert not list(tmp_path.rglob("*.zip"))


_ARCHIVE_DRIVER = """
import json, sys
from pathlib import Path

surface, output = sys.argv[1], Path(sys.argv[2])
if surface == "cli":
    from failure_lab.cli import main
    assert main(["run", "--test", "T10", "--archive", "--no-telemetry",
                 "--output", str(output)]) == 0
else:
    from starlette.testclient import TestClient
    from failure_lab.diagnostic import create_app, DiagnosticSettings
    # T10 exports receipts; the public default currently selects T02/T03/T06.
    settings = DiagnosticSettings(telemetry=False, state_dir=output, allowed_scenarios=("T10",))
    with TestClient(create_app(settings)) as client:
        started = client.post("/diagnostic/run", json={"scenarios": ["T10"]})
        assert started.status_code == 202, started.status_code
        run_id = started.json()["run_id"]
        with client.stream("GET", f"/diagnostic/run/{run_id}/events") as stream:
            for line in stream.iter_lines():
                assert line != "event: failed", "diagnostic failed"
                if line == "event: complete":
                    break
        downloaded = client.get(f"/diagnostic/run/{run_id}/evidence.zip")
        assert downloaded.status_code == 200, downloaded.status_code
        (output / "evidence.zip").write_bytes(downloaded.content)
"""


@pytest.mark.parametrize("surface", ["cli", "diagnostic"])
def test_real_run_archive_reverifies_away_from_its_run_directory(
    tmp_path, capsys, surface
):
    """Fresh process, real T10 and ASGI services, no socket or external target."""
    output = tmp_path / "run"
    completed = subprocess.run(
        [sys.executable, "-c", _ARCHIVE_DRIVER, surface, str(output)],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    with zipfile.ZipFile(output / "evidence.zip") as archive:
        archive.extractall(tmp_path / "downloaded")
    assert (
        command_verify(
            argparse.Namespace(
                bundle=tmp_path / "downloaded" / "evidence", keys=None, as_json=True
            )
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["signatures_valid"] == report["receipts_found"] > 0
    for receipt in report["verification"]:
        claims = {entry["claim"]: entry["status"] for entry in receipt["claims"]}
        assert claims["ISSUER_TRUST_ESTABLISHED"] == "NOT_ESTABLISHED"
