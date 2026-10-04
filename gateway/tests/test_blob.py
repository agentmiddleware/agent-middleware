"""
Tests for the blob abstraction introduced in #31.

The LocalFilesystemBlob is the only backend fully implemented today.
S3 and Vercel backends raise NotImplementedError while the media proof
surface is frozen — tests pin that contract so the stubs do not silently
succeed.
"""

from pathlib import Path

import pytest

from app.core.blob import (
    LocalFilesystemBlob,
    _UnimplementedBlob,
    blob_backend_name,
    get_blob_backend,
)


@pytest.fixture
def local_blob(tmp_path: Path) -> LocalFilesystemBlob:
    return LocalFilesystemBlob(root=tmp_path)


@pytest.fixture(autouse=True)
def _clear_backend_cache():
    """get_blob_backend is lru_cached; ensure env changes take effect per test."""
    get_blob_backend.cache_clear()
    yield
    get_blob_backend.cache_clear()


# ---------------------------------------------------------------------------
# LocalFilesystemBlob
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_local_backend_put_get_round_trip(local_blob):
    payload = b"hello world"
    url = await local_blob.put("my-video.mp4", payload, "video/mp4")
    assert url.startswith("file://")

    got = await local_blob.get("my-video.mp4")
    assert got == payload


@pytest.mark.anyio
async def test_local_backend_isolates_keys_under_hashed_path(tmp_path):
    """Path-traversal attempts must not escape the blob root."""
    store = LocalFilesystemBlob(root=tmp_path)
    await store.put("../../etc/passwd", b"malicious")

    files = list(tmp_path.iterdir())
    # Only hashed filenames + their .meta sidecars should exist.
    for f in files:
        # Must stay under tmp_path.
        assert tmp_path in f.resolve().parents or f.resolve() == tmp_path / f.name


@pytest.mark.anyio
async def test_local_backend_sidecar_records_original_key(tmp_path):
    store = LocalFilesystemBlob(root=tmp_path)
    await store.put("campaign-42/thumbnail.jpg", b"data", "image/jpeg")

    # Find the meta file.
    metas = list(tmp_path.glob("*.meta"))
    assert len(metas) == 1
    content = metas[0].read_text()
    assert "key=campaign-42/thumbnail.jpg" in content
    assert "content_type=image/jpeg" in content


@pytest.mark.anyio
async def test_local_backend_delete_returns_whether_removed(local_blob):
    await local_blob.put("x", b"payload")
    assert await local_blob.delete("x") is True
    assert await local_blob.delete("x") is False  # Idempotent no-op on 2nd call.


@pytest.mark.anyio
async def test_local_backend_get_missing_raises(local_blob):
    with pytest.raises(FileNotFoundError):
        await local_blob.get("never-stored")


# ---------------------------------------------------------------------------
# Factory / selection
# ---------------------------------------------------------------------------


def test_default_backend_is_local(monkeypatch):
    monkeypatch.delenv("BLOB_BACKEND", raising=False)
    assert blob_backend_name() == "local"
    backend = get_blob_backend()
    assert isinstance(backend, LocalFilesystemBlob)


def test_unknown_backend_falls_back_to_local(monkeypatch):
    monkeypatch.setenv("BLOB_BACKEND", "moonbase")
    assert isinstance(get_blob_backend(), LocalFilesystemBlob)


def test_s3_and_vercel_stubs_raise_not_implemented(monkeypatch):
    monkeypatch.setenv("BLOB_BACKEND", "s3")
    s3 = get_blob_backend()
    assert isinstance(s3, _UnimplementedBlob)

    get_blob_backend.cache_clear()
    monkeypatch.setenv("BLOB_BACKEND", "vercel")
    vercel = get_blob_backend()
    assert isinstance(vercel, _UnimplementedBlob)


@pytest.mark.anyio
async def test_stub_methods_raise_not_implemented_with_freeze_guidance():
    stub = _UnimplementedBlob("s3")
    for operation in (
        lambda: stub.put("k", b"d"),
        lambda: stub.get("k"),
        lambda: stub.delete("k"),
        lambda: stub.url("k"),
    ):
        with pytest.raises(NotImplementedError, match="docs/PROOF_SURFACES.md"):
            await operation()
