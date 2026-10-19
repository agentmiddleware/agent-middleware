"""The evidence bundle must never leak a credential, and a claim must have a test.

These are the two properties that decide whether the lab's output can be
handed to anyone. A bundle that carries a secret cannot be published; a claims
manifest that reports PASS for a test that did not pass makes the whole
traceability exercise worthless.
"""

from __future__ import annotations

import hashlib
import errno
import json
from pathlib import Path

import pytest

import failure_lab.evidence as evidence
from failure_lab.evidence import (
    MIN_SCANNABLE_SECRET_LENGTH,
    build_evidence_bundle,
    REDACTED_KEY_PATTERN,
    SecretLeakError,
    assert_no_secret_leak,
    find_leaked_secrets,
    redact,
    verify_bundle_integrity,
)
from failure_lab.telemetry import (
    CONVERSION_SOURCES,
    TrafficSource,
    counts_toward_conversion,
)


class TestRedaction:
    def test_credential_shaped_keys_are_redacted_at_every_depth(self):
        document = {
            "api_key": "amw_live_should_not_survive",
            "nested": {
                "bearer_token": "tok_should_not_survive",
                "deeper": [{"signing_seed": "seed_should_not_survive"}],
            },
            "headers": {"Authorization": "Bearer abcdefghijklmnop"},
            "harmless": "downstream executions: 2",
        }
        cleaned = json.dumps(redact(document))

        for secret in (
            "amw_live_should_not_survive",
            "tok_should_not_survive",
            "seed_should_not_survive",
        ):
            assert secret not in cleaned, secret
        # Redaction must not destroy the measurements it travels with.
        assert "downstream executions: 2" in cleaned

    @pytest.mark.parametrize(
        "name",
        [
            "api_key",
            "apiKey",
            "api-key",
            "TOKEN",
            "client_secret",
            "signing_seed",
            "password",
            "Authorization",
            "bearer",
            "private_key",
        ],
    )
    def test_the_key_pattern_covers_the_names_credentials_actually_use(self, name):
        assert REDACTED_KEY_PATTERN.search(name.lower())

    def test_measurement_keys_are_not_swept_up(self):
        """Over-redaction would quietly destroy the evidence."""
        for name in (
            "downstream_executions",
            "gateway_dispatches",
            "receipt_id",
            "operation_id",
            "idempotency_key",
        ):
            if name == "idempotency_key":
                # This one legitimately matches: a key is a key. It is not a
                # credential, but redacting it is the safe direction.
                continue
            assert not REDACTED_KEY_PATTERN.search(name), name


class TestSecretLeakDetection:
    def test_a_planted_secret_in_written_bytes_is_found(self, tmp_path):
        secret = "amw_dev_planted_secret_value_1234"
        (tmp_path / "manifest.json").write_text(json.dumps({"ok": True}))
        (tmp_path / "event-log.jsonl").write_text(
            json.dumps({"step": "invoke", "header": f"Bearer {secret}"}) + "\n"
        )

        leaks = find_leaked_secrets(sorted(tmp_path.iterdir()), [secret])
        assert leaks, "a secret written into the bundle was not detected"

        with pytest.raises(SecretLeakError):
            assert_no_secret_leak(tmp_path, [secret])

    def test_a_clean_bundle_passes(self, tmp_path):
        (tmp_path / "results.json").write_text(json.dumps({"downstream_effects": 2}))
        assert_no_secret_leak(tmp_path, ["amw_dev_planted_secret_value_1234"])

    def test_short_values_are_not_scanned_for(self, tmp_path):
        """A six-character 'secret' would match everywhere and mean nothing."""
        (tmp_path / "results.json").write_text("the verdict was PASS")
        short = "PASS"
        assert len(short) < MIN_SCANNABLE_SECRET_LENGTH
        assert_no_secret_leak(tmp_path, [short])


