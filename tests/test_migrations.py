from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command


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
            == "20260718_0010"
        )
    score_columns = {column["name"] for column in inspect(engine).get_columns("score_snapshots")}
    assert "input_fingerprint" in score_columns
    job_columns = {column["name"] for column in inspect(engine).get_columns("job_runs")}
    assert {"partial_count", "skipped_count"}.issubset(job_columns)
    engine.dispose()
