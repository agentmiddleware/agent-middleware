"""Exercise the release gate's repository-owned gateway image preflight."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE_SCRIPT = REPO_ROOT / "scripts/trust_release_gate.sh"
MAKEFILE = REPO_ROOT / "Makefile"
CHECKER = REPO_ROOT / "scripts/check_gateway_image_contract.py"

DOCKERFILE = """\
FROM python:3.12-slim AS build-metadata
WORKDIR /context
COPY . /context/
ARG RELEASE_COMMIT_SHA
RUN mkdir /stamp && STAGED_SHA_PATH=/context/.build_commit_sha BUILD_SHA_PATH=/stamp/.build_commit_sha python /context/gateway/scripts/write_build_commit_sha.py
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONPATH=/sdk/python/src
COPY sdk/python /sdk/python
COPY gateway/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt && pip install /sdk/python
COPY gateway/ /app/
COPY --from=build-metadata /stamp/.build_commit_sha /app/.build_commit_sha
ENTRYPOINT ["scripts/docker_entrypoint.sh"]
"""

DEV_DOCKERFILE = """\
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONPATH=/sdk/python/src
COPY sdk/python /sdk/python
COPY gateway/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt && pip install /sdk/python
COPY gateway/ /app/
ENTRYPOINT ["scripts/docker_entrypoint.sh"]
"""

WRITER = """\
import os
import re
from pathlib import Path

staged_path = Path(os.environ["STAGED_SHA_PATH"])
staged = staged_path.read_text().strip() if staged_path.exists() else None
argument = os.environ.get("RELEASE_COMMIT_SHA", "").strip()
valid = lambda value: re.fullmatch(r"[0-9a-fA-F]{7,64}", value) is not None
if argument and not valid(argument):
    raise SystemExit(1)
if staged is not None and not valid(staged):
    raise SystemExit(1)
if staged is not None and argument and staged.lower() != argument.lower():
    raise SystemExit(1)
value = staged or argument
if not value:
    raise SystemExit(1)
