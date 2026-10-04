"""Execute the entrypoint with inert commands; never start a service or database."""

from pathlib import Path
import shlex
import subprocess

import pytest


ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def commands(tmp_path):
    for name in ("uvicorn", "alembic"):
        command = tmp_path / name
        command.write_text(
            "#!/bin/sh\n"
            'printf "%s\\n" "${0##*/}" "$@" >> "$CAPTURE_PATH"\n'
            'if [ "${0##*/}" = alembic ]; then exit "${MIGRATION_EXIT:-0}"; fi\n'
        )
        command.chmod(0o700)
    return {
        "PATH": f"{tmp_path}:/usr/bin:/bin",
        "CAPTURE_PATH": str(tmp_path / "commands.txt"),
    }


def _run(arguments, environment):
    return subprocess.run(
        ["sh", str(ROOT / "scripts/docker_entrypoint.sh"), *arguments],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_compose_reload_command_reaches_uvicorn(commands):
    compose = (ROOT / "docker-compose.yml").read_text()
    raw = next(
        line.split("command:", 1)[1]
        for line in compose.splitlines()
        if "command:" in line
    )
    arguments = shlex.split(raw)
    assert "--reload" in arguments
    assert _run(arguments, commands).returncode == 0
    assert Path(commands["CAPTURE_PATH"]).read_text().splitlines() == arguments


def test_override_preserves_argument_boundaries_and_runs_after_migrations(commands):
    commands.update(RUN_MIGRATIONS_ON_START="true", DATABASE_URL="sqlite:///unused.db")
    arguments = ["uvicorn", "custom.module:app", "--label", "two words"]
    assert _run(arguments, commands).returncode == 0
    assert Path(commands["CAPTURE_PATH"]).read_text().splitlines() == [
        "alembic",
        "upgrade",
        "head",
        *arguments,
    ]


@pytest.mark.parametrize("database_url", [None, "sqlite:///unused.db"])
def test_migration_failure_cannot_be_bypassed_by_command_override(
    commands, database_url
):
    commands.update(RUN_MIGRATIONS_ON_START="true", MIGRATION_EXIT="7")
    if database_url is not None:
        commands["DATABASE_URL"] = database_url
    result = _run(["uvicorn", "custom.module:app", "--reload"], commands)
    assert result.returncode == (1 if database_url is None else 7)
    capture = Path(commands["CAPTURE_PATH"])
    assert not capture.exists() or "uvicorn" not in capture.read_text()
