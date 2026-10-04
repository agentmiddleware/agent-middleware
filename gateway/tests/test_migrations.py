"""Migration coverage for production schema drift."""

import asyncio
import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


def test_alembic_upgrade_creates_auth_schema(tmp_path, monkeypatch):
    db_path = tmp_path / "migration.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    sync_url = f"sqlite:///{db_path}"

    monkeypatch.setenv("DATABASE_URL", async_url)

    config = Config("alembic.ini")
    command.upgrade(config, "head")
    asyncio.set_event_loop(asyncio.new_event_loop())

    engine = create_engine(sync_url)
    inspector = inspect(engine)

    tables = set(inspector.get_table_names())
    assert {
        "api_keys",
        "key_rotation_logs",
        "kyc_verifications",
        "service_registry",
        "signing_keys",
        "permits",
        "receipts",
        "idempotency_records",
        "mcp_dispatch_attempts",
        "human_approvals",
    } <= tables

    permit_columns = {col["name"] for col in inspector.get_columns("permits")}
    assert "requires_human_approval" in permit_columns
    receipt_columns = {col["name"] for col in inspector.get_columns("receipts")}
    assert "approval_id" in receipt_columns
    assert {"idempotency_record_id", "dispatch_attempt_id"} <= receipt_columns

    ledger_columns = {col["name"] for col in inspector.get_columns("ledger_entries")}
    assert "operation_key" in ledger_columns

    idempotency_columns = {
        col["name"] for col in inspector.get_columns("idempotency_records")
    }
    assert "operation_kind" in idempotency_columns

    dispatch_columns = {
        col["name"] for col in inspector.get_columns("mcp_dispatch_attempts")
    }
    assert {
        "idempotency_record_id",
        "approval_id",
        "ledger_entry_id",
        "state",
        "dispatch_claim_hash",
        "result_json",
        "result_size_bytes",
        "response_hash",
    } <= dispatch_columns
    dispatch_foreign_keys = inspector.get_foreign_keys("mcp_dispatch_attempts")
    assert any(
        foreign_key["constrained_columns"] == ["approval_id"]
        and foreign_key["referred_table"] == "human_approvals"
        for foreign_key in dispatch_foreign_keys
    )
    dispatch_indexes = inspector.get_indexes("mcp_dispatch_attempts")
    assert any(
        index["name"] == "ix_mcp_dispatch_attempts_approval_id"
        and index["column_names"] == ["approval_id"]
        for index in dispatch_indexes
    )

    wallet_columns = {col["name"] for col in inspector.get_columns("wallets")}
    assert {
        "child_agent_id",
        "max_spend",
        "task_description",
        "ttl_seconds",
        "kyc_status",
        "kyc_verified_at",
    } <= wallet_columns
    # Revision 025 scrubs these compatibility columns but retains their shape
    # for one rolling release so the previous worker can drain safely.
    assert "owner_key" in wallet_columns

    service_columns = {col["name"] for col in inspector.get_columns("service_registry")}
    assert "owner_key" in service_columns

    audit_columns = {
        col["name"] for col in inspector.get_columns("control_plane_audit_events")
    }
    assert {
        "payload_hash",
        "previous_hash",
        "chain_hash",
        "signature",
        "signature_key_id",
    } <= audit_columns

    engine.dispose()
    os.remove(db_path)


