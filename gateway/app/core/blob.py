"""
PROOF SURFACE — frozen. Do not expand; see docs/PROOF_SURFACES.md.
Blob storage abstraction.

Media and content generation produce byte artifacts (video clips,
thumbnails, captioned renders). The database only stores references;
bytes live in a blob backend. This module defines the interface and
ships a local-filesystem backend for dev/tests. S3 and Vercel Blob remain
explicitly unsupported while the media proof surface is frozen.

Pluggable via ``BLOB_BACKEND`` env var:

- ``local`` (default) — ``./data/blobs/`` on disk
- ``s3``                — AWS S3 (not yet implemented)
- ``vercel``            — Vercel Blob (not yet implemented)

The interface is intentionally small:

    put(key, data, content_type) -> url
    get(key) -> bytes
    delete(key) -> bool
    url(key)   -> str
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from abc import ABC, abstractmethod
from functools import lru_cache
from pathlib import Path
from typing import Optional

from .config import get_settings

logger = logging.getLogger(__name__)


class BlobBackend(ABC):
    """Abstract blob storage backend."""

    @abstractmethod
    async def put(
        self, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> str:
        """Store ``data`` under ``key``. Return a URL or path callers can store."""

    @abstractmethod
    async def get(self, key: str) -> bytes:
        """Fetch the bytes at ``key``. Raises FileNotFoundError if missing."""

    @abstractmethod
    async def delete(self, key: str) -> bool:
        """Remove the object at ``key``. Returns True if something was removed."""

    @abstractmethod
    async def url(self, key: str) -> str:
        """Return a URL or path reference for ``key`` without fetching it."""


class LocalFilesystemBlob(BlobBackend):
    """
    Stores blobs under a directory on disk. Only suitable for dev and
    tests — not durable across deploys.

    Keys are hashed to avoid path traversal: a caller supplying
    ``../../etc/passwd`` gets a hex digest inside the blob root. The
    original key is recorded in a sidecar for debugging.
    """

    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root or "./data/blobs").resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.root / digest

    async def put(
        self, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> str:
        loop = asyncio.get_running_loop()
        path = self._path(key)

        def _write() -> None:
            path.write_bytes(data)
            # Sidecar with the original key + content type for inspection.
            path.with_suffix(".meta").write_text(
                f"key={key}\ncontent_type={content_type}\n"
            )

        await loop.run_in_executor(None, _write)
        return self._to_url(path)

    async def get(self, key: str) -> bytes:
        loop = asyncio.get_running_loop()
        path = self._path(key)
        if not path.exists():
            raise FileNotFoundError(key)
        return await loop.run_in_executor(None, path.read_bytes)

    async def delete(self, key: str) -> bool:
        loop = asyncio.get_running_loop()
        path = self._path(key)

        def _rm() -> bool:
            removed = False
            if path.exists():
                path.unlink()
                removed = True
            meta = path.with_suffix(".meta")
            if meta.exists():
                meta.unlink()
            return removed

        return await loop.run_in_executor(None, _rm)

    async def url(self, key: str) -> str:
        return self._to_url(self._path(key))

    @staticmethod
    def _to_url(path: Path) -> str:
        return f"file://{path}"


class _UnimplementedBlob(BlobBackend):
    """Unsupported backend retained to fail loudly on invalid production use."""

    def __init__(self, name: str):
        self._name = name

    async def put(self, key, data, content_type="application/octet-stream"):  # type: ignore[override]
        raise NotImplementedError(
            f"{self._name} blob backend is outside the frozen proof-surface scope; "
            "see docs/PROOF_SURFACES.md"
        )

    async def get(self, key):  # type: ignore[override]
        raise NotImplementedError(
            f"{self._name} blob backend is outside the frozen proof-surface scope; "
            "see docs/PROOF_SURFACES.md"
        )

    async def delete(self, key):  # type: ignore[override]
        raise NotImplementedError(
            f"{self._name} blob backend is outside the frozen proof-surface scope; "
            "see docs/PROOF_SURFACES.md"
        )

    async def url(self, key):  # type: ignore[override]
        raise NotImplementedError(
            f"{self._name} blob backend is outside the frozen proof-surface scope; "
            "see docs/PROOF_SURFACES.md"
        )


@lru_cache()
def get_blob_backend() -> BlobBackend:
    """Resolve the configured blob backend. Cached per-process."""
    backend_name = os.environ.get("BLOB_BACKEND", "local").strip().lower()

    if backend_name == "local":
        root = os.environ.get("BLOB_ROOT")
        return LocalFilesystemBlob(Path(root) if root else None)

    if backend_name == "s3":
        logger.warning(
            "S3 blob backend selected but not implemented; falling back to local"
        )
        return _UnimplementedBlob("s3")

    if backend_name == "vercel":
        logger.warning(
            "Vercel blob backend selected but unsupported while media is frozen"
        )
        return _UnimplementedBlob("vercel")

    logger.warning(
        "Unknown BLOB_BACKEND=%r, falling back to local filesystem", backend_name
    )
    return LocalFilesystemBlob()


# Surface settings for debugging.
def blob_backend_name() -> str:
    return os.environ.get("BLOB_BACKEND", "local").strip().lower()


# Ensure get_settings is importable here so downstream tooling picks up
# config changes; unused directly, retained for side-effects only if any.
_ = get_settings  # noqa: F841
