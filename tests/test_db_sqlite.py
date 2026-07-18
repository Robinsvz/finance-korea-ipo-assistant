from sqlalchemy import text

from app.db import engine


def test_sqlite_runtime_pragmas_are_concurrency_safe():
    if engine.dialect.name != "sqlite":
        return
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
        assert connection.execute(text("PRAGMA busy_timeout")).scalar_one() == 30_000
        assert connection.execute(text("PRAGMA journal_mode")).scalar_one().lower() == "wal"