Path(os.environ["BUILD_SHA_PATH"]).write_text(value.lower() + "\\n")
"""

DOCKERIGNORE = """\
.git
.agents
**/.agents
.claude
**/.claude
.envrc
**/.envrc
.env
.env.*
**/.env
**/.env.*
.railway
**/.railway
.aws
**/.aws
.ssh
**/.ssh
*.pem
**/*.pem
*.key
**/*.key
*.db
**/*.db
*.db-wal
**/*.db-wal
*.db-shm
**/*.db-shm
*.log
**/*.log
gateway/data
gateway/.build_commit_sha
gateway/tests
"""


def _invoked_make_targets(script_text: str) -> list[str]:
    targets = []
    for line in script_text.splitlines():
        match = re.match(r"\s*make\s+(.*)", line)
        if not match:
            continue
        tokens = iter(match.group(1).split())
        for token in tokens:
            if token in {"-C", "-f"}:
                next(tokens, None)
            elif not token.startswith("-") and "=" not in token:
                targets.append(token)
                break
    return targets


def _check(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER), "--root", str(root)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.fixture
def image_source(tmp_path: Path) -> Path:
    for source in (
        "gateway/requirements.txt",
        "gateway/scripts/docker_entrypoint.sh",
        "sdk/python/pyproject.toml",
        "sdk/python/src/b2a_sdk/__init__.py",
        "sdk/python/src/b2a_sdk/receipt_verifier.py",
    ):
        target = tmp_path / source
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT.parent / source, target)
    sources = {
        "gateway/Dockerfile": DOCKERFILE,
        "gateway/Dockerfile.dev": DEV_DOCKERFILE,
        "gateway/docker-compose.yml": "services:\n  api:\n    build:\n      context: ..\n      dockerfile: gateway/Dockerfile.dev\n",
        "gateway/scripts/write_build_commit_sha.py": WRITER,
        ".dockerignore": DOCKERIGNORE,
    }
    for name, contents in sources.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")
    assert _check(tmp_path).returncode == 0
    return tmp_path


def test_release_gate_invokes_defined_image_contract_target() -> None:
    invoked = _invoked_make_targets(GATE_SCRIPT.read_text())
    defined = set(
        re.findall(
            r"^([A-Za-z0-9][A-Za-z0-9_.-]*)\s*::?", MAKEFILE.read_text(), re.MULTILINE
        )
    )
    assert "check-gateway-image-contract" in invoked
    assert invoked and set(invoked) <= defined


@pytest.mark.parametrize(
    ("path", "before", "after"),
    (
        ("gateway/Dockerfile", "COPY sdk/python /sdk/python", "# removed SDK copy"),
        ("gateway/Dockerfile.dev", "COPY sdk/python /sdk/python", "# removed SDK copy"),
        ("gateway/Dockerfile", "COPY gateway/ /app/", "COPY . /app/"),
        (
            "gateway/Dockerfile",
            "COPY --from=build-metadata /stamp/.build_commit_sha /app/.build_commit_sha",
            "# removed validated stamp copy",
        ),
        (
            "gateway/Dockerfile",
            "STAGED_SHA_PATH=/context/.build_commit_sha",
            "STAGED_SHA_PATH=/context/gateway/.build_commit_sha",
        ),
        ("gateway/docker-compose.yml", "context: ..", "context: ."),
        (".dockerignore", "**/.env.*", "# nested secrets admitted"),
        (".dockerignore", "**/.agents", "# nested agent files admitted"),
        (".dockerignore", "**/.claude", "# nested assistant files admitted"),
        (".dockerignore", "**/.envrc", "# nested env loader admitted"),
        (".dockerignore", ".aws", "# local AWS credentials admitted"),
        (".dockerignore", "**/.aws", "# nested AWS credentials admitted"),
        (".dockerignore", "**/*.db", "# nested database admitted"),
        (".dockerignore", "**/*.db-wal", "# database sidecar admitted"),
        (".dockerignore", "**/*.db-shm", "# database sidecar admitted"),
        (".dockerignore", "**/*.log", "# logs admitted"),
        (".dockerignore", "gateway/data", "# local state admitted"),
        (".dockerignore", "gateway/.build_commit_sha", "# nested stamp admitted"),
    ),
)
def test_checker_rejects_source_contract_regressions(
    image_source: Path, path: str, before: str, after: str
) -> None:
    source = image_source / path
    source.write_text(source.read_text().replace(before, after), encoding="utf-8")
    assert _check(image_source).returncode != 0


@pytest.mark.parametrize(
    "excluded_input",
    (
        "sdk/python",
        "sdk/python/",
        ".build_commit_sha",
        "**/.build_commit_sha",
        "gateway/scripts",
        "gateway/scripts/",
        "**/*.py",
    ),
)
def test_checker_rejects_excluded_image_inputs(
    image_source: Path, excluded_input: str
) -> None:
    ignore = image_source / ".dockerignore"
    ignore.write_text(ignore.read_text() + excluded_input + "\n", encoding="utf-8")
    assert "excludes image inputs" in _check(image_source).stderr


@pytest.mark.parametrize("name", ["COMMIT_SHA", "BUILD_COMMIT_SHA"])
def test_checker_rejects_legacy_provenance_args(image_source: Path, name: str) -> None:
    dockerfile = image_source / "gateway/Dockerfile"
    dockerfile.write_text(dockerfile.read_text() + f"ARG {name}\n", encoding="utf-8")
    assert "legacy provenance" in _check(image_source).stderr


def test_checker_rejects_missing_sdk_input(image_source: Path) -> None:
    (image_source / "sdk/python/src/b2a_sdk/receipt_verifier.py").unlink()
    assert "missing image input" in _check(image_source).stderr


def test_checker_executes_stamp_writer_validation(image_source: Path) -> None:
    writer = image_source / "gateway/scripts/write_build_commit_sha.py"
    writer.write_text(
        "import os\nfrom pathlib import Path\n"
        "Path(os.environ['BUILD_SHA_PATH']).write_text(os.environ.get('RELEASE_COMMIT_SHA', ''))\n"
    )
    assert "stamp writer accepted invalid" in _check(image_source).stderr
