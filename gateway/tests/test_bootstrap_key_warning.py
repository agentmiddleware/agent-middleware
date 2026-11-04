"""Startup warning for short VALID_API_KEYS entries in production-like environments.

It carries only a count. No key value, and no prefix of one, may reach a log.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from app.core.trust_mode import (
    MIN_RECOMMENDED_BOOTSTRAP_KEY_LENGTH,
    REASON_BOOTSTRAP_KEYS_SHORT,
    count_short_bootstrap_keys,
    reset_short_bootstrap_key_warning,
    warn_if_short_bootstrap_keys,
)

_LOGGER = "app.core.trust_mode"
_STRONG = "amw_live_" + "x" * 43
_THIRTY_ONE = "k" * 31
_THIRTY_TWO = "k" * 32


@pytest.fixture(autouse=True)
def _fresh_warning_state():
    reset_short_bootstrap_key_warning()
    yield
    reset_short_bootstrap_key_warning()


def _settings(environment: str, keys: str) -> SimpleNamespace:
    return SimpleNamespace(ENVIRONMENT=environment, VALID_API_KEYS=keys)


def _warnings(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == _LOGGER and REASON_BOOTSTRAP_KEYS_SHORT in record.getMessage()
    ]


def test_count_follows_the_auth_parser_and_the_32_character_boundary() -> None:
    assert MIN_RECOMMENDED_BOOTSTRAP_KEY_LENGTH == 32
    assert count_short_bootstrap_keys("") == 0
    assert count_short_bootstrap_keys(" , ,, ") == 0
    assert count_short_bootstrap_keys(_STRONG) == 0
    assert count_short_bootstrap_keys(_THIRTY_TWO) == 0
    assert count_short_bootstrap_keys(_THIRTY_ONE) == 1
    assert count_short_bootstrap_keys(f" {_THIRTY_ONE} ,,{_STRONG},short-key-01") == 2
    assert count_short_bootstrap_keys(f"  {_THIRTY_TWO}  ") == 0


def test_warns_once_in_a_production_like_environment(caplog) -> None:
    settings = _settings("production", f"{_STRONG},short-key-01,another-short-1")
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        first = warn_if_short_bootstrap_keys(settings)
        second = warn_if_short_bootstrap_keys(settings)
    assert (first, second) == (True, False)
    messages = _warnings(caplog)
    assert len(messages) == 1
    assert "2 entries are" in messages[0]
    assert "shorter than 32 characters" in messages[0]
    assert "rotate_api_keys.py" in messages[0]
    assert not any(dash in messages[0] for dash in (chr(0x2014), chr(0x2013), " - "))


def test_singular_wording_for_one_entry(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        warn_if_short_bootstrap_keys(_settings("production", "short-key-01"))
    assert "1 entry is" in _warnings(caplog)[0]


@pytest.mark.parametrize(
    ("environment", "keys"),
    [
        ("production", _STRONG),
        ("production", f"{_STRONG},{_THIRTY_TWO}"),
        ("production", ""),
        ("production", " , "),
        ("local", "short-key-01"),
        ("development", "short-key-01"),
        ("test", "short-key-01"),
    ],
)
def test_silent_when_nothing_applies(caplog, environment, keys) -> None:
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        logged = warn_if_short_bootstrap_keys(_settings(environment, keys))
    assert logged is False
    assert _warnings(caplog) == []


def test_other_production_like_environments_warn(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        logged = warn_if_short_bootstrap_keys(_settings("staging", "short-key-01"))
    assert logged is True


def test_no_key_material_reaches_any_log(caplog) -> None:
    """Neither a full value nor any prefix of an entry may appear in a log."""
    short_one = "zq9xk-secret-alpha"
    short_two = "rv7mw-secret-bravo"
    settings = _settings("production", f"{short_one}, {short_two} ,{_STRONG}")
    with caplog.at_level(logging.DEBUG):
        warn_if_short_bootstrap_keys(settings)
        warn_if_short_bootstrap_keys(settings)
    text = caplog.text + "".join(
        str(arg) for record in caplog.records for arg in (record.args or ())
    )
    assert REASON_BOOTSTRAP_KEYS_SHORT in text
    for entry in (short_one, short_two, _STRONG):
        for length in range(3, len(entry) + 1):
            assert entry[:length] not in text, entry[:length]


def test_a_failed_check_does_not_use_up_the_warning(caplog) -> None:
    """The flag is only set when a warning is logged."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert warn_if_short_bootstrap_keys(_settings("local", "short-key-01")) is False
        assert warn_if_short_bootstrap_keys(_settings("production", _STRONG)) is False
        assert (
            warn_if_short_bootstrap_keys(_settings("production", "short-key-01"))
            is True
        )


def test_never_refuses_to_start() -> None:
    # No exception for any input: this is a warning, not a guardrail.
    for keys in ("", "x", "x,y,z", "\udcffbad"):
        warn_if_short_bootstrap_keys(_settings("production", keys))
        reset_short_bootstrap_key_warning()


def test_lifespan_calls_the_warning(monkeypatch) -> None:
    """Wired into startup: the lifespan function names the warning."""
    import inspect

    from app import main as main_module

    source = inspect.getsource(main_module.lifespan)
    assert "warn_if_short_bootstrap_keys(settings)" in source
