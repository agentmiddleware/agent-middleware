"""Public Agent Middleware SDK import surface."""

from __future__ import annotations

import warnings
from importlib import import_module
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from b2a_sdk import *  # noqa: F403


def _load_sdk() -> ModuleType:
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="b2a_sdk is deprecated; import amw instead",
            category=DeprecationWarning,
        )
        return import_module("b2a_sdk")


def __getattr__(name: str) -> object:
    sdk = _load_sdk()
    if name not in {*sdk.__all__, "__all__", "__version__"}:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(sdk, name)


def __dir__() -> list[str]:
    return sorted(_load_sdk().__all__)
