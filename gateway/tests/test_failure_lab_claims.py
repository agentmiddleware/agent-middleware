"""A public claim must have a passing, still-documented test behind it.

The point of a machine-readable claims manifest is that marketing copy can be
refused mechanically rather than by review. That only works if the refusal is
strict in the two directions that matter: a claim whose test did not pass, and
a claim whose test passed but surprised its own documentation. The second is
the subtle one — a scenario that diverged from its expectation has not yet
been re-documented, and shipping the claim in that window is how a manifest
stops meaning anything.
"""

from __future__ import annotations

import json

import pytest

from failure_lab.claims import (
    SUPPORTING_STATUS,
    UnsupportedClaimError,
    assert_claim_is_supported,
    assert_claims_are_supported,
    build_claims_manifest,
    find_claim,
    load_claims_manifest,
    normalize_claim,
    render_markdown,
    supported_claims,
)
from failure_lab.configurations import CONFIGURATION_LABELS, Configuration
from failure_lab.scenarios.base import (
    ConfigurationResult,
    Counters,
    ScenarioResult,
    Verdict,
)

CLAIM = (
    "One accepted idempotency key admits at most one gateway dispatch and one debit."
)


def _scenario(
    test_id: str,
    *,
    claim: str = CLAIM,
    observed: Verdict = Verdict.PASS,
    documented: Verdict = Verdict.PASS,
) -> ScenarioResult:
    entry = ConfigurationResult(
        configuration=Configuration.GATEWAY_NATIVE.value,
        label=CONFIGURATION_LABELS[Configuration.GATEWAY_NATIVE],
        verdict=observed,
        expectation=documented.value,
        observation="synthetic",
        counters=Counters(incoming_requests=1, downstream_executions=1),
    )
    return ScenarioResult(
        test_id=test_id,
        title=f"Scenario {test_id}",
        claim=claim,
        definition_version="2026.09.1",
        definition_hash="a" * 64,
        started_at="2026-09-19T00:00:00Z",
        finished_at="2026-09-19T00:00:01Z",
        configurations=[entry],
        limitations=["does not prove exactly-once downstream execution"],
        events=[],
        expected={Configuration.GATEWAY_NATIVE.value: documented.value},
    )


def test_a_passing_test_supports_its_claim():
    manifest = build_claims_manifest([_scenario("T01")], environment="local")
    record = assert_claim_is_supported(manifest, CLAIM)

    assert record.test_id == "T01"
    assert record.status == SUPPORTING_STATUS
    assert record.limitations, "a claim that carries no limitation is overclaiming"
    assert CLAIM in supported_claims(manifest)


def test_a_claim_nobody_tested_is_refused():
    manifest = build_claims_manifest([_scenario("T01")], environment="local")
    with pytest.raises(UnsupportedClaimError):
        assert_claim_is_supported(
            manifest, "The gateway guarantees exactly-once refunds."
        )


def test_a_failing_test_cannot_support_a_claim():
    manifest = build_claims_manifest(
        [_scenario("T06", observed=Verdict.FAIL, documented=Verdict.FAIL)],
        environment="local",
    )
    with pytest.raises(UnsupportedClaimError):
        assert_claim_is_supported(manifest, CLAIM)
    assert supported_claims(manifest) == []


def test_a_pass_that_surprised_its_documentation_is_still_refused():
    """The subtle case: status reads PASS, but the docs said FAIL."""
    manifest = build_claims_manifest(
        [_scenario("T06", observed=Verdict.PASS, documented=Verdict.FAIL)],
        environment="local",
    )
    record = find_claim(manifest, CLAIM)
    assert record is not None
    assert record.status == SUPPORTING_STATUS
    assert not record.matches_documented_expectation
    assert not record.supports_a_public_claim

    with pytest.raises(UnsupportedClaimError):
        assert_claim_is_supported(manifest, CLAIM)


def test_claim_matching_ignores_only_incidental_differences():
    manifest = build_claims_manifest([_scenario("T01")], environment="local")
    assert assert_claim_is_supported(manifest, CLAIM.upper() + "   ")
    assert assert_claim_is_supported(manifest, CLAIM.rstrip("."))
    assert normalize_claim("  A  b. ") == normalize_claim("a B")

    # A claim that differs by a word is a different claim.
    with pytest.raises(UnsupportedClaimError):
        assert_claim_is_supported(manifest, CLAIM.replace("at most one", "exactly one"))


def test_a_whole_page_of_claims_reports_every_failure_at_once():
    manifest = build_claims_manifest([_scenario("T01")], environment="local")
    with pytest.raises(UnsupportedClaimError) as caught:
        assert_claims_are_supported(
            manifest, [CLAIM, "Unbacked claim one.", "Unbacked claim two."]
        )
    message = str(caught.value)
    assert "Unbacked claim one" in message
    assert "Unbacked claim two" in message


def test_the_manifest_carries_the_prd_fields_and_serialises():
    manifest = build_claims_manifest(
        [_scenario("T01")], environment="local", version="1.3.0"
    )
    document = json.loads(json.dumps(manifest.as_dict()))
    record = document["claims"][0]

    for field in (
        "claim",
        "test_id",
        "version",
        "status",
        "environment",
        "tested_at",
        "limitations",
    ):
        assert field in record, field
    assert record["version"] == "1.3.0"
    assert record["definition_hash"]
    assert "matches_documented_expectation" in record

    table = render_markdown(manifest)
    assert "T01" in table and "|" in table


@pytest.mark.parametrize("row_verdict", [Verdict.FAIL, Verdict.ERROR])
def test_loaded_manifest_cannot_promote_negative_rows_to_pass(tmp_path, row_verdict):
    manifest = build_claims_manifest(
        [_scenario("T01", observed=row_verdict, documented=row_verdict)],
        environment="local",
    )
    document = manifest.as_dict()
    # A stale or edited summary disagrees with its own observed rows.
    document["claims"][0]["status"] = "PASS"
    path = tmp_path / "claims.json"
    path.write_text(json.dumps(document))
    loaded = load_claims_manifest(path)
    record = loaded.records[0]
    assert record.status == row_verdict.value
    assert record.matches_documented_expectation
    assert any("configuration row observed" in line for line in record.limitations)
    with pytest.raises(UnsupportedClaimError):
        assert_claim_is_supported(loaded, CLAIM)
    assert supported_claims(loaded) == []


@pytest.mark.parametrize("documented", [Verdict.PASS, Verdict.FAIL])
def test_loaded_manifest_keeps_passing_rows_and_expectation_gate(tmp_path, documented):
    manifest = build_claims_manifest(
        [_scenario("T01", observed=Verdict.PASS, documented=documented)],
        environment="local",
    )
    path = tmp_path / "claims.json"
    path.write_text(json.dumps(manifest.as_dict()))
    loaded = load_claims_manifest(path)
    assert loaded.records[0].status == "PASS"
    if documented == Verdict.PASS:
        assert assert_claim_is_supported(loaded, CLAIM)
    else:
        with pytest.raises(UnsupportedClaimError):
            assert_claim_is_supported(loaded, CLAIM)
