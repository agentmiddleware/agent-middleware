"""The state-machine explorer's invariants must be real and must be checkable.

A property-based harness earns its keep in two ways: the invariants describe
properties of the *observations* rather than restating what the code does, and
a violation produces a reproduction someone can act on. Both are pinned here.

The exploration itself is deliberately small — the point is that the machinery
works and is seeded reproducibly, not to search hard. The searching happens in
the scheduled CI job, where an unbounded cost is appropriate.
"""

from __future__ import annotations

import random

import pytest

from failure_lab.configurations import LabEnvironment
from failure_lab.stateful import (
    CommandKind,
    OperationState,
    default_invariants,
    explore,
    generate_sequence,
)

TEST_SIGNING_KEY = "dGVzdC1zaWduaW5nLWtleS1tYXRlcmlhbC0zMmJ5dGU="


@pytest.fixture
def strict_trust_mode(monkeypatch):
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


def test_the_model_covers_the_lifecycle_the_plan_names():
    states = {state.value for state in OperationState}
    for required in (
        "CREATED",
        "AUTHORIZED",
        "ACCEPTED",
        "DISPATCH_PENDING",
        "DISPATCHED",
        "SUCCEEDED",
        "FAILED",
        "OUTCOME_UNKNOWN",
        "RECONCILED",
        "REVOKED",
    ):
        assert required in states, required


def test_the_command_vocabulary_can_express_the_interesting_histories():
    kinds = {kind.value for kind in CommandKind}
    # Without these, whole classes of failure are unreachable by generation.
    for required in (
        "invoke",
        "retry_same_key",
        "retry_new_key",
        "revoke",
        "reconcile",
    ):
        assert any(required in kind for kind in kinds), required


def test_every_invariant_states_a_property_rather_than_a_mechanism():
    invariants = default_invariants()
    assert len(invariants) >= 7

    names = {invariant.name for invariant in invariants}
    for required in (
        "REJECTION_HAS_NO_EFFECT",
        "RECEIPTS_DO_NOT_OUTRUN_EVIDENCE",
        "AT_MOST_ONE_DISPATCH_PER_ACCEPTED_KEY",
        "BUDGET_NOT_EXCEEDED",
        "SIGNED_RECEIPTS_RESIST_MUTATION",
    ):
        assert required in names, required

    for invariant in invariants:
        assert invariant.statement.strip(), invariant.name
        assert len(invariant.statement.split()) >= 10, invariant.name
        # An invariant phrased as "the service does X" is describing the
        # implementation, which is what makes a property test worthless.
        lowered = invariant.statement.lower()
        for mechanism in ("claim_dispatch", "authorize_and_reserve", "sqlalchemy"):
            assert mechanism not in lowered, f"{invariant.name} names a mechanism"


def test_generation_is_reproducible_from_the_seed_alone():
    """A violation is only actionable if its seed regenerates it."""
    first = generate_sequence(random.Random(11), 8)
    again = generate_sequence(random.Random(11), 8)
    different = generate_sequence(random.Random(12), 8)

    assert first, "generation produced no commands"
    assert [c.kind for c in first] == [c.kind for c in again]
    assert [c.kind for c in first] != [c.kind for c in different] or len(first) != len(
        different
    )


@pytest.mark.anyio
async def test_a_short_exploration_runs_and_reports_honestly(
    tmp_path, clean_database, strict_trust_mode
):
    from app.main import app

    env = LabEnvironment(run_dir=tmp_path, app=app, admin_api_key="test-key")
    result = await explore(env, seed=5, sequences=2, max_length=6, shrink_budget=4)

    assert result.seed == 5
    assert result.sequences_run == 2
    assert result.commands_executed > 0, "nothing ran, so nothing was checked"
    assert not result.errors, result.errors
    assert result.invariants, "the result does not record what it checked"
    assert result.limitations, (
        "an exploration that claims no limitation is overclaiming"
    )

    document = result.as_dict()
    # The conclusion must not read as a proof.
    assert "proof" in document["conclusion"].lower()
    assert "not a proof" in document["conclusion"].lower()

    for violation in result.violations:
        # If the search did find something, it must be reproducible.
        assert violation.minimized_sequence is not None
        assert len(violation.minimized_sequence) <= len(violation.sequence)
