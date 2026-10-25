"""Coverage for scripts/rotate_api_keys.py.

The verifier samples one response for each key; rotation completion also
requires host configuration and rollout evidence. Its exit status reports
whether the two sampled responses match the expected statuses.
"""

import importlib.util
from pathlib import Path
import sys

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "rotate_api_keys", REPO_ROOT / "scripts" / "rotate_api_keys.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def rotate():
    return _load_module()


def _env(
    monkeypatch,
    url="https://api.example.test",
    old="old-key-value",
    new="new-key-value",
):
    monkeypatch.setenv("AGENT_MIDDLEWARE_API_URL", url)
    monkeypatch.setenv("OLD_API_KEY", old)
    monkeypatch.setenv("NEW_API_KEY", new)


def _fake_probe(status_by_key):
    def probe(base_url, api_key):
        return httpx.Response(
            status_by_key[api_key], request=httpx.Request("GET", base_url)
        )

    return probe


def test_generate_prints_distinct_prefixed_keys(rotate, capsys):
    assert rotate.generate(3) == 0
    keys = capsys.readouterr().out.strip().splitlines()
    assert len(keys) == 3
    assert len(set(keys)) == 3
    for key in keys:
        assert key.startswith(rotate.KEY_PREFIX)
        # token_urlsafe(32) yields 43 chars; anything shorter means the
        # entropy constant was accidentally lowered.
        assert len(key) >= len(rotate.KEY_PREFIX) + 43


def test_verify_requires_env(rotate, monkeypatch):
    for var in ("AGENT_MIDDLEWARE_API_URL", "OLD_API_KEY", "NEW_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert rotate.verify() == 2


def test_verify_reports_sampled_old_rejection_and_new_acceptance(rotate, monkeypatch):
    _env(monkeypatch)
    monkeypatch.setattr(
        rotate, "_probe", _fake_probe({"old-key-value": 403, "new-key-value": 200})
    )
    assert rotate.verify() == 0


def test_verify_help_describes_sampled_responses(rotate, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["rotate_api_keys.py", "--help"])

    with pytest.raises(SystemExit) as exc:
        rotate.main()

    assert exc.value.code == 0
    assert "sample old-key rejection and new-key acceptance" in capsys.readouterr().out
    assert "different load-balanced workers" in rotate.__doc__
    assert "database-key authentication can update last_used_at" in rotate.__doc__


def test_verify_fails_when_old_key_still_authenticates(rotate, monkeypatch):
    _env(monkeypatch)
    monkeypatch.setattr(
        rotate, "_probe", _fake_probe({"old-key-value": 200, "new-key-value": 200})
    )
    assert rotate.verify() == 1


def test_verify_fails_when_new_key_rejected(rotate, monkeypatch):
    _env(monkeypatch)
    monkeypatch.setattr(
        rotate, "_probe", _fake_probe({"old-key-value": 403, "new-key-value": 403})
    )
    assert rotate.verify() == 1


@pytest.mark.parametrize(
    "target",
    [
        "http://remote.example.test",
        "http://127.0.0.1.remote.example.test",
        "ftp://remote.example.test",
        "https://user:password@remote.example.test",
        "https://remote.example.test/path",
        "https://remote.example.test?query=value",
        "https://remote.example.test#fragment",
        "https://remote.example.test:0",
    ],
)
def test_verify_rejects_unsafe_origins_before_probing(rotate, monkeypatch, target):
    _env(monkeypatch, url=target)

    def unexpected_probe(*args):
        pytest.fail("unsafe origin reached credential-bearing probe")

    monkeypatch.setattr(rotate, "_probe", unexpected_probe)
    assert rotate.verify() == 2


@pytest.mark.parametrize(
    ("target", "normalized"),
    [
        ("https://api.example.test/", "https://api.example.test"),
        ("https://api.agentmiddleware.dev", "https://api.agentmiddleware.dev"),
        ("http://localhost:8000", "http://localhost:8000"),
        ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
        ("http://[::1]:8000", "http://[::1]:8000"),
    ],
)
def test_verify_allows_safe_origins_and_disables_redirects(
    rotate, monkeypatch, target, normalized
):
    _env(monkeypatch, url=target)
    requests = []

    class Client:
        def __init__(self, *, base_url, timeout, follow_redirects):
            assert base_url == normalized
            assert timeout == 30
            assert follow_redirects is False

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, path, *, headers):
            requests.append((path, headers))
            return httpx.Response(403 if len(requests) == 1 else 200)

    monkeypatch.setattr(rotate.httpx, "Client", Client)
    assert rotate.verify() == 0
    assert requests == [
        (rotate.CHECK_PATH, {"X-API-Key": "old-key-value"}),
        (rotate.CHECK_PATH, {"X-API-Key": "new-key-value"}),
    ]
