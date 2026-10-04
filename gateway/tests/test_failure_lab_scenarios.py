"""Contract guards for the failure-lab scenario suite.

The expensive part of the suite runs in its own CI job
(``python -m failure_lab run --tier fast``). What belongs here is the set of
properties a contributor must not be able to break without noticing:

* every scenario declares a claim, a tier, and a documented expectation for
  every configuration it runs;
* the expectation map is the guarantee the product *documents*, so a scenario
  whose observed verdict drifts away from it fails loudly rather than quietly
  being re-baselined;
* no scenario can report PASS without having implemented anything.

One scenario is executed end to end in strict trust mode, so the suite cannot
rot into a set of well-formed classes that no longer run.
"""

from __future__ import annotations

import pytest

from failure_lab.configurations import ALL_CONFIGURATIONS, LabEnvironment
from failure_lab.scenarios import (
    FAST_SCENARIO_IDS,
    SCENARIO_CLASSES,
    SLOW_SCENARIO_IDS,
    get_scenario,
)
from failure_lab.scenarios.base import Scenario, Verdict

ALL_IDS = [scenario.test_id for scenario in SCENARIO_CLASSES]


# Non-secret, 32 raw bytes, the same material the repository's own CI uses.
TEST_SIGNING_KEY = "dGVzdC1zaWduaW5nLWtleS1tYXRlcmlhbC0zMmJ5dGU="


@pytest.fixture
def strict_trust_mode(monkeypatch):
    """Run against the posture a production deployment actually boots with.

    The shared conftest opts the suite into permissive legacy MCP so older
    tests keep working, and sets no signing key. A failure lab measured under
    permissive flags, or without the signing key that makes receipts possible,
    would be measuring something nobody deploys.
    """
    from app.core.config import get_settings

    monkeypatch.setenv("TRUST_SIGNING_PRIVATE_KEY_B64", TEST_SIGNING_KEY)
    monkeypatch.setenv("TRUST_SIGNING_KEY_ID", "failure-lab-test-ed25519")
    get_settings.cache_clear()
    settings = get_settings()
    settings.TRUST_MODE_ENABLED = True
    settings.ALLOW_LEGACY_UNPERMITTED_MCP = False
    try:
        yield settings
    finally:
        get_settings.cache_clear()


def test_the_suite_covers_the_p0_test_plan():
    """Fourteen P0 tests, no gaps, no duplicates."""
    assert ALL_IDS == [f"T{index:02d}" for index in range(1, 15)]
    assert len(set(ALL_IDS)) == len(ALL_IDS)
    assert set(FAST_SCENARIO_IDS) | set(SLOW_SCENARIO_IDS) == set(ALL_IDS)
    assert not set(FAST_SCENARIO_IDS) & set(SLOW_SCENARIO_IDS)


@pytest.mark.parametrize("test_id", ALL_IDS)
def test_every_scenario_declares_what_it_tests_and_what_it_does_not(test_id):
    scenario = get_scenario(test_id)

    assert scenario.title.strip()
    assert len(scenario.claim.split()) >= 8, "a claim must be a sentence, not a label"
    assert scenario.tier in ("fast", "slow")
    assert scenario.limitations, "a scenario that claims no limitation is overclaiming"
    assert scenario.configurations, "a scenario must run against something"


@pytest.mark.parametrize("test_id", ALL_IDS)
def test_every_configuration_has_a_documented_expectation(test_id):
    """A configuration with no stated expectation cannot regress detectably."""
    scenario = get_scenario(test_id)
    expected = scenario.expected

    assert set(expected) == {c.value for c in ALL_CONFIGURATIONS}
    valid = {verdict.value for verdict in Verdict}
    assert set(expected.values()) <= valid

    for configuration in ALL_CONFIGURATIONS:
        runs = configuration in scenario.configurations
        verdict = expected[configuration.value]
        if runs:
            assert verdict != Verdict.NOT_APPLICABLE.value, (
                f"{test_id} runs {configuration.value} but documents it as not applicable"
            )
        else:
            assert verdict == Verdict.NOT_APPLICABLE.value, (
                f"{test_id} does not run {configuration.value} but documents {verdict}"
            )


