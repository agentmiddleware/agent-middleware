"""The P0 scenario suite.

Each module holds one scenario: its identity, the claim it tests, the
configurations it runs against, and the verdict expected of each of them. The
``expected`` map encodes the guarantee the product *documents*. A run that
disagrees with it is a finding to report, never a reason to edit the map.
"""

from __future__ import annotations

from failure_lab.scenarios.base import (
    ConfigurationResult,
    Counters,
    EventLog,
    Measurements,
    Scenario,
    ScenarioResult,
    Verdict,
)
from failure_lab.scenarios.t01_concurrent_identical_retry import (
    ConcurrentIdenticalRetry,
)
from failure_lab.scenarios.t02_same_key_different_arguments import (
    SameKeyDifferentArguments,
)
from failure_lab.scenarios.t03_execute_then_lose_response import ExecuteThenLoseResponse
from failure_lab.scenarios.t04_crash_before_dispatch import CrashBeforeDispatch
from failure_lab.scenarios.t05_crash_after_dispatch import CrashAfterDispatch
from failure_lab.scenarios.t06_agent_restart_new_key import AgentRestartNewKey
from failure_lab.scenarios.t07_concurrent_budget_race import ConcurrentBudgetRace
from failure_lab.scenarios.t08_permit_revocation_race import PermitRevocationRace
from failure_lab.scenarios.t09_forbidden_parameter import ForbiddenParameter
from failure_lab.scenarios.t10_receipt_tampering import ReceiptTampering
from failure_lab.scenarios.t11_database_restart import DatabaseRestart
from failure_lab.scenarios.t12_cache_failure import CacheFailure
from failure_lab.scenarios.t13_retention_expiration import RetentionExpiration
from failure_lab.scenarios.t14_standard_mcp_client import StandardMcpClient

SCENARIO_CLASSES: tuple[type[Scenario], ...] = (
    ConcurrentIdenticalRetry,
    SameKeyDifferentArguments,
    ExecuteThenLoseResponse,
    CrashBeforeDispatch,
    CrashAfterDispatch,
    AgentRestartNewKey,
    ConcurrentBudgetRace,
    PermitRevocationRace,
    ForbiddenParameter,
    ReceiptTampering,
    DatabaseRestart,
    CacheFailure,
    RetentionExpiration,
    StandardMcpClient,
)

SCENARIOS_BY_ID: dict[str, type[Scenario]] = {
    scenario.test_id: scenario for scenario in SCENARIO_CLASSES
}

#: Scenarios cheap enough to run on every pull request.
FAST_SCENARIO_IDS = tuple(
    scenario.test_id for scenario in SCENARIO_CLASSES if scenario.tier == "fast"
)
#: Scenarios reserved for main, nightly and release candidates.
SLOW_SCENARIO_IDS = tuple(
    scenario.test_id for scenario in SCENARIO_CLASSES if scenario.tier == "slow"
)


def get_scenario(test_id: str, **options: object) -> Scenario:
    """Instantiate one scenario by its test id (case-insensitive)."""
    try:
        scenario_class = SCENARIOS_BY_ID[test_id.upper()]
    except KeyError:
        known = ", ".join(sorted(SCENARIOS_BY_ID))
        raise KeyError(f"unknown scenario {test_id!r}; known ids: {known}") from None
    return scenario_class(**options)


def select_scenarios(
    test_ids: object = None, *, tier: str | None = None, **options: object
) -> list[Scenario]:
    """Instantiate a selection of scenarios by id and/or tier."""
    if test_ids:
        chosen = [get_scenario(str(test_id), **options) for test_id in test_ids]  # type: ignore[union-attr]
    else:
        chosen = [scenario_class(**options) for scenario_class in SCENARIO_CLASSES]
    if tier is not None:
        chosen = [scenario for scenario in chosen if scenario.tier == tier]
    return chosen


__all__ = [
    "ConfigurationResult",
    "Counters",
    "EventLog",
    "FAST_SCENARIO_IDS",
    "Measurements",
    "SCENARIOS_BY_ID",
    "SCENARIO_CLASSES",
    "SLOW_SCENARIO_IDS",
    "Scenario",
    "ScenarioResult",
    "Verdict",
    "get_scenario",
    "select_scenarios",
]
