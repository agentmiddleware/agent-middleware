"""Keep the local mypy hook aligned with the gateway's installed dependencies."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def _mypy_hook() -> dict[str, object]:
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    hooks = [
        hook
        for repo in config["repos"]
        if repo["repo"] == "local"
        for hook in repo["hooks"]
        if hook["id"] == "mypy"
    ]
    assert len(hooks) == 1
    return hooks[0]


def test_mypy_hook_uses_isolated_checkout_dependencies() -> None:
    hook = _mypy_hook()
    tokens = shlex.split(hook["entry"])

    assert hook["language"] == "system"
    assert hook["pass_filenames"] is False
    assert hook["require_serial"] is True
    assert tokens[:4] == ["uv", "run", "--directory", "gateway"]
    assert {"--no-project", "--isolated", "--no-env-file"} <= set(tokens)
    assert [
        tokens[index + 1]
        for index, token in enumerate(tokens)
        if token == "--with-requirements"
    ] == ["requirements.txt", "requirements-dev.txt"]
    assert tokens[tokens.index("--with-editable") + 1] == "../sdk/python[verify]"
    assert tokens[-6:] == ["python", "-m", "mypy", "--config-file", "mypy.ini", "."]
    assert tokens.count(".") == 1


def test_mypy_hook_triggers_on_every_type_environment_input() -> None:
    pattern = re.compile(_mypy_hook()["files"])
    required = (
        ".pre-commit-config.yaml",
        "gateway/app/main.py",
        "gateway/typecheck/scopes_coroutine.py",
        "gateway/mypy.ini",
        "gateway/requirements.txt",
        "gateway/requirements-dev.txt",
        "gateway/pyproject.toml",
        "sdk/python/src/b2a_sdk/models.py",
        "sdk/python/src/b2a_sdk/py.typed",
        "sdk/python/pyproject.toml",
    )
    for path in required:
        assert pattern.fullmatch(path), path
    for path in ("gateway/tests/test_api_keys.py", "sdk/python/tests/test_sdk.py"):
        assert not pattern.fullmatch(path), path


def test_gateway_python312_ci_runs_the_actual_hook() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["python"]["steps"]
    hook_steps = [
        step for step in steps if step.get("name") == "Pre-commit mypy (gateway)"
    ]
    assert len(hook_steps) == 1
    step = hook_steps[0]
    assert (
        step["if"] == "matrix.project == 'gateway' && matrix.python-version == '3.12'"
    )
    assert step["working-directory"] == "."
    assert step["env"]["UV_PYTHON"] == "3.12"
    assert shlex.split(step["run"]) == [
        "uv",
        "tool",
        "run",
        "--from",
        "pre-commit==4.6.0",
        "pre-commit",
        "run",
        "mypy",
        "--all-files",
    ]