@pytest.mark.parametrize("test_id", ALL_IDS)
def test_scenario_definitions_are_content_addressed(test_id):
    """A claims manifest points at a definition hash; it has to be stable."""
    first = get_scenario(test_id)
    second = get_scenario(test_id)
    assert first.definition_hash() == second.definition_hash()
    assert len(first.definition_hash()) == 64

    # An option that changes the workload must change the hash, so a published
    # claim cannot silently refer to a different test.
    altered = get_scenario(test_id, concurrency=3)
    assert altered.definition_hash() != first.definition_hash()


@pytest.mark.parametrize("test_id", ALL_IDS)
def test_every_scenario_is_actually_implemented(test_id):
    """Guards against a placeholder, or a half-written one, joining the suite.

    Checking only for a body that is exactly ``raise NotImplementedError``
    misses the more likely case: a scenario written most of the way and left
    with one unreachable-looking stub in a branch nobody exercised. That
    reports PASS for the configurations that do run and says nothing about the
    one that does not, which is worse than an obvious placeholder because it
    looks finished. So the whole method is walked for any NotImplementedError
    raise, at any depth.
    """
    import ast
    import inspect
    import textwrap

    scenario = get_scenario(test_id)
    method = type(scenario).run_configuration

    assert method is not Scenario.run_configuration, (
        f"{test_id} inherits the abstract body"
    )

    source = textwrap.dedent(inspect.getsource(method))
    tree = ast.parse(source)
    stubs = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Raise)
        and (
            (isinstance(node.exc, ast.Name) and node.exc.id == "NotImplementedError")
            or (
                isinstance(node.exc, ast.Call)
                and isinstance(node.exc.func, ast.Name)
                and node.exc.func.id == "NotImplementedError"
            )
        )
    ]
    assert not stubs, (
        f"{test_id} still raises NotImplementedError inside run_configuration "
        f"(relative line(s) {stubs}) -- it is a placeholder or half written"
    )


def test_the_expected_map_records_the_known_business_level_gap():
    """T06 is the test the PRD says must exist even though it fails today.

    An agent that restarts and mints a fresh idempotency key for the same
    business operation is not covered by a guarantee keyed on that key. If a
    future change makes this pass, that is good news — and it must be
    accompanied by a documentation change, which is why this assertion exists.
    """
    scenario = get_scenario("T06")
    assert scenario.expected["C_gateway_with_native_idempotency"] == Verdict.FAIL.value
    assert scenario.expected["D_gateway_naive_downstream"] == Verdict.FAIL.value
    assert scenario.expected["B_direct_native_idempotency"] == Verdict.PASS.value


@pytest.mark.anyio
async def test_a_scenario_runs_end_to_end_and_matches_its_documentation(
    tmp_path, clean_database, strict_trust_mode
):
    """Execute the headline scenario for real, in the deployed trust posture."""
    from app.main import app

    scenario = get_scenario("T03")
    env = LabEnvironment(run_dir=tmp_path, app=app, admin_api_key="test-key")
    result = await scenario.run(env)

    assert result.verdict is not Verdict.ERROR, [
        entry.error for entry in result.configurations if entry.error
    ]
    assert result.matches_expectation, result.mismatches()

    for entry in result.configurations:
        assert entry.observation.strip(), f"{entry.configuration} reported nothing"
        # Downstream effects are counted by the tool's own ledger; a scenario
        # that reports them without any being observed is not measuring.
        if entry.verdict in (Verdict.PASS.value, Verdict.OBSERVED.value):
            assert entry.counters.incoming_requests > 0

    # The independent instruments, not the gateway's own tables, are what make
    # this evidence. Both must have recorded something for the governed runs.
    governed = [
        entry
        for entry in result.configurations
        if entry.configuration.startswith(("C_", "D_"))
    ]
    assert governed
    for entry in governed:
        assert entry.downstream_effects, "the effect ledger recorded nothing"
        assert entry.crossings, "the fault layer observed no request crossing it"
        assert entry.counters.downstream_executions == 1
        assert entry.counters.gateway_dispatches == 1
