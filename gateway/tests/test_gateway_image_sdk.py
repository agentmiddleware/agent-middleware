"""Gateway images must ship the in-repo SDK that app import loads.

``app.main`` imports ``mcp_public``, which imports ``b2a_sdk.receipt_verifier``
at import time. The SDK is not inside ``gateway/``. A build whose context is
only that directory installs requirements.txt, then fails when uvicorn loads
``app.main:app``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.routers import mcp_public

GATEWAY = Path(__file__).resolve().parents[1]
REPO = GATEWAY.parent
_FILE_LOADER = "_agent_middleware_receipt_verifier"


def _dockerfile(name: str) -> str:
    return (GATEWAY / name).read_text(encoding="utf-8")


def test_checkout_still_has_the_verifier_the_fallback_loads():
    assert mcp_public._CHECKOUT_SDK_RECEIPT_VERIFIER.is_file()
    assert mcp_public._IMAGE_SDK_RECEIPT_VERIFIER == Path(
        "/sdk/python/src/b2a_sdk/receipt_verifier.py"
    )
    # Inside the image the module is /app/app/routers/mcp_public.py, so
    # parents[3] is /. That is the same file the Dockerfiles copy into place.
    container_module = Path("/app/app/routers/mcp_public.py")
    derived = (
        container_module.parents[3]
        / "sdk"
        / "python"
        / "src"
        / "b2a_sdk"
        / "receipt_verifier.py"
    )
    assert derived == mcp_public._IMAGE_SDK_RECEIPT_VERIFIER
    # On a host checkout the two paths differ, and the repository file wins
    # over a stray /sdk tree. In the image they are the same path.
    assert mcp_public._receipt_verifier_file_candidates() == (
        mcp_public._CHECKOUT_SDK_RECEIPT_VERIFIER,
        mcp_public._IMAGE_SDK_RECEIPT_VERIFIER,
    )


def test_image_build_context_contains_the_sdk():
    compose = (GATEWAY / "docker-compose.yml").read_text(encoding="utf-8")
    assert "context: .." in compose
    assert "dockerfile: gateway/Dockerfile.dev" in compose
    context = (GATEWAY / "..").resolve()
    assert context == REPO
    assert (context / "sdk/python/pyproject.toml").is_file()
    assert (context / "sdk/python/src/b2a_sdk/receipt_verifier.py").is_file()
    assert (context / "gateway/requirements.txt").is_file()


@pytest.mark.parametrize("name", ["Dockerfile", "Dockerfile.dev"])
def test_images_install_sdk_and_put_it_on_pythonpath(name: str):
    text = _dockerfile(name)
    sdk_copy = text.index("COPY sdk/python /sdk/python")
    requirements = text.index("COPY gateway/requirements.txt /tmp/requirements.txt")
    install = text.index("pip install --no-cache-dir /sdk/python")
    user = text.index("\nUSER app\n")
    assert "PYTHONPATH=/sdk/python/src" in text
    assert "COPY gateway/ /app/" in text
    assert sdk_copy < install < user
    assert requirements < install


def test_release_dockerfile_validates_root_stamp_before_final_copy():
    text = _dockerfile("Dockerfile")
    assert "COPY . /context/" in text
    assert "STAGED_SHA_PATH=/context/.build_commit_sha" in text
    assert "BUILD_SHA_PATH=/stamp/.build_commit_sha" in text
    assert "python /context/gateway/scripts/write_build_commit_sha.py" in text
    assert "COPY .build_commit_sha /app/.build_commit_sha" not in text
    assert text.index("COPY gateway/ /app/") < text.index(
        "COPY --from=build-metadata /stamp/.build_commit_sha /app/.build_commit_sha"
    )


def test_development_image_does_not_require_or_copy_a_stamp():
    text = _dockerfile("Dockerfile.dev")
    assert "RELEASE_COMMIT_SHA" not in text
    assert "write_build_commit_sha.py" not in text
    assert "COPY --from=build-metadata" not in text
    assert "COPY .build_commit_sha" not in text


def test_root_dockerignore_keeps_sdk_sources_and_the_stamp():
    lines = [
        line.strip()
        for line in (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert "sdk" not in lines
    assert "sdk/python" not in lines
    assert "sdk/python/src" not in lines
    assert ".build_commit_sha" not in lines
    assert "gateway/tests" in lines
    assert ".env" in lines
    assert "**/.env" in lines
    assert "gateway/.build_commit_sha" in lines
    assert "*.pem" in lines
    assert "**/*.key" in lines


def _package_not_installed(_name: str):
    raise ImportError("b2a_sdk is not installed")


def test_fallback_uses_the_image_file_when_the_package_is_not_installed(
    monkeypatch, tmp_path
):
    image_copy = tmp_path / "receipt_verifier.py"
    image_copy.write_text(
        mcp_public._CHECKOUT_SDK_RECEIPT_VERIFIER.read_text(encoding="utf-8")
        + "\nMARKER = 'image-layout'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mcp_public.importlib, "import_module", _package_not_installed)
    monkeypatch.setattr(
        mcp_public,
        "_receipt_verifier_file_candidates",
        lambda: (image_copy, Path("/no/such/checkout/receipt_verifier.py")),
    )
    sys.modules.pop(_FILE_LOADER, None)

    loaded = mcp_public._import_receipt_verifier()

    assert loaded.MARKER == "image-layout"
    assert hasattr(loaded, "verify_bundle")


def test_fallback_reraises_the_original_import_error_when_no_file_exists(monkeypatch):
    monkeypatch.setattr(mcp_public.importlib, "import_module", _package_not_installed)
    monkeypatch.setattr(
        mcp_public,
        "_receipt_verifier_file_candidates",
        lambda: (Path("/no/such/image/receipt_verifier.py"),),
    )

    with pytest.raises(ImportError, match="b2a_sdk is not installed"):
        mcp_public._import_receipt_verifier()
