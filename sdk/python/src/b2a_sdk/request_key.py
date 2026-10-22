"""Deterministic request keys for the strict JSON action contract."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import warnings
from dataclasses import dataclass
from typing import Any

_DEFAULT_DROPPED = frozenset(
    {
        "timestamp",
        "ts",
        "nonce",
        "request_id",
        "trace_id",
        "attempt",
        "requested_at",
        "now",
        "note",
        "reason",
        "memo",
        "permit_id",
    }
)


@dataclass(frozen=True)
class RequestKeyExplanation:
    """The generated key and argument paths that did or did not enter it."""

    key: str
    used_fields: tuple[str, ...]
    dropped_fields: tuple[str, ...]


def _strict_json(value: Any) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for child in value:
            _strict_json(child)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for child in value.values():
            _strict_json(child)
        return
    raise ValueError("args must contain only finite JSON values")


def _field_paths(value: Any, path: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    if type(value) is list:
        return set().union(*(_field_paths(child, path) for child in value))
    if type(value) is not dict:
        return set()
    paths: set[tuple[str, ...]] = set()
    for name, child in value.items():
        child_path = (*path, name)
        paths.add(child_path)
        paths.update(_field_paths(child, child_path))
    return paths


def _paths(value: Any, path: tuple[str, ...]) -> list[str]:
    if type(value) is dict and value:
        return [field for name, child in value.items() for field in _paths(child, (*path, name))]
    if type(value) is list and value:
        return [field for child in value for field in _paths(child, path)]
    return [_display_path(path)]


def _display_path(path: tuple[str, ...]) -> str:
    return ".".join(part.replace("\\", "\\\\").replace(".", "\\.") for part in path)


def _split_path(path: str) -> tuple[str, ...]:
    parts: list[str] = []
    part: list[str] = []
    escaped = False
    for char in path:
        if escaped:
            if char not in (".", "\\"):
                raise ValueError("invalid field path escape")
            part.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ".":
            if not part:
                raise ValueError("empty field path segment")
            parts.append("".join(part))
            part = []
        else:
            part.append(char)
    if escaped or not part:
        raise ValueError("invalid field path ending")
    return (*parts, "".join(part))


def _parse_paths(
    paths: list[str] | tuple[str, ...] | None,
    label: str,
    available: set[tuple[str, ...]],
) -> set[tuple[str, ...]] | None:
    if paths is None:
        return None
    if type(paths) not in (list, tuple):
        raise ValueError(f"{label} must be a list of field paths")
    parsed: set[tuple[str, ...]] = set()
    for path in paths:
        if type(path) is not str or not path:
            raise ValueError(f"{label} contains an invalid field path")
        try:
            parts = _split_path(path)
        except ValueError as exc:
            raise ValueError(f"{label} contains an invalid field path: {path}") from exc
        if parts not in available:
            raise ValueError(f"{label} contains an unknown field path: {path}")
        parsed.add(parts)
    return parsed


def _matches(path: tuple[str, ...], selected: set[tuple[str, ...]]) -> bool:
    return any(path[: len(item)] == item or item[: len(path)] == path for item in selected)


def _filter_args(
    value: Any,
    path: tuple[str, ...],
    shown_path: tuple[str, ...],
    include: set[tuple[str, ...]] | None,
    exclude: set[tuple[str, ...]],
    dropped: list[str],
    unstable: list[str],
) -> Any:
    if type(value) is list:
        return [
            _filter_args(child, path, shown_path, include, exclude, dropped, unstable)
            for child in value
        ]
    if type(value) is not dict:
        return value
    kept: dict[str, Any] = {}
    for name, child in value.items():
        child_path = (*path, name)
        display_path = (*shown_path, name)
        default_drop = name in _DEFAULT_DROPPED and (
            include is None or not _matches(child_path, include)
        )
        omitted = (
            (include is not None and not _matches(child_path, include))
            or any(child_path[: len(item)] == item for item in exclude)
            or default_drop
        )
        if omitted:
            dropped.extend(_paths(child, display_path))
            if default_drop:
                unstable.append(_display_path(display_path))
            continue
        kept[name] = _filter_args(
            child, child_path, display_path, include, exclude, dropped, unstable
        )
    return kept


class _RequestKeyHelper:
    def __call__(
        self,
        tool: str,
        args: dict[str, Any],
        business_id: str | None = None,
        scope: str | None = None,
        include: list[str] | tuple[str, ...] | None = None,
        exclude: list[str] | tuple[str, ...] | None = None,
    ) -> str:
        return self.explain(tool, args, business_id, scope, include, exclude).key

    def explain(
        self,
        tool: str,
        args: dict[str, Any],
        business_id: str | None = None,
        scope: str | None = None,
        include: list[str] | tuple[str, ...] | None = None,
        exclude: list[str] | tuple[str, ...] | None = None,
    ) -> RequestKeyExplanation:
        """Return the key plus argument paths used and dropped during derivation."""
        if type(tool) is not str or not tool:
            raise ValueError("tool must be a nonempty string")
        if type(args) is not dict:
            raise ValueError("args must be a JSON object")
        _strict_json(args)
        for label, value in (("business_id", business_id), ("scope", scope)):
            if value is not None and (type(value) is not str or not value):
                raise ValueError(f"{label} must be a nonempty string or None")
        if include is not None and exclude is not None:
            raise ValueError("include and exclude cannot be used together")
        available = _field_paths(args)
        included = _parse_paths(include, "include", available)
        excluded = _parse_paths(exclude, "exclude", available) or set()
        if included is not None and not included:
            raise ValueError("include must name at least one field")
        dropped: list[str] = []
        unstable: list[str] = []
        canonical_args = _filter_args(args, (), (), included, excluded, dropped, unstable)
        for field in sorted(set(unstable)):
            warnings.warn(
                f"unstable_field: This field changes on every try, so we left it out of the key: "
                f"{field}. Nothing to do. To keep it, add it to include.",
                UserWarning,
                stacklevel=2,
            )
        if _contains_float(canonical_args):
            warnings.warn(
                "float_argument: Decimal numbers can change slightly between tries. "
                'Send amounts as text, like "12.00".',
                UserWarning,
                stacklevel=2,
            )
        payload = {
            "v": 1,
            "tool": tool,
            "args": canonical_args,
            "business_id": business_id,
            "scope": scope,
        }
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).digest()
        key = "amwk1_" + base64.b32encode(digest).decode("ascii").rstrip("=")[:52]
        used = sorted(set(_paths(canonical_args, ()))) if canonical_args else []
        return RequestKeyExplanation(key, tuple(used), tuple(sorted(set(dropped))))


def _contains_float(value: Any) -> bool:
    if type(value) is float:
        return True
    if type(value) is dict:
        return any(_contains_float(child) for child in value.values())
    if type(value) is list:
        return any(_contains_float(child) for child in value)
    return False


request_key = _RequestKeyHelper()

__all__ = ["RequestKeyExplanation", "request_key"]
