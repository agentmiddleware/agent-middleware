"""The per-test ``clean_database`` fixture must keep up with the schema.

``clean_database`` (tests/conftest.py) empties an explicit list of tables
rather than rolling back a per-test transaction. A table added to
``app/db/models.py`` without a matching entry would silently stop being
cleaned, and rows left over from one test (a permit, a receipt, a ledger debit)
would leak into the next. These checks fail the moment the list drifts from
``SQLModel.metadata``.
"""

from __future__ import annotations

import app.db.models  # noqa: F401  (registers every table on SQLModel.metadata)
from sqlmodel import SQLModel

from tests.conftest import CLEAN_DATABASE_TABLES

# Tables clean_database deliberately leaves alone. All of them back example
# proof-surface workloads (telemetry, IoT, the API oracle, the red-team
# scanner, the content pipeline), not the trust plane, and none of them holds a
# foreign key into, or is referenced by, a cleaned table (pinned below). A new
# trust-plane table belongs in CLEAN_DATABASE_TABLES, not here.
PROOF_SURFACE_TABLES_EXEMPT = frozenset(
    {
        "telemetry_events",
        "iot_devices",
        "iot_device_events",
        "oracle_crawl_targets",
        "oracle_indexed_apis",
        "oracle_registrations",
        "oracle_discovery_hits",
        "security_scans",
        "security_vulnerabilities",
        "content_pipelines",
        "content_pieces",
        "content_campaigns",
        "content_schedules",
    }
)


def _foreign_key_parents(table_name: str) -> set[str]:
    table = SQLModel.metadata.tables[table_name]
    return {fk.column.table.name for fk in table.foreign_keys}


def test_every_table_is_cleaned_or_explicitly_exempt():
    declared = set(SQLModel.metadata.tables)
    cleaned = set(CLEAN_DATABASE_TABLES)

    missing = declared - cleaned - PROOF_SURFACE_TABLES_EXEMPT
    assert not missing, (
        f"tables not emptied by clean_database: {sorted(missing)}. Add them to "
        "CLEAN_DATABASE_TABLES in tests/conftest.py (children before parents)."
    )


def test_clean_and_exempt_lists_name_only_real_tables():
    declared = set(SQLModel.metadata.tables)
    assert len(CLEAN_DATABASE_TABLES) == len(set(CLEAN_DATABASE_TABLES)), (
        "CLEAN_DATABASE_TABLES lists a table twice"
    )
    assert not set(CLEAN_DATABASE_TABLES) - declared, (
        "CLEAN_DATABASE_TABLES names tables the schema no longer declares: "
        f"{sorted(set(CLEAN_DATABASE_TABLES) - declared)}"
    )
    assert not PROOF_SURFACE_TABLES_EXEMPT - declared, (
        f"stale exemption: {sorted(PROOF_SURFACE_TABLES_EXEMPT - declared)}"
    )
    assert not PROOF_SURFACE_TABLES_EXEMPT & set(CLEAN_DATABASE_TABLES), (
        "a table cannot be both cleaned and exempt: "
        f"{sorted(PROOF_SURFACE_TABLES_EXEMPT & set(CLEAN_DATABASE_TABLES))}"
    )


def test_clean_order_deletes_children_before_parents():
    """Each table must be emptied before every table it references.

    The fixture issues plain DELETEs with foreign keys enforced, so an
    out-of-order entry fails as soon as the child table holds a row. The
    self-reference on wallets is cleared by the fixture before its DELETE.
    """
    position = {name: index for index, name in enumerate(CLEAN_DATABASE_TABLES)}
    out_of_order = [
        f"{child} references {parent}, which is deleted first"
        for child, index in position.items()
        for parent in _foreign_key_parents(child)
        if parent != child and parent in position and position[parent] < index
    ]
    assert not out_of_order, out_of_order


def test_exempt_tables_are_not_linked_to_cleaned_tables():
    """An exempt row must neither block a cleaned DELETE nor be orphaned by one."""
    cleaned = set(CLEAN_DATABASE_TABLES)
    links = [
        f"{child} -> {parent}"
        for child in SQLModel.metadata.tables
        for parent in _foreign_key_parents(child)
        if (child in PROOF_SURFACE_TABLES_EXEMPT and parent in cleaned)
        or (child in cleaned and parent in PROOF_SURFACE_TABLES_EXEMPT)
    ]
    assert not links, links
