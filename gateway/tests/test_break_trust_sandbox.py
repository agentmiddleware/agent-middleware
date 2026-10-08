"""Break-it regression tests: trust and sandbox services.

Each test below fails on the pre-fix code and passes after the fix:
- pattern scoring accepted any transform keyword, so wrong answers solved
  the puzzle;
- api_mock counted any repeated bogus path, so nonsense calls solved it;
- navigation accepted JSON true as node 1 (bool is an int subclass);
- the outbound-URL guard trusted this platform's DNS roundtrip for
  ambiguous numeric IP forms, which other resolvers read as loopback;
- the behavioral-sandbox router answered foreign environments with 403
  (echoing the owner's wallet id) instead of 404 like the puzzle router.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.core import url_guard
from app.core.url_guard import check_outbound_url
from app.main import app
from app.services.sandbox import SandboxEngine
from tests.test_trust_helpers import provision_agent_wallet


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def engine():
    return SandboxEngine()


class TestPatternScoring:
    @pytest.mark.anyio
    async def test_wrong_transform_guess_is_rejected(self, engine):
        env = await engine.create_environment(
            env_type="pattern", difficulty="easy", seed=1, owner_wallet_id="w1"
        )
        assert env.hidden_rules == ["Transform input grid by applying: mirror"]
        result = await engine.submit_action(
            env.env_id, {"type": "submit_transform", "value": "rotate"}
        )
        assert result.done is False
        assert result.reward == -1.0
        assert env.state.solved is False

    @pytest.mark.anyio
    async def test_correct_transform_guess_solves(self, engine):
        env = await engine.create_environment(
            env_type="pattern", difficulty="easy", seed=1, owner_wallet_id="w1"
        )
        result = await engine.submit_action(
            env.env_id, {"type": "submit_transform", "value": "mirror"}
        )
        assert result.done is True
        assert result.reward == 10.0

    @pytest.mark.anyio
    async def test_hard_rule_needs_every_transform_named(self, engine):
        env = await engine.create_environment(
            env_type="pattern", difficulty="hard", seed=1, owner_wallet_id="w1"
        )
        partial = await engine.submit_action(
            env.env_id, {"type": "submit_transform", "value": "rotate"}
        )
        assert partial.done is False
        full = await engine.submit_action(
            env.env_id,
            {"type": "submit_transform", "value": "rotate then invert, odd rows shift"},
        )
        assert full.done is True


class TestApiMockScoring:
    @pytest.mark.anyio
    async def test_bogus_endpoints_never_solve(self, engine):
        env = await engine.create_environment(
            env_type="api_mock", difficulty="easy", seed=1, owner_wallet_id="w1"
        )
        total = env.state.metadata["num_endpoints"]
        for _ in range(total + 5):
            result = await engine.submit_action(
                env.env_id,
                {"type": "call_endpoint", "value": "/api/v1/does-not-exist"},
            )
            assert result.done is False
        assert env.state.solved is False

    @pytest.mark.anyio
    async def test_repeated_valid_endpoint_counts_once(self, engine):
        env = await engine.create_environment(
            env_type="api_mock", difficulty="easy", seed=1, owner_wallet_id="w1"
        )
        real_path = env.state.metadata["mock_api"][0]["path"]
        for _ in range(env.state.metadata["num_endpoints"] + 2):
            result = await engine.submit_action(
                env.env_id, {"type": "call_endpoint", "value": real_path}
            )
        assert result.done is False
        assert env.state.solved is False

    @pytest.mark.anyio
    async def test_calling_every_real_endpoint_solves(self, engine):
        env = await engine.create_environment(
            env_type="api_mock", difficulty="easy", seed=1, owner_wallet_id="w1"
        )
        result = None
        for endpoint in env.state.metadata["mock_api"]:
            result = await engine.submit_action(
                env.env_id,
                {"type": "call_endpoint", "value": endpoint["path"]},
            )
            if result.done:
                break
        assert result is not None and result.done is True
        assert env.state.solved is True


class TestNavigationTyping:
    @pytest.mark.anyio
    async def test_boolean_move_is_rejected(self, engine):
        env = await engine.create_environment(
            env_type="navigation", difficulty="easy", seed=0, owner_wallet_id="w1"
        )
        assert 1 in env.state.metadata["visible_edges"].get("0", [])
        result = await engine.submit_action(env.env_id, {"type": "move", "value": True})
        assert result.reward == -0.5
        assert env.state.metadata["current_node"] == 0

    @pytest.mark.anyio
    async def test_integer_move_still_works(self, engine):
        env = await engine.create_environment(
            env_type="navigation", difficulty="easy", seed=0, owner_wallet_id="w1"
        )
        neighbors = env.state.metadata["visible_edges"]["0"]
        result = await engine.submit_action(
            env.env_id, {"type": "move", "value": neighbors[0]}
        )
        assert result.reward == 0.5
        assert env.state.metadata["current_node"] == neighbors[0]


class TestAmbiguousNumericHosts:
    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "url",
        [
            "http://0177.0.0.1/",
            "http://0x7f.0.0.1/",
            "http://0x7f000001/",
            "http://2130706433/",
        ],
    )
    async def test_ambiguous_numeric_forms_fail_closed_without_dns(
        self, monkeypatch, url
    ):
        async def no_dns(_host):
            raise AssertionError("ambiguous numeric host must not reach DNS")

        monkeypatch.setattr(url_guard, "_resolve_host", no_dns)
        assert await check_outbound_url(url) == "private_address_blocked"

    @pytest.mark.anyio
    async def test_ordinary_hostname_still_uses_dns(self, monkeypatch):
        seen = []

        async def resolve(host):
            seen.append(host)
            return [(None, None, None, None, ("93.184.216.34", 0))]

        monkeypatch.setattr(url_guard, "_resolve_host", resolve)
        assert await check_outbound_url("https://example.com/page") is None
        assert seen == ["example.com"]


@pytest.mark.proof
class TestBehavioralSandboxTenantIsolation:
    @pytest.mark.anyio
    async def test_foreign_environment_is_indistinguishable_from_missing(
        self, client, clean_database
    ):
        a = await provision_agent_wallet(client)
        b = await provision_agent_wallet(client)
        created = await client.post(
            "/v1/sandbox/behavioral/environments",
            json={"name": "victim", "environment_type": "mcp_sandbox"},
            headers=a["agent_headers"],
        )
        assert created.status_code == 201
        env_id = created.json()["env_id"]
        missing_id = "sandbox-doesnotexist"

        def _same_as_missing(foreign, missing) -> None:
            assert foreign.status_code == 404
            assert missing.status_code == 404
            assert foreign.text.replace(env_id, "ID") == missing.text.replace(
                missing_id, "ID"
            )
            assert a["agent_wallet_id"] not in foreign.text

        try:
            _same_as_missing(
                await client.get(
                    f"/v1/sandbox/behavioral/environments/{env_id}",
                    headers=b["agent_headers"],
                ),
                await client.get(
                    f"/v1/sandbox/behavioral/environments/{missing_id}",
                    headers=b["agent_headers"],
                ),
            )
            _same_as_missing(
                await client.post(
                    "/v1/sandbox/behavioral/execute",
                    json={
                        "env_id": env_id,
                        "tool_name": "get_weather",
                        "tool_input": {"location": "x"},
                        "dry_run": True,
                    },
                    headers=b["agent_headers"],
                ),
                await client.post(
                    "/v1/sandbox/behavioral/execute",
                    json={
                        "env_id": missing_id,
                        "tool_name": "get_weather",
                        "tool_input": {"location": "x"},
                        "dry_run": True,
                    },
                    headers=b["agent_headers"],
                ),
            )
            _same_as_missing(
                await client.delete(
                    f"/v1/sandbox/behavioral/environments/{env_id}",
                    headers=b["agent_headers"],
                ),
                await client.delete(
                    f"/v1/sandbox/behavioral/environments/{missing_id}",
                    headers=b["agent_headers"],
                ),
            )
            # Owner access still works, and nothing was destroyed by the attacker.
            own = await client.get(
                f"/v1/sandbox/behavioral/environments/{env_id}",
                headers=a["agent_headers"],
            )
            assert own.status_code == 200
        finally:
            await client.delete(
                f"/v1/sandbox/behavioral/environments/{env_id}",
                headers=a["agent_headers"],
            )

    @pytest.mark.anyio
    async def test_owner_can_execute_in_own_environment(self, client, clean_database):
        a = await provision_agent_wallet(client)
        created = await client.post(
            "/v1/sandbox/behavioral/environments",
            json={"name": "owner-exec", "environment_type": "mcp_sandbox"},
            headers=a["agent_headers"],
        )
        assert created.status_code == 201
        env_id = created.json()["env_id"]
        try:
            resp = await client.post(
                "/v1/sandbox/behavioral/execute",
                json={
                    "env_id": env_id,
                    "tool_name": "get_weather",
                    "tool_input": {"location": "SF"},
                    "dry_run": True,
                },
                headers=a["agent_headers"],
            )
            assert resp.status_code == 200
            assert resp.json()["status"] == "success"
        finally:
            await client.delete(
                f"/v1/sandbox/behavioral/environments/{env_id}",
                headers=a["agent_headers"],
            )
