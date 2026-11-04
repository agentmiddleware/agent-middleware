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

import dataclasses

import httpx
import pytest

from failure_lab.configurations import (
    ALL_CONFIGURATIONS,
    Configuration,
    DirectAgent,
    LabEnvironment,
)
from failure_lab.effect_ledger import EffectLedger
from failure_lab.faults import FaultInjector
from failure_lab.gateway import DOWNSTREAM_BASE_URL
from failure_lab.refund_tool import SimulatedRefundService, build_downstream_app
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


# --------------------------------------------------------------------------- #
# The target safety fuse                                                        #
# --------------------------------------------------------------------------- #

DIRECT_CONFIGURATIONS = (Configuration.DIRECT_NAIVE, Configuration.DIRECT_NATIVE)


def _simulated_downstream(tmp_path, name):
    service = SimulatedRefundService(
        EffectLedger(tmp_path / f"{name}.db"),
        native_idempotency=False,
        configuration=name,
    )
    return build_downstream_app(
        service, FaultInjector(), bearer_token="bearer", control_token="control"
    )


@pytest.mark.anyio
async def test_the_fuse_admits_only_the_agents_own_simulated_tool(tmp_path):
    """Identity, not configuration: only the very tool the agent was built for.

    Every refusal case below changes where the client's requests actually go
    (or what the downstream really is), not some attribute beside them.
    """
    own = _simulated_downstream(tmp_path, "own")
    twin = _simulated_downstream(tmp_path, "twin")
    agent = DirectAgent(Configuration.DIRECT_NAIVE, own)
    original = agent._client
    try:
        assert agent.targets_in_process_downstream(own)
        # Same shape, separately built: a configuration match is not enough.
        assert not agent.targets_in_process_downstream(twin)
        # The right app, but not the lab's simulated service behind it.
        assert not agent.targets_in_process_downstream(
            dataclasses.replace(own, service=object())
        )
        # The client itself replaced by one on a real network transport, which
        # is what a real refund processor would sit behind.
        agent._client = httpx.AsyncClient(transport=httpx.AsyncHTTPTransport())
        assert not agent.targets_in_process_downstream(own)
        await agent._client.aclose()
        # The right transport, but a mount that routes some URLs elsewhere.
        agent._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=own.asgi),
            mounts={"https://": httpx.AsyncHTTPTransport()},
        )
        assert not agent.targets_in_process_downstream(own)
        await agent._client.aclose()
    finally:
        agent._client = original
        await agent.aclose()


@pytest.mark.anyio
async def test_the_fuse_refuses_a_direct_agent_subclass(tmp_path):
    """A future adapter is most naturally a DirectAgent subclass. Refuse it.

    This subclass changes nothing at all, so its client is still bound to the
    lab's own tool -- and it is refused anyway. A real adapter would reuse
    DirectAgent.submit and swap the client or the method under it, which the
    transport check alone cannot be trusted to see.
    """
    from failure_lab.configurations import (
        Target,
        UnsafeTargetError,
        require_in_process_downstream,
    )

    class AdapterAgent(DirectAgent):
        pass

    downstream = _simulated_downstream(tmp_path, "subclass")
    agent = AdapterAgent(Configuration.DIRECT_NAIVE, downstream)
    target = Target(
        configuration=Configuration.DIRECT_NAIVE,
        ledger=downstream.service.ledger,
        injector=downstream.injector,
        downstream=downstream,
        agent=agent,
    )
    try:
        assert agent.targets_in_process_downstream(downstream)
        with pytest.raises(UnsafeTargetError, match="refused before any call"):
            require_in_process_downstream(target, scenario="T02", hazard="probe")
    finally:
        await agent.aclose()


@pytest.mark.anyio
async def test_the_fuse_refuses_t02_before_any_call_against_a_foreign_tool(
    tmp_path, monkeypatch
):
    """The property the fuse exists for.

    On a direct configuration nothing stands between T02 and the tool, and
    under A_direct_naive nothing refuses its 100x payload at all. Repointed at
    anything but the lab's simulated tool, the arm must not send a single
    request. The foreign transport records every request it is handed and
    answers 201, so a fuse that let one through cannot pass quietly.
    """
    sent: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(201, json={"refund_id": "re_real", "replayed": False})

    def foreign_init(self, configuration, downstream):
        self.configuration = configuration
        self._client = httpx.AsyncClient(
            transport=httpx.MockTransport(record),
            base_url=DOWNSTREAM_BASE_URL,
            headers={"Authorization": f"Bearer {downstream.bearer_token}"},
        )

    monkeypatch.setattr(DirectAgent, "__init__", foreign_init)
    env = LabEnvironment(run_dir=tmp_path, app=object(), admin_api_key="unused")

    result = await get_scenario("T02").run(env, configurations=DIRECT_CONFIGURATIONS)

    assert sent == [], "the fuse let a request leave the agent"
    assert len(result.configurations) == len(DIRECT_CONFIGURATIONS)
    for entry in result.configurations:
        assert entry.verdict == Verdict.ERROR, entry.observation
        # Named as the fuse, not buried as an opaque ExceptionGroup: the arm
        # runs inside the downstream's task group, which wraps the refusal.
        assert entry.observation.startswith("target safety fuse: ")
        assert "refused before any call" in entry.observation
        assert "no gateway" in entry.observation
        ledger = EffectLedger(tmp_path / f"effects-{entry.configuration}-T02.db")
        assert ledger.execution_count() == 0


def test_error_observations_unwrap_groups_and_keep_plain_wording():
    """A wrapped refusal is named; every other harness error reads as before."""
    from failure_lab.configurations import UnsafeTargetError
    from failure_lab.scenarios.base import _error_observation

    assert _error_observation(ValueError("boom")) == "harness error: ValueError: boom"
    refused = ExceptionGroup("tg", [ExceptionGroup("inner", [UnsafeTargetError("no")])])
    assert _error_observation(refused) == "target safety fuse: no"
    mixed = ExceptionGroup("tg", [ValueError("a"), KeyError("b")])
    assert _error_observation(mixed) == "harness error: ValueError: a; KeyError: 'b'"


@pytest.mark.anyio
async def test_t02_direct_arms_still_measure_and_record_the_fuse(tmp_path):
    """Against the lab's own tool the fuse is invisible to the measurement.

    It must not change a verdict, and it must show in the evidence rather than
    only in the source.
    """
    scenario = get_scenario("T02")
    env = LabEnvironment(run_dir=tmp_path, app=object(), admin_api_key="unused")

    result = await scenario.run(env, configurations=DIRECT_CONFIGURATIONS)

    assert len(result.configurations) == len(DIRECT_CONFIGURATIONS)
    for entry in result.configurations:
        assert entry.verdict != Verdict.ERROR, entry.error
        assert Verdict(entry.verdict).value == scenario.expected[entry.configuration]
        assert "target safety fuse" in entry.extra["target_safety_fuse"]
