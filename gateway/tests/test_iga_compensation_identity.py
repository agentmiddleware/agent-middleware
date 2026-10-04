from types import SimpleNamespace
from app.core import oidc_iga


async def test_late_iga_compensation_preserves_recent_accepted_call(monkeypatch):
    import app.services.policies

    oidc_iga.reset_iga_counters()
    clock = [0.0]
    monkeypatch.setattr(oidc_iga, "_monotonic", lambda: clock[0])
    grant = oidc_iga.IGAGrant(
        group="operators",
        policy_id="synthetic-policy",
        velocity_window_seconds=10,
        velocity_max_calls=2,
    )
    monkeypatch.setattr(oidc_iga, "resolve_policy_grants", lambda p: [grant])

    async def bundle(_id):
        return SimpleNamespace(is_active=True, allowed_tools=["synthetic.tool"])

    monkeypatch.setattr(app.services.policies, "get_policy_bundle", bundle)
    principal = oidc_iga.EnterprisePrincipal(
        subject="qa",
        provider="okta",
        issuer="https://synthetic.invalid",
        groups=("operators",),
    )
    first = await oidc_iga.enforce_tool_call(principal, "synthetic.tool")
    assert first.allowed  # A: slow pre-dispatch gate.
    clock[0] = 8
    assert (
        await oidc_iga.enforce_tool_call(principal, "synthetic.tool")
    ).allowed  # B: successful dispatch.
    clock[0] = 11
    await oidc_iga.release_tool_use(
        principal,
        "synthetic.tool",
        group="operators",
        policy_id="synthetic-policy",
        reservation=first.reservation,
    )  # A fails late.
    assert (
        await oidc_iga.enforce_tool_call(principal, "synthetic.tool")
    ).allowed  # C: second real call in window.
    clock[0] = 12
    decision = await oidc_iga.enforce_tool_call(
        principal, "synthetic.tool"
    )  # D: would be third.
    assert not decision.allowed and decision.reason == "iga_velocity_exceeded"


async def test_compensation_cannot_release_other_grant_or_release_twice(monkeypatch):
    import app.services.policies

    oidc_iga.reset_iga_counters()
    grant = oidc_iga.IGAGrant(
        group="operators",
        policy_id="synthetic-policy",
        max_uses=2,
        velocity_window_seconds=10,
        velocity_max_calls=2,
    )
    monkeypatch.setattr(oidc_iga, "resolve_policy_grants", lambda p: [grant])

    async def bundle(_id):
        return SimpleNamespace(is_active=True, allowed_tools=["synthetic.tool"])

    monkeypatch.setattr(app.services.policies, "get_policy_bundle", bundle)
    monkeypatch.setattr(
        oidc_iga, "_monotonic", lambda: 1.0
    )  # Equal timestamps are distinct uses.
    principal = oidc_iga.EnterprisePrincipal(
        subject="qa",
        provider="okta",
        issuer="https://synthetic.invalid",
        groups=("operators",),
    )
    first = await oidc_iga.enforce_tool_call(principal, "synthetic.tool")
    second = await oidc_iga.enforce_tool_call(principal, "synthetic.tool")
    assert first.allowed and second.allowed
    await oidc_iga.release_tool_use(
        principal,
        "synthetic.tool",
        group="wrong",
        policy_id="synthetic-policy",
        reservation=first.reservation,
    )
    assert not (await oidc_iga.enforce_tool_call(principal, "synthetic.tool")).allowed
    await oidc_iga.release_tool_use(
        principal,
        "synthetic.tool",
        group="operators",
        policy_id="synthetic-policy",
        reservation=first.reservation,
    )
    assert (await oidc_iga.enforce_tool_call(principal, "synthetic.tool")).allowed
    await oidc_iga.release_tool_use(
        principal,
        "synthetic.tool",
        group="operators",
        policy_id="synthetic-policy",
        reservation=first.reservation,
    )
    await oidc_iga.release_tool_use(
        principal, "synthetic.tool", group="operators", policy_id="synthetic-policy"
    )
    assert not (await oidc_iga.enforce_tool_call(principal, "synthetic.tool")).allowed
