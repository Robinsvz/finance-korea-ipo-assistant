from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command
from app.services import scheduler

LEDGER_TABLES = {"user_participations", "participation_exits"}
PROVENANCE_TABLES = {
    "source_documents",
    "ipo_fact_observations",
    "ipo_schedule_revisions",
    "monthly_universe_snapshots",
    "broker_term_versions",
    "data_quality_issues",
}
NOTIFICATION_AUDIT_TABLE = "notification_content_artifacts"


def test_alembic_builds_fresh_schema(tmp_path, monkeypatch):
    database = tmp_path / "fresh.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database}")
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{database}")
    tables = set(inspect(engine).get_table_names())
    assert {"job_runs", "notification_events", "diligence_reviews"}.issubset(tables)
    assert "market_condition_snapshots" in tables
    with engine.connect() as connection:
        assert (
            connection.execute(text("select version_num from alembic_version")).scalar_one()
            == "20260719_0014"
        )
    score_columns = {column["name"] for column in inspect(engine).get_columns("score_snapshots")}
    assert "input_fingerprint" in score_columns
    job_columns = {column["name"] for column in inspect(engine).get_columns("job_runs")}
    assert {"partial_count", "skipped_count"}.issubset(job_columns)
    ipo_columns = {column["name"] for column in inspect(engine).get_columns("ipos")}
    assert {"is_sample", "data_origin", "offering_id"}.issubset(ipo_columns)
    assert LEDGER_TABLES.issubset(tables)
    assert PROVENANCE_TABLES.issubset(tables)
    assert NOTIFICATION_AUDIT_TABLE in tables
    participation_columns = {
        column["name"] for column in inspect(engine).get_columns("user_participations")
    }
    assert "offer_price_snapshot" in participation_columns
    assert scheduler.logger.disabled is False
    engine.dispose()


def test_participation_ledger_revision_round_trip_is_deterministic(tmp_path, monkeypatch):
    database = tmp_path / "ledger-round-trip.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database}")
    config = Config("alembic.ini")

    command.upgrade(config, "20260719_0011")
    engine = create_engine(f"sqlite:///{database}")
    before_ledger = _schema_signature(engine)
    assert LEDGER_TABLES.isdisjoint(before_ledger)
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{database}")
    at_head = _schema_signature(engine)
    assert LEDGER_TABLES.issubset(at_head)
    engine.dispose()

    command.downgrade(config, "20260719_0011")
    engine = create_engine(f"sqlite:///{database}")
    assert _schema_signature(engine) == before_ledger
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{database}")
    assert _schema_signature(engine) == at_head
    engine.dispose()


def test_universe_provenance_revision_backfills_and_round_trips(tmp_path, monkeypatch):
    database = tmp_path / "universe-provenance-round-trip.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database}")
    config = Config("alembic.ini")
    command.upgrade(config, "20260719_0012")
    engine = create_engine(f"sqlite:///{database}")
    before = _schema_signature(engine)
    assert PROVENANCE_TABLES.isdisjoint(before)
    assert "offering_id" in {column["name"] for column in inspect(engine).get_columns("ipos")}
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO ipos "
                "(identity_key, offering_id, corp_name, normalized_name, data_origin, "
                "is_sample, status, "
                "status_changed_at, schedule_version, needs_review, source_confidence, "
                "created_at, updated_at) VALUES "
                "('legacy:test', 'offering:temp:test', '테스트', '테스트', 'kind', 0, "
                "'scheduled', "
                "CURRENT_TIMESTAMP, 1, 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{database}")
    at_head = _schema_signature(engine)
    assert PROVENANCE_TABLES.issubset(at_head)
    with engine.connect() as connection:
        assert (
            connection.execute(text("select offering_id from ipos"))
            .scalar_one()
            .startswith("offering:legacy:")
        )
    engine.dispose()

    command.downgrade(config, "20260719_0012")
    engine = create_engine(f"sqlite:///{database}")
    after = _schema_signature(engine)
    assert PROVENANCE_TABLES.isdisjoint(after)
    assert "offering_id" in {column["name"] for column in inspect(engine).get_columns("ipos")}
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{database}")
    assert _schema_signature(engine) == at_head
    engine.dispose()


def test_notification_content_audit_revision_round_trips(tmp_path, monkeypatch):
    database = tmp_path / "notification-audit-round-trip.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database}")
    config = Config("alembic.ini")

    command.upgrade(config, "20260719_0013")
    engine = create_engine(f"sqlite:///{database}")
    before = _schema_signature(engine)
    assert NOTIFICATION_AUDIT_TABLE not in before
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{database}")
    at_head = _schema_signature(engine)
    assert NOTIFICATION_AUDIT_TABLE in at_head
    columns = {column[0] for column in at_head[NOTIFICATION_AUDIT_TABLE][0]}
    assert {
        "notification_event_id",
        "subject",
        "text_body",
        "html_body",
        "content_sha256",
        "universe_hash",
        "evidence_hash",
        "metadata_json",
    }.issubset(columns)
    engine.dispose()

    command.downgrade(config, "20260719_0013")
    engine = create_engine(f"sqlite:///{database}")
    assert _schema_signature(engine) == before
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{database}")
    assert _schema_signature(engine) == at_head
    engine.dispose()


def _schema_signature(engine) -> dict[str, tuple]:
    inspector = inspect(engine)
    return {
        table_name: (
            tuple(
                (column["name"], str(column["type"]), column["nullable"], column["default"])
                for column in inspector.get_columns(table_name)
            ),
            tuple(
                sorted(
                    (index["name"], tuple(index["column_names"]), index["unique"])
                    for index in inspector.get_indexes(table_name)
                )
            ),
        )
        for table_name in inspector.get_table_names()
    }