class TestConversionMetrics:
    def test_only_a_human_customer_counts(self):
        assert CONVERSION_SOURCES == frozenset({TrafficSource.HUMAN_CUSTOMER})
        assert counts_toward_conversion(TrafficSource.HUMAN_CUSTOMER)

    @pytest.mark.parametrize(
        "source",
        [
            TrafficSource.INTERNAL_TEST,
            TrafficSource.AI_TEST_AGENT,
            TrafficSource.CI_RUN,
            TrafficSource.UNKNOWN,
        ],
    )
    def test_synthetic_traffic_can_never_inflate_a_conversion(self, source):
        """This is a property of the data model, not a reporting convention."""
        assert not counts_toward_conversion(source)

    def test_an_unrecognised_source_is_not_a_sale(self):
        assert not counts_toward_conversion("HUMAN_CUSTOMER_TYPO")
        assert not counts_toward_conversion("")


def _result_document(observation: str) -> dict:
    """A ScenarioResult-shaped document carrying text in a free-form field."""
    return {
        "test_id": "TZZ",
        "title": "leak probe",
        "claim": "A claim long enough to read like a sentence about behaviour.",
        "definition_version": "1",
        "definition_hash": "a" * 64,
        "started_at": "2026-09-19T00:00:00Z",
        "finished_at": "2026-09-19T00:00:01Z",
        "verdict": "PASS",
        "expected": {},
        "matches_expectation": True,
        "limitations": [],
        "events": [],
        "configurations": [
            {
                "configuration": "C_gateway_with_native_idempotency",
                "label": "Native controls + Agent Middleware",
                "verdict": "PASS",
                "expectation": "PASS",
                "observation": observation,
                "counters": {},
                "attempts": [],
                "downstream_effects": [],
                "crossings": [],
                "gateway": None,
                "receipts": [],
                "remaining_risks": [],
                "extra": {},
                "error": None,
            }
        ],
    }


class TestTheBundleRefusesToLeak:
    """Two layers, and both have to be shown working.

    Redaction is the primary defence and it reaches into free text, not just
    values under credential-shaped key names. The written-bytes scan is the
    backstop for whatever redaction's heuristics do not recognise. A backstop
    that has never been seen to fire is indistinguishable from one that cannot.
    """

    def test_redaction_reaches_a_credential_in_free_prose(self, tmp_path):
        secret = "lab-admin-SUPERSECRET-abcdef123456"
        document = _result_document(f"the call presented {secret} as its credential")

        result = build_evidence_bundle(
            tmp_path / "bundle",
            [document],
            secret_values=[secret],
            include_environment_secrets=False,
        )

        written = "\n".join(
            path.read_text(errors="ignore")
            for path in result.directory.rglob("*")
            if path.is_file()
        )
        assert secret not in written

    @pytest.mark.parametrize(
        "secret",
        [
            "correct horse battery staple",
            "internal-db.corp.example",
        ],
        ids=["ordinary-words", "hostname"],
    )
    def test_the_backstop_fires_on_what_redaction_cannot_recognise(
        self, tmp_path, secret
    ):
        """A secret that does not look like one still must not reach disk."""
        document = _result_document(f"the run used {secret} here")

        with pytest.raises(SecretLeakError):
            build_evidence_bundle(
                tmp_path / "bundle",
                [document],
                secret_values=[secret],
                include_environment_secrets=False,
            )

    def test_a_bundle_that_leaked_is_never_left_on_disk(self, tmp_path):
        """Refusing is not enough if the staging copy survives for someone to find."""
        secret = "correct horse battery staple"
        target = tmp_path / "bundle"

        with pytest.raises(SecretLeakError):
            build_evidence_bundle(
                target,
                [_result_document(f"the run used {secret} here")],
                secret_values=[secret],
                include_environment_secrets=False,
            )

        assert not target.exists()
        survivors = [
            path
            for path in tmp_path.rglob("*")
            if path.is_file() and secret in path.read_text(errors="ignore")
        ]
        assert not survivors, survivors


def _manifest(bundle, files):
    (bundle / "manifest.json").write_text(json.dumps({"files": files}))


