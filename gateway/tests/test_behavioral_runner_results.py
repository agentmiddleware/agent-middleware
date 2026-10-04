"""Exercise generated runners with fixed local code; no backend is activated."""

import contextlib
import io
import json

import pytest

from app.services.behavioral_sandbox import BehavioralSandboxEngine


def run_fixed_code(code, context):
    source = BehavioralSandboxEngine._build_python_wrapper(code, context, False)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exec(compile(source, "synthetic-wrapper", "exec"), {})
    return BehavioralSandboxEngine._parse_python_execution_output(
        output.getvalue().encode(), b"", "synthetic"
    )


@pytest.mark.parametrize(
    "context",
    [{}, {"enabled": True}, {"optional": None}, {"nested": [False, "quote'\"\n☃"]}],
)
def test_runner_preserves_json_context(context):
    result = run_fixed_code("print(json.dumps(context))", context)
    assert result["success"] is True
    assert json.loads(result["output"]) == context


def test_print_then_failure_stays_failed():
    result = run_fixed_code("print('before failure')\nraise ValueError('failed')", {})
    assert result["success"] is False
    assert result["error"] == "failed"
    assert result["output"] == "before failure\n"


@pytest.mark.parametrize(
    "stdout",
    [
        b"",
        b"plain output",
        b"[]",
        b"{}",
        b'{"success": "yes"}',
        b'hello\n{"success": false, "error": "failed"}\n',
    ],
)
def test_malformed_runner_result_cannot_establish_success(stdout):
    result = BehavioralSandboxEngine._parse_python_execution_output(
        stdout, b"", "synthetic"
    )
    assert result["success"] is False


def test_nonzero_exit_overrides_printed_success():
    result = BehavioralSandboxEngine._parse_python_execution_output(
        b'{"success": true}', b"process failed", "synthetic", returncode=1
    )
    assert result["success"] is False
    assert result["error"] == "process failed"