def test_governed_persistence_migration_backfills_only_unambiguous_receipts(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "governed-persistence-migration.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    sync_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", async_url)
    config = Config("alembic.ini")
    command.upgrade(config, "025_remove_plaintext_owner_keys")

    engine = create_engine(sync_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO wallets (
                    wallet_id, wallet_type, balance, lifetime_credits,
                    lifetime_debits, daily_spent, auto_refill, status
                ) VALUES ('agt-migration', 'agent', 100, 100, 0, 0, 0, 'active')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO signing_keys (
                    key_id, alg, public_key_b64, status
                ) VALUES ('sig-migration', 'Ed25519', 'public', 'active')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO permits (
                    permit_id, issuer_wallet_id, subject_wallet_id,
                    scopes_json, allowed_tools_json, max_credits,
                    spent_credits, expires_at, nonce, status, signature, key_id
                ) VALUES (
                    'permit-migration', 'agt-migration', 'agt-migration',
                    '[]', '[]', 10, 0, '2030-01-01 00:00:00',
                    'nonce-migration', 'active', 'signature', 'sig-migration'
                )
                """
            )
        )
        for receipt_id in (
            "receipt-unambiguous",
            "receipt-ambiguous",
            "receipt-mismatched",
        ):
            connection.execute(
                text(
                    """
                    INSERT INTO receipts (
                        receipt_id, permit_id, wallet_id, tool, request_hash,
                        credits_authorized, credits_charged, outcome,
                        signature, signature_key_id
                    ) VALUES (
                        :receipt_id, 'permit-migration', 'agt-migration',
                        'partner-tool', :request_hash, 1, 0, 'denied',
                        'signature', 'sig-migration'
                    )
                    """
                ),
                {"receipt_id": receipt_id, "request_hash": "a" * 64},
            )
        for record_id, idem_key, receipt_id in (
            ("idem-unambiguous", "key-one", "receipt-unambiguous"),
            ("idem-ambiguous-a", "key-two", "receipt-ambiguous"),
            ("idem-ambiguous-b", "key-three", "receipt-ambiguous"),
            ("idem-mismatched", "key-four", "receipt-mismatched"),
        ):
            connection.execute(
                text(
                    """
                    INSERT INTO idempotency_records (
                        record_id, wallet_id, endpoint, idempotency_key,
                        request_hash, response_reference, status_code
                    ) VALUES (
                        :record_id, 'agt-migration', '/mcp/messages', :idem_key,
                        :request_hash, :receipt_id, 200
                    )
                    """
                ),
                {
                    "record_id": record_id,
                    "idem_key": idem_key,
                    "request_hash": (
                        "b" * 64 if record_id == "idem-mismatched" else "a" * 64
                    ),
                    "receipt_id": receipt_id,
                },
            )
    engine.dispose()

    command.upgrade(config, "head")
    asyncio.set_event_loop(asyncio.new_event_loop())

    engine = create_engine(sync_url)
    with engine.connect() as connection:
        rows = dict(
            connection.execute(
                text(
                    """
                    SELECT receipt_id, idempotency_record_id
                    FROM receipts
                    ORDER BY receipt_id
                    """
                )
            ).all()
        )
    assert rows["receipt-unambiguous"] == "idem-unambiguous"
    assert rows["receipt-ambiguous"] is None
    assert rows["receipt-mismatched"] is None
    engine.dispose()
    os.remove(db_path)


def test_governed_mcp_identity_migration_enforces_cross_endpoint_uniqueness(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "governed-mcp-identity-migration.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    sync_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", async_url)
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    asyncio.set_event_loop(asyncio.new_event_loop())

    engine = create_engine(sync_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO wallets (
                    wallet_id, wallet_type, balance, lifetime_credits,
                    lifetime_debits, daily_spent, auto_refill, status
                ) VALUES (
                    'agt-identity-migration', 'agent', 100, 100, 0, 0, 0, 'active'
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO idempotency_records (
                    record_id, wallet_id, endpoint, idempotency_key,
                    request_hash, status_code
                ) VALUES (
                    'idm-identity-legacy', 'agt-identity-migration',
                    '/mcp/messages', 'shared-identity-key', :request_hash, 200
                )
                """
            ),
            {"request_hash": "a" * 64},
        )
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    """
                    INSERT INTO idempotency_records (
                        record_id, wallet_id, endpoint, idempotency_key,
                        request_hash, status_code
                    ) VALUES (
                        'idm-identity-canonical', 'agt-identity-migration',
                        '/mcp/invoke', 'shared-identity-key', :request_hash, 200
                    )
                    """
                ),
                {"request_hash": "b" * 64},
            )
    engine.dispose()
    os.remove(db_path)


