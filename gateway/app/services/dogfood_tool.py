"""Opt-in executable dogfood tool for live trust-loop demos.

When ``ENABLE_DOGFOOD_TOOL=true``, registers ``partner.notes.write`` as a
local MCP tool that appends a note to a JSONL file under permit control.

When ``ENABLE_DOGFOOD_SECOND_TOOL=true``, also registers ``partner.notes.count``
as a second harmless tool that returns the current note count (read-only, no
side effects). This second tool is used in CI to prove out-of-scope denial when
the permit only allows the first tool.

Default is off so production discovery stays empty of demo tools unless
ops explicitly enables the flag. This is independent of
``ENABLE_PROOF_SURFACES`` (which gates Phase9 / marketplace stubs).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from ..schemas.billing import ServiceCategory
from .service_registry import get_service_registry

logger = logging.getLogger(__name__)

DOGFOOD_TOOL_ID = "partner.notes.write"
DOGFOOD_TOOL_NAME = "Partner Notes Write"
DOGFOOD_SECOND_TOOL_ID = "partner.notes.count"
DOGFOOD_SECOND_TOOL_NAME = "Partner Notes Count"
DOGFOOD_NOTES_PATH = Path("data") / "dogfood_partner_notes.jsonl"
DOGFOOD_CREDITS_PER_UNIT = 2.0
DOGFOOD_MAX_TEXT_CHARS = 2_000
DOGFOOD_MAX_NOTES = 1_000

_registered = False
_second_registered = False
_registration_lock = Lock()
_note_count: int | None = None


def _load_note_count() -> int:
    if not DOGFOOD_NOTES_PATH.exists():
        return 0
    return sum(
        1
        for line in DOGFOOD_NOTES_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


def _write_note(text: str = "hello") -> dict[str, Any]:
    """Append one governed dogfood note (safe side effect: local JSONL)."""
    global _note_count
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    if len(text) > DOGFOOD_MAX_TEXT_CHARS:
        raise ValueError(
            f"text exceeds max length of {DOGFOOD_MAX_TEXT_CHARS} characters"
        )

    with _registration_lock:
        if _note_count is None:
            _note_count = _load_note_count()
        if _note_count >= DOGFOOD_MAX_NOTES:
            raise ValueError(
                f"dogfood notes file is full (max {DOGFOOD_MAX_NOTES} notes); "
                "rotate or delete data/dogfood_partner_notes.jsonl"
            )
        DOGFOOD_NOTES_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "text": text,
            "written_at": datetime.now(timezone.utc).isoformat(),
            "governed": True,
        }
        with DOGFOOD_NOTES_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
        _note_count += 1
        note_count = _note_count

    return {
        "status": "ok",
        "tool": DOGFOOD_TOOL_ID,
        "note": entry,
        "notes_path": str(DOGFOOD_NOTES_PATH),
        "note_count": note_count,
    }


def _count_notes() -> dict[str, Any]:
    """Count dogfood notes (read-only, no side effects; for out-of-scope denial test)."""
    global _note_count
    with _registration_lock:
        if _note_count is None:
            _note_count = _load_note_count()
        count = _note_count

    return {
        "status": "ok",
        "tool": DOGFOOD_SECOND_TOOL_ID,
        "note_count": count,
        "notes_path": str(DOGFOOD_NOTES_PATH),
    }


def register_dogfood_tool() -> None:
    """Register the executable dogfood tool in the local MCP registry."""
    global _registered
    if _registered:
        return

    with _registration_lock:
        if _registered:
            return
        registry = get_service_registry()
        registry.register_local(
            service_id=DOGFOOD_TOOL_ID,
            name=DOGFOOD_TOOL_NAME,
            description=(
                "Dogfood stand-in for one internal MCP tool. "
                "Appends a note to a local JSONL file under permit control. "
                "Opt-in via ENABLE_DOGFOOD_TOOL; not a production partner tool."
            ),
            category=ServiceCategory.AGENT_COMMS,
            func=_write_note,
            credits_per_unit=DOGFOOD_CREDITS_PER_UNIT,
            unit_name="call",
            require_permit=True,
        )
        _registered = True
        logger.info("Registered dogfood MCP tool: %s", DOGFOOD_TOOL_ID)


def register_dogfood_second_tool() -> None:
    """Register the second harmless dogfood tool (read-only note counter)."""
    global _second_registered
    if _second_registered:
        return

    with _registration_lock:
        if _second_registered:
            return
        registry = get_service_registry()
        registry.register_local(
            service_id=DOGFOOD_SECOND_TOOL_ID,
            name=DOGFOOD_SECOND_TOOL_NAME,
            description=(
                "Harmless read-only tool that returns the current dogfood note count. "
                "Used in CI to test out-of-scope denial when permit only allows the first tool. "
                "Opt-in via ENABLE_DOGFOOD_SECOND_TOOL; not a production partner tool."
            ),
            category=ServiceCategory.AGENT_COMMS,
            func=_count_notes,
            credits_per_unit=1.0,
            unit_name="call",
            require_permit=True,
        )
        _second_registered = True
        logger.info("Registered second dogfood MCP tool: %s", DOGFOOD_SECOND_TOOL_ID)


def unregister_dogfood_tool() -> None:
    """Remove the dogfood tool from the local registry."""
    global _registered, _note_count
    registry = get_service_registry()
    with _registration_lock:
        registry.unregister_local(DOGFOOD_TOOL_ID)
        _registered = False
        _note_count = None


def unregister_dogfood_second_tool() -> None:
    """Remove the second dogfood tool from the local registry."""
    global _second_registered
    registry = get_service_registry()
    with _registration_lock:
        registry.unregister_local(DOGFOOD_SECOND_TOOL_ID)
        _second_registered = False


def sync_dogfood_tool_registration() -> None:
    """Register or unregister the dogfood tools to match ENABLE_DOGFOOD_TOOL / ENABLE_DOGFOOD_SECOND_TOOL.

    When the flag is off, only remove the tool if *this* module registered it.
    Scripts (``dogfood_trust_plane``) and tests may register the same id
    independently; discovery sync must not delete those registrations.
    """
    from ..core.config import get_settings

    settings = get_settings()

    if settings.ENABLE_DOGFOOD_TOOL:
        register_dogfood_tool()
    elif _registered:
        unregister_dogfood_tool()

    if settings.ENABLE_DOGFOOD_SECOND_TOOL:
        register_dogfood_second_tool()
    elif _second_registered:
        unregister_dogfood_second_tool()