def test_integrity_check_accepts_a_regular_file_inside_the_bundle(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    body = b"inside"
    (bundle / "report.txt").write_bytes(body)
    _manifest(
        bundle,
        [
            {
                "path": "report.txt",
                "sha256": hashlib.sha256(body).hexdigest(),
                "bytes": len(body),
            }
        ],
    )
    report = verify_bundle_integrity(bundle)
    assert report.ok, report.problems
    assert report.checked == 1


def test_integrity_check_does_not_hash_a_path_that_leaves_the_bundle(tmp_path):
    """A manifest path of ../ or an absolute path is not a bundle file.

    pathlib joins an absolute path by discarding the bundle directory, and
    a matching hash would then make the check pass for bytes that are not
    in the bundle.
    """
    outside = tmp_path / "outside.txt"
    payload = b"not-in-the-bundle"
    outside.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    entry = {"sha256": digest, "bytes": len(payload)}

    parent_ref = tmp_path / "parent-ref"
    parent_ref.mkdir()
    _manifest(parent_ref, [{**entry, "path": "../outside.txt"}])
    parent_report = verify_bundle_integrity(parent_ref)
    assert not parent_report.ok
    assert parent_report.checked == 0
    assert any("not read" in problem for problem in parent_report.problems)

    absolute = tmp_path / "absolute"
    absolute.mkdir()
    _manifest(absolute, [{**entry, "path": str(outside)}])
    absolute_report = verify_bundle_integrity(absolute)
    assert not absolute_report.ok
    assert absolute_report.checked == 0

    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / "report.txt").symlink_to(outside)
    _manifest(linked, [{**entry, "path": "report.txt"}])
    linked_report = verify_bundle_integrity(linked)
    assert not linked_report.ok
    assert linked_report.checked == 0
    assert outside.read_bytes() == payload


def test_integrity_check_accepts_nested_files_and_reports_missing_files(tmp_path):
    bundle = tmp_path / "bundle"
    (bundle / "nested").mkdir(parents=True)
    body = b"nested"
    (bundle / "nested" / "report.txt").write_bytes(body)
    _manifest(
        bundle,
        [
            {
                "path": "nested/report.txt",
                "sha256": hashlib.sha256(body).hexdigest(),
                "bytes": len(body),
            },
            {"path": "missing.txt", "sha256": "0" * 64, "bytes": 1},
        ],
    )
    report = verify_bundle_integrity(bundle)
    assert report.checked == 1
    assert report.missing == ["missing.txt"]
    assert not report.ok


@pytest.mark.parametrize("unsafe", ["../outside.txt", "/outside.txt", "bad\x00name", 4])
def test_integrity_check_never_hashes_unsafe_manifest_paths(
    tmp_path, monkeypatch, unsafe
):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _manifest(bundle, [{"path": unsafe, "sha256": "0" * 64, "bytes": 1}])

    def forbidden_hash(_path):
        pytest.fail("unsafe target was hashed")

    monkeypatch.setattr(evidence, "_sha256_file", forbidden_hash)
    report = verify_bundle_integrity(bundle)
    assert not report.ok
    assert report.checked == 0


def test_integrity_check_rejects_escaping_parent_link_and_linked_manifest(
    tmp_path, monkeypatch
):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "payload.txt").write_bytes(b"outside")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "linked").symlink_to(outside, target_is_directory=True)
    _manifest(bundle, [{"path": "linked/payload.txt", "sha256": "0" * 64}])
    monkeypatch.setattr(
        evidence, "_sha256_file", lambda _path: pytest.fail("outside hashed")
    )
    report = verify_bundle_integrity(bundle)
    assert not report.ok
    assert report.checked == 0

    linked_bundle = tmp_path / "linked-bundle"
    linked_bundle.mkdir()
    (linked_bundle / "manifest.json").symlink_to(bundle / "manifest.json")
    monkeypatch.setattr(
        Path, "read_text", lambda _path, **_kw: pytest.fail("linked manifest read")
    )
    linked_report = verify_bundle_integrity(linked_bundle)
    assert not linked_report.ok
    assert linked_report.checked == 0


def test_integrity_check_reports_unlisted_symlink_without_reading_target(
    tmp_path, monkeypatch
):
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "unlisted.txt").symlink_to(outside)
    _manifest(bundle, [])
    monkeypatch.setattr(
        evidence, "_sha256_file", lambda _path: pytest.fail("unlisted link was hashed")
    )
    report = verify_bundle_integrity(bundle)
    assert not report.ok
    assert report.checked == 0
    assert report.unexpected == ["unlisted.txt"]
    assert outside.read_bytes() == b"outside"