def test_governed_mcp_identity_migration_refuses_ambiguous_history(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "governed-mcp-identity-conflict.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    sync_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", async_url)
    config = Config("alembic.ini")
    command.upgrade(config, "026_governed_mcp_persistence")

    engine = create_engine(sync_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO wallets (
                    wallet_id, wallet_type, balance, lifetime_credits,
                    lifetime_debits, daily_spent, auto_refill, status
                ) VALUES (
                    'agt-identity-conflict', 'agent', 100, 100, 0, 0, 0, 'active'
                )
                """
            )
        )
        for record_id, endpoint in (
            ("idm-identity-conflict-legacy", "/mcp/messages"),
            ("idm-identity-conflict-current", "/mcp/invoke"),
        ):
            connection.execute(
                text(
                    """
                    INSERT INTO idempotency_records (
                        record_id, wallet_id, endpoint, idempotency_key,
                        request_hash, status_code
                    ) VALUES (
                        :record_id, 'agt-identity-conflict', :endpoint,
                        'ambiguous-history-key', :request_hash, 200
                    )
                    """
                ),
                {
                    "record_id": record_id,
                    "endpoint": endpoint,
                    "request_hash": "a" * 64,
                },
            )
    engine.dispose()

    with pytest.raises(
        RuntimeError,
        match=r"1 ambiguous wallet/key group\(s\)",
    ) as exc_info:
        command.upgrade(config, "head")
    assert "agt-identity-conflict" not in str(exc_info.value)
    assert "ambiguous-history-key" not in str(exc_info.value)
    asyncio.set_event_loop(asyncio.new_event_loop())

    engine = create_engine(sync_url)
    with engine.connect() as connection:
        revision = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        installed_index = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM sqlite_master
                WHERE type = 'index'
                  AND name = 'uq_idempotency_governed_mcp_identity'
                """
            )
        ).scalar_one()
    assert revision == "026_governed_mcp_persistence"
    assert installed_index == 0
    engine.dispose()
    os.remove(db_path)


