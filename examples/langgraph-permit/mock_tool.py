"""Tools for the local permit demonstration, with memory as the only storage."""

notes: list[dict[str, str]] = []


def append_note(note: str) -> dict[str, str]:
    """Append a note to local memory."""
    notes.append({"note": note})
    return {"status": "simulated"}


async def append_note_stub(note: str) -> dict[str, str]:
    """Append a note through the gateway."""
    raise AssertionError("the client stub must never execute")


async def delete_note_stub(note: str) -> dict[str, str]:
    """Delete a note through the gateway, if permitted."""
    raise AssertionError("the client stub must never execute")