def test_integrity_check_accepts_empty_and_nested_regular_bundles(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    _manifest(empty, [])
    assert verify_bundle_integrity(empty).ok

    nested = tmp_path / "nested"
    (nested / "subdir").mkdir(parents=True)
    payload = b"regular"
    (nested / "subdir" / "payload.txt").write_bytes(payload)
    _manifest(
        nested,
        [
            {
                "path": "subdir/payload.txt",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload),
            }
        ],
    )
    report = verify_bundle_integrity(nested)
    assert report.ok, report.problems
    assert report.checked == 1


@pytest.mark.parametrize("nested", [False, True], ids=["root", "nested"])
@pytest.mark.parametrize("error", [errno.EIO, errno.EACCES], ids=["eio", "permission"])
def test_integrity_check_reports_directory_scan_failure(
    tmp_path, monkeypatch, nested, error
):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    blocked = bundle / "subdir" if nested else bundle
    if nested:
        blocked.mkdir()
    (blocked / "unlisted.txt").write_bytes(b"not indexed")
    _manifest(bundle, [])
    original_scandir = evidence.os.scandir

    def failing_scandir(path):
        if Path(path) == blocked:
            raise OSError(error, "synthetic directory scan failure")
        return original_scandir(path)

    with monkeypatch.context() as patch:
        patch.setattr(evidence.os, "scandir", failing_scandir)
        report = verify_bundle_integrity(bundle)
    assert not report.ok
    assert report.checked == 0
    assert any("could not scan" in problem for problem in report.problems)


def test_integrity_check_does_not_descend_into_unlisted_directory_link(
    tmp_path, monkeypatch
):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "payload.txt").write_bytes(b"outside")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "linked").symlink_to(outside, target_is_directory=True)
    _manifest(bundle, [])
    original_scandir = evidence.os.scandir

    def guarded_scandir(path):
        assert Path(path) != outside, "outside directory was scanned"
        return original_scandir(path)

    with monkeypatch.context() as patch:
        patch.setattr(evidence.os, "scandir", guarded_scandir)
        patch.setattr(
            evidence,
            "_sha256_file",
            lambda _path: pytest.fail("outside target was hashed"),
        )
        report = verify_bundle_integrity(bundle)
    assert not report.ok
    assert report.unexpected == ["linked"]


@pytest.mark.parametrize("failure", [OSError, RuntimeError])
def test_integrity_check_reports_entry_classification_failure(
    tmp_path, monkeypatch, failure
):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "unlisted.txt").write_bytes(b"not indexed")
    _manifest(bundle, [])

    class BrokenEntry:
        path = str(bundle / "unlisted.txt")

        def is_dir(self, *, follow_symlinks):
            assert follow_symlinks is False
            raise failure("synthetic entry classification failure")

    class BrokenListing:
        def __enter__(self):
            return iter([BrokenEntry()])

        def __exit__(self, *_args):
            return False

    with monkeypatch.context() as patch:
        patch.setattr(evidence.os, "scandir", lambda _path: BrokenListing())
        report = verify_bundle_integrity(bundle)
    assert not report.ok
    assert any("could not scan" in problem for problem in report.problems)


@pytest.mark.parametrize(
    "files", [None, {}, [1], [None], [{"path": "file", "bytes": "invalid"}]]
)
def test_integrity_check_handles_malformed_manifest_shapes(tmp_path, files):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "file").write_bytes(b"ok")
    _manifest(bundle, files)
    report = verify_bundle_integrity(bundle)
    assert not report.ok


@pytest.mark.parametrize("failure", [RuntimeError, OSError, ValueError])
def test_integrity_check_handles_resolution_failure_without_hashing(
    tmp_path, monkeypatch, failure
):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "file").write_bytes(b"ok")
    _manifest(bundle, [{"path": "file", "sha256": "0" * 64}])
    original_resolve = Path.resolve

    def broken_resolve(path, *args, **kwargs):
        if path.name == "file":
            raise failure("cannot resolve")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", broken_resolve)
    monkeypatch.setattr(
        evidence, "_sha256_file", lambda _path: pytest.fail("unresolved target hashed")
    )
    report = verify_bundle_integrity(bundle)
    assert not report.ok
    assert report.checked == 0