def test_owner_key_migration_scrubs_values_but_retains_rolling_compatible_columns(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "owner-key-migration.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    sync_url = f"sqlite:///{db_path}"
    sentinel = "b2a_live_secret_that_must_not_survive"

    monkeypatch.setenv("DATABASE_URL", async_url)
    config = Config("alembic.ini")
    command.upgrade(config, "021_ledger_stripe_event_id")

    engine = create_engine(sync_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO wallets (
                    wallet_id,
                    wallet_type,
                    owner_key,
                    balance,
                    lifetime_credits,
                    lifetime_debits,
                    daily_spent,
                    auto_refill,
                    status
                ) VALUES (
                    :wallet_id,
                    'sponsor',
                    :owner_key,
                    0,
                    0,
                    0,
                    0,
                    0,
                    'active'
                )
                """
            ),
            {"wallet_id": "spn-owner-key-migration", "owner_key": sentinel},
        )
        connection.execute(
            text(
                """
                INSERT INTO service_registry (
                    service_id,
                    name,
                    owner_wallet_id,
                    owner_key,
                    category,
                    credits_per_unit
                ) VALUES (
                    'svc-owner-key-migration',
                    'Legacy service',
                    :wallet_id,
                    :owner_key,
                    'agent_comms',
                    1
                )
                """
            ),
            {"wallet_id": "spn-owner-key-migration", "owner_key": sentinel},
        )
    engine.dispose()

    command.upgrade(config, "head")
    asyncio.set_event_loop(asyncio.new_event_loop())

    engine = create_engine(sync_url)
    inspector = inspect(engine)
    assert "owner_key" in {
        column["name"] for column in inspector.get_columns("wallets")
    }
    assert "owner_key" in {
        column["name"] for column in inspector.get_columns("service_registry")
    }
    with engine.connect() as connection:
        wallet = connection.execute(
            text(
                """
                SELECT wallet_id, owner_key
                FROM wallets
                WHERE wallet_id = :wallet_id
                """
            ),
            {"wallet_id": "spn-owner-key-migration"},
        ).one()
        service = connection.execute(
            text(
                """
                SELECT owner_wallet_id, owner_key
                FROM service_registry
                WHERE service_id = 'svc-owner-key-migration'
                """
            )
        ).one()
        assert wallet == (
            "spn-owner-key-migration",
            "",
        )
        assert service == (
            "spn-owner-key-migration",
            "",
        )
        assert sentinel not in repr((wallet, service))
    engine.dispose()


def test_refresh_token_binding_migration_revokes_unbound_rows(tmp_path, monkeypatch):
    """Historical refresh tokens cannot be safely assigned to a live API key."""
    db_path = tmp_path / "refresh-token-binding-migration.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    sync_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", async_url)
    config = Config("alembic.ini")
    # Reproduce the real upgrade path: revision 025 is already published, so
    # the data fix must live in a later migration.
    command.upgrade(config, "025_refresh_token_key_binding")

    engine = create_engine(sync_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO wallets (
                    wallet_id, wallet_type, owner_key, balance, lifetime_credits,
                    lifetime_debits, daily_spent, auto_refill, status
                ) VALUES (
                    'agt-legacy-refresh', 'agent', '', 0, 0, 0, 0, 0, 'active'
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO refresh_tokens (
                    jti, wallet_id, key_id, revoked, created_at, expires_at
                ) VALUES (
                    'legacy-refresh-jti', 'agt-legacy-refresh', NULL, 0,
                    '2026-08-01 00:00:00', '2026-08-08 00:00:00'
                ), (
                    'bound-refresh-jti', 'agt-legacy-refresh', 'key-live', 0,
                    '2026-08-01 00:00:00', '2026-08-08 00:00:00'
                )
                """
            )
        )
    engine.dispose()

    command.upgrade(config, "head")
    asyncio.set_event_loop(asyncio.new_event_loop())

    engine = create_engine(sync_url)
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT jti, key_id, revoked
                FROM refresh_tokens
                ORDER BY jti
                """
            )
        ).all()
    assert rows == [
        ("bound-refresh-jti", "key-live", 0),
        ("legacy-refresh-jti", None, 1),
    ]
    engine.dispose()


def test_024_repairs_sqlite_boolean_backfill(tmp_path, monkeypatch):
    """Verify 024 repairs 023's SQLite text-boolean backfill."""
    from sqlalchemy import Boolean, Column, MetaData, String, Table, select

    db_path = tmp_path / "repair.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    sync_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", async_url)
    config = Config("alembic.ini")

    command.upgrade(config, "023_human_approval_gate")
    asyncio.set_event_loop(asyncio.new_event_loop())
    engine = create_engine(sync_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO permits (permit_id, issuer_wallet_id, "
                "subject_wallet_id, scopes_json, allowed_tools_json, max_credits, "
                "expires_at, nonce, status, requires_human_approval, signature, "
                "key_id, issued_at) VALUES ('p-legacy', 'w', 'w', '[]', '[]', 10, "
                "'2999-01-01 00:00:00', 'n', 'active', 'false', 'sig', 'k', "
                "'2026-01-01 00:00:00')"
            )
        )

    metadata = MetaData()
    permits = Table(
        "permits",
        metadata,
        Column("permit_id", String, primary_key=True),
        Column("requires_human_approval", Boolean),
    )
    with engine.connect() as connection:
        buggy = connection.execute(
            select(permits.c.requires_human_approval).where(
                permits.c.permit_id == "p-legacy"
            )
        ).scalar_one()
    assert buggy is True, "precondition: 023 leaves a truthy text value on SQLite"

    command.upgrade(config, "024_human_approval_hardening")
    with engine.connect() as connection:
        fixed = connection.execute(
            select(permits.c.requires_human_approval).where(
                permits.c.permit_id == "p-legacy"
            )
        ).scalar_one()
    assert fixed is False, "024 must normalize the backfilled text 'false' to False"

    approval_columns = {
        column["name"] for column in inspect(engine).get_columns("human_approvals")
    }
    assert "request_hash" in approval_columns
    engine.dispose()


