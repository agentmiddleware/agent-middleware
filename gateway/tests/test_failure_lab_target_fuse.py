"""The direct configurations refuse any downstream that is not the lab's own.

``configured_target`` yields direct targets whose agent calls the downstream
with no gateway in the path. Some scenarios expect the naive one to *execute*
what it should have refused: T02 expects ``A_direct_naive`` to pay a 100x
refund under a spent key. That is only harmless because the downstream is the
simulated refund service, in this process. These tests pin the fuse that keeps
it that way: a direct target is refused before any request is sent unless its
agent can only reach this target's own downstream app.

None of these tests touch the network. A foreign transport here is either an
``httpx.MockTransport`` that records the request and raises, or a real
transport the fuse must refuse before it is ever used.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import httpx
import pytest

import failure_lab.configurations as configurations
from failure_lab.configurations import (
    Configuration,
    DirectAgent,
    LabEnvironment,
    UnsafeTargetError,
    configured_target,
)
from failure_lab.effect_ledger import EffectLedger
from failure_lab.refund_tool import SimulatedRefundService
from failure_lab.scenarios.base import Verdict
from failure_lab.scenarios.t02_same_key_different_arguments import (
    SameKeyDifferentArguments,
)

DIRECT_CONFIGURATIONS = (Configuration.DIRECT_NAIVE, Configuration.DIRECT_NATIVE)


def _env(tmp_path: Path) -> LabEnvironment:
    # The direct branch never touches the gateway application.
    return LabEnvironment(run_dir=tmp_path, app=None, admin_api_key="unused")


async def _foreign_app(scope, receive, send):  # pragma: no cover - never called
    raise AssertionError("a refused target must not send a request")


class _Redirecting(httpx.ASGITransport):
    """Keeps the right ``app`` attribute but would send requests elsewhere."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise AssertionError("a refused target must not send a request")


class _Wrapper(httpx.AsyncBaseTransport):
    """A pass-through wrapper: correct leaf, but the direct agent never wraps."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self.inner.handle_async_request(request)


def _install_client(monkeypatch, build):
    """Make DirectAgent use ``build(downstream)`` as its client."""

    def _init(self, configuration, downstream):
        self.configuration = configuration
        self._client = build(downstream)

    monkeypatch.setattr(DirectAgent, "__init__", _init)


def _recording_client(requests: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ConnectError("blocked: this test must not reach the network")

    return lambda downstream: httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=configurations.DOWNSTREAM_BASE_URL,
    )


@pytest.mark.anyio
@pytest.mark.parametrize("configuration", DIRECT_CONFIGURATIONS)
async def test_direct_targets_route_only_to_their_own_downstream(
    tmp_path, configuration
):
    """The real path is admitted, so the fuse cannot just refuse everything."""
    async with configured_target(_env(tmp_path), configuration) as target:
        assert target.agent.routes_only_to(target.downstream)


FOREIGN_CLIENTS = {
    "real network transport": lambda downstream: httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(),
        base_url=configurations.DOWNSTREAM_BASE_URL,
    ),
    "in-process but a different app": lambda downstream: httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_foreign_app),
        base_url=configurations.DOWNSTREAM_BASE_URL,
    ),
    "right app behind a wrapper": lambda downstream: httpx.AsyncClient(
        transport=_Wrapper(httpx.ASGITransport(app=downstream.asgi)),
        base_url=configurations.DOWNSTREAM_BASE_URL,
    ),
    "ASGITransport subclass holding the right app": lambda downstream: (
        httpx.AsyncClient(
            transport=_Redirecting(app=downstream.asgi),
            base_url=configurations.DOWNSTREAM_BASE_URL,
        )
    ),
    "right app plus a mounted network route": lambda downstream: httpx.AsyncClient(
        transport=httpx.ASGITransport(app=downstream.asgi),
        mounts={"https://": httpx.AsyncHTTPTransport()},
        base_url=configurations.DOWNSTREAM_BASE_URL,
    ),
}


@pytest.mark.anyio
@pytest.mark.parametrize("configuration", DIRECT_CONFIGURATIONS)
@pytest.mark.parametrize("case", sorted(FOREIGN_CLIENTS))
async def test_foreign_client_is_refused_before_yield(
    tmp_path, monkeypatch, configuration, case
):
    _install_client(monkeypatch, FOREIGN_CLIENTS[case])
    env = _env(tmp_path)
    with pytest.raises(UnsafeTargetError, match="refusing before any request"):
        async with configured_target(env, configuration):
            pytest.fail("a refused target must not be yielded")
    ledger = EffectLedger(tmp_path / f"effects-{configuration.value}.db")
    assert ledger.execution_count() == 0


@pytest.mark.anyio
async def test_refusal_sends_no_request(tmp_path, monkeypatch):
    """Refused before the yield: the transport never sees a request."""
    requests: list[httpx.Request] = []
    _install_client(monkeypatch, _recording_client(requests))
    with pytest.raises(UnsafeTargetError):
        async with configured_target(_env(tmp_path), Configuration.DIRECT_NAIVE):
            pytest.fail("a refused target must not be yielded")
    assert requests == []


@pytest.mark.anyio
async def test_downstream_that_does_not_wrap_the_built_service_is_refused(
    tmp_path, monkeypatch
):
    """The agent reaching the downstream is not enough if the downstream changed."""
    original = configurations.build_downstream_app

    def swapped(service, *args, **kwargs):
        downstream = original(service, *args, **kwargs)
        other = SimulatedRefundService(
            EffectLedger(tmp_path / "other.db"),
            native_idempotency=False,
            configuration="other",
        )
        return dataclasses.replace(downstream, service=other)

    monkeypatch.setattr(configurations, "build_downstream_app", swapped)
    with pytest.raises(UnsafeTargetError, match="does not wrap the simulated"):
        async with configured_target(_env(tmp_path), Configuration.DIRECT_NAIVE):
            pytest.fail("a refused target must not be yielded")


def test_unknown_httpx_internals_fail_closed():
    """If httpx renames the attributes the fuse reads, it refuses."""

    class NotAClient:
        pass

    assert configurations._client_routes_only_to(NotAClient(), object()) is False


@pytest.mark.anyio
async def test_t02_direct_naive_is_refused_not_executed(tmp_path, monkeypatch):
    """The arm this fuse exists for: refused, recorded as an error, nothing paid."""
    requests: list[httpx.Request] = []
    _install_client(monkeypatch, _recording_client(requests))
    result = await SameKeyDifferentArguments().run(
        _env(tmp_path), configurations=[Configuration.DIRECT_NAIVE]
    )
    (outcome,) = result.configurations
    assert outcome.verdict is Verdict.ERROR
    assert "UnsafeTargetError" in outcome.observation
    assert requests == []
    ledger = EffectLedger(tmp_path / "effects-A_direct_naive-T02.db")
    assert ledger.execution_count() == 0
