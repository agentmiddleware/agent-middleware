"""Coverage for scripts/rotate_api_keys.py.

The verifier samples one response for each key; rotation completion also
requires host configuration and rollout evidence. Its exit status reports
whether the two sampled responses match the expected statuses.
"""

import importlib.util
from pathlib import Path
import ssl
import sys

import certifi
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
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    requests = []

    class Client:
        def __init__(self, *, base_url, timeout, follow_redirects, trust_env, verify):
            assert base_url == normalized
            assert timeout == 30
            assert follow_redirects is False
            assert trust_env is False
            assert verify is True

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


@pytest.mark.parametrize("ca_source", ["file", "dir"])
def test_probe_honors_custom_ca_without_ambient_proxy(rotate, monkeypatch, ca_source):
    if ca_source == "file":
        ca_path = certifi.where()
        ca_arg = "cafile"
        monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    else:
        ca_path = ssl.get_default_verify_paths().capath
        if ca_path is None or not Path(ca_path).is_dir():
            pytest.skip("system CA directory unavailable")
        ca_arg = "capath"
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setenv(f"SSL_CERT_{ca_source.upper()}", ca_path)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    seen = {}
    real_create_default_context = ssl.create_default_context

    def capture_context(*args, **kwargs):
        seen["ca_path"] = kwargs.get(ca_arg)
        return real_create_default_context(*args, **kwargs)

    class Client:
        def __init__(self, *, verify, trust_env, follow_redirects, **_kwargs):
            seen.update(
                verify=verify, trust_env=trust_env, follow_redirects=follow_redirects
            )

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def get(self, *_args, **_kwargs):
            return httpx.Response(204)

    monkeypatch.setattr(ssl, "create_default_context", capture_context)
    monkeypatch.setattr(rotate.httpx, "Client", Client)
    assert rotate._probe("https://api.thisisatest.tech", "").status_code == 204
    assert seen["ca_path"] == ca_path
    assert seen["trust_env"] is False
    assert seen["follow_redirects"] is False
    assert isinstance(seen["verify"], ssl.SSLContext)
    assert seen["verify"].verify_mode == ssl.CERT_REQUIRED
    if ca_source == "file":
        assert seen["verify"].get_ca_certs()


def test_probe_invalid_ca_file_fails_before_client_or_key_send(
    rotate, monkeypatch, tmp_path
):
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing-ca.pem"))
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)

    def unexpected_client(**_kwargs):
        pytest.fail("invalid CA reached credential-bearing client")

    monkeypatch.setattr(rotate.httpx, "Client", unexpected_client)
    with pytest.raises(FileNotFoundError):
        rotate._probe("https://api.thisisatest.tech", "")