def test_040_repeat_window_seconds_column_upgrade_and_downgrade(tmp_path, monkeypatch):
    """Migration 040 preserves legacy permit data and adds a nullable window."""
    db_path = tmp_path / "repeat-window-migration.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    sync_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", async_url)
    config = Config("alembic.ini")

    # Upgrade to 039 (before repeat_window_seconds)
    command.upgrade(config, "039_permit_allow_ident_repeats")
    asyncio.set_event_loop(asyncio.new_event_loop())

    engine = create_engine(sync_url)
    inspector = inspect(engine)
    permit_columns_before = {col["name"] for col in inspector.get_columns("permits")}
    assert "repeat_window_seconds" not in permit_columns_before
    assert "allow_identical_repeats" in permit_columns_before
    # Persist the legacy permit before the schema upgrade, as on production 039.
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO wallets (
                    wallet_id, wallet_type, balance, lifetime_credits,
                    lifetime_debits, daily_spent, auto_refill, status
                ) VALUES ('agt-repeat-window', 'agent', 100, 100, 0, 0, 0, 'active')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO signing_keys (
                    key_id, alg, public_key_b64, status
                ) VALUES ('sig-repeat-window', 'Ed25519', 'public', 'active')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO permits (
                    permit_id, issuer_wallet_id, subject_wallet_id,
                    scopes_json, allowed_tools_json, max_credits,
                    spent_credits, expires_at, nonce, status, signature, key_id,
                    issued_at
                ) VALUES (
                    'permit-without-window', 'agt-repeat-window', 'agt-repeat-window',
                    '[]', '[]', 10, 0, '2030-01-01 00:00:00',
                    'nonce-without-window', 'active', 'signature', 'sig-repeat-window',
                    '2026-01-01 00:00:00'
                )
                """
            )
        )
        legacy_before = (
            connection.execute(
                text("SELECT * FROM permits WHERE permit_id = 'permit-without-window'")
            )
            .mappings()
            .one()
        )
    engine.dispose()

    command.upgrade(config, "040_permit_repeat_window")
    command.upgrade(config, "040_permit_repeat_window")  # Restart migration is a no-op.
    asyncio.set_event_loop(asyncio.new_event_loop())

    engine = create_engine(sync_url)
    permit_columns_after = {
        column["name"]: column for column in inspect(engine).get_columns("permits")
    }
    assert permit_columns_after["repeat_window_seconds"]["nullable"] is True
    with engine.begin() as connection:
        legacy_after = (
            connection.execute(
                text("SELECT * FROM permits WHERE permit_id = 'permit-without-window'")
            )
            .mappings()
            .one()
        )
        assert dict(legacy_after) == {**legacy_before, "repeat_window_seconds": None}
        assert (
            connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            == "040_permit_repeat_window"
        )
        connection.execute(
            text(
                """
                INSERT INTO permits (
                    permit_id, issuer_wallet_id, subject_wallet_id,
                    scopes_json, allowed_tools_json, max_credits,
                    spent_credits, expires_at, nonce, status, signature, key_id,
                    issued_at, repeat_window_seconds
                ) VALUES (
                    'permit-with-window', 'agt-repeat-window', 'agt-repeat-window',
                    '[]', '[]', 10, 0, '2030-01-01 00:00:00',
                    'nonce-with-window', 'active', 'signature', 'sig-repeat-window',
                    '2026-01-01 00:00:00', 3600
                )
                """
            )
        )

    with engine.connect() as connection:
        with_window = connection.execute(
            text(
                "SELECT repeat_window_seconds FROM permits WHERE permit_id = 'permit-with-window'"
            )
        ).scalar_one()
        without_window = connection.execute(
            text(
                "SELECT repeat_window_seconds FROM permits WHERE permit_id = 'permit-without-window'"
            )
        ).scalar_one()

    assert with_window == 3600
    assert without_window is None
    engine.dispose()

    # Downgrade to 039 (removes repeat_window_seconds)
    command.downgrade(config, "039_permit_allow_ident_repeats")
    asyncio.set_event_loop(asyncio.new_event_loop())

    engine = create_engine(sync_url)
    inspector = inspect(engine)
    permit_columns_downgraded = {
        col["name"] for col in inspector.get_columns("permits")
    }
    assert "repeat_window_seconds" not in permit_columns_downgraded
    assert "allow_identical_repeats" in permit_columns_downgraded
    engine.dispose()
    os.remove(db_path)


def test_041_scrubs_legacy_plaintext_owner_keys_from_content_tables(
    tmp_path, monkeypatch
):
    """Legacy content rows stored the raw API key in owner_key; 041 blanks them.

    Rows whose owner_key is a known wallet id (what the factory routers write
    now) keep their owner, and an already-blank row stays blank. The rows are
    inserted at head and the scrub is exercised by stepping back to 040 and
    forward again, which also pins that the downgrade is a no-op (the scrubbed
    values were credentials and are unrecoverable) and the upgrade idempotent.
    """
    db_path = tmp_path / "content-owner-key-migration.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    sync_url = f"sqlite:///{db_path}"
    sentinel = "b2a_live_secret_that_must_not_survive"
    wallet_id = "spn-content-owner-mig"

    monkeypatch.setenv("DATABASE_URL", async_url)
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    asyncio.set_event_loop(asyncio.new_event_loop())

    engine = create_engine(sync_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO wallets (
                    wallet_id, wallet_type, owner_key, balance, lifetime_credits,
                    lifetime_debits, daily_spent, auto_refill, status
                ) VALUES (:wallet_id, 'sponsor', '', 0, 0, 0, 0, 0, 'active')
                """
            ),
            {"wallet_id": wallet_id},
        )
        for pipeline_id, owner_key in (
            ("pipe-legacy", sentinel),
            ("pipe-owned", wallet_id),
            ("pipe-admin", ""),
        ):
            connection.execute(
                text(
                    """
                    INSERT INTO content_pipelines (
                        pipeline_id, title, language, auto_schedule, owner_key,
                        status, caption_style, aspect_ratio, created_at
                    ) VALUES (
                        :pipeline_id, 'Legacy pipeline', 'en', 1, :owner_key,
                        'queued', 'bold_impact', '9:16', '2026-10-01 00:00:00'
                    )
                    """
                ),
                {"pipeline_id": pipeline_id, "owner_key": owner_key},
            )
        for campaign_id, owner_key in (
            ("camp-legacy", sentinel),
            ("camp-owned", wallet_id),
        ):
            connection.execute(
                text(
                    """
                    INSERT INTO content_campaigns (
                        campaign_id, campaign_title, source_url, status,
                        owner_key, created_at
                    ) VALUES (
                        :campaign_id, 'Legacy campaign', 'https://example.com/v',
                        'running', :owner_key, '2026-10-01 00:00:00'
                    )
                    """
                ),
                {"campaign_id": campaign_id, "owner_key": owner_key},
            )
    engine.dispose()

    def owners() -> tuple[dict, dict]:
        engine = create_engine(sync_url)
        with engine.connect() as connection:
            pipelines = dict(
                connection.execute(
                    text("SELECT pipeline_id, owner_key FROM content_pipelines")
                ).all()
            )
            campaigns = dict(
                connection.execute(
                    text("SELECT campaign_id, owner_key FROM content_campaigns")
                ).all()
            )
        engine.dispose()
        return pipelines, campaigns

    # Nothing is scrubbed by inserting at head; the scrub runs on upgrade.
    assert owners()[0]["pipe-legacy"] == sentinel

    command.downgrade(config, "040_permit_repeat_window")
    asyncio.set_event_loop(asyncio.new_event_loop())
    command.upgrade(config, "head")
    asyncio.set_event_loop(asyncio.new_event_loop())

    pipelines, campaigns = owners()
    assert pipelines == {"pipe-legacy": "", "pipe-owned": wallet_id, "pipe-admin": ""}
    assert campaigns == {"camp-legacy": "", "camp-owned": wallet_id}
    assert sentinel not in repr((pipelines, campaigns))

    # Idempotent: a second pass changes nothing.
    command.downgrade(config, "040_permit_repeat_window")
    asyncio.set_event_loop(asyncio.new_event_loop())
    command.upgrade(config, "head")
    asyncio.set_event_loop(asyncio.new_event_loop())
    assert owners() == (pipelines, campaigns)
