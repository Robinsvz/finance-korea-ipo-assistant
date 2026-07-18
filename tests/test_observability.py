from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from app.models import JobRun, NotificationEvent
from app.services import observability
from app.services.observability import latest_job_statuses, reserve_notification, run_tracked_job


def test_tracked_job_records_partial_and_json_safe_result(db_session, monkeypatch):
    @contextmanager
    def test_session_scope():
        yield db_session
        db_session.commit()

    monkeypatch.setattr(observability, "init_db", lambda: None)
    monkeypatch.setattr(observability, "session_scope", test_session_scope)
    result = run_tracked_job(
        "partial-test",
        lambda: {
            "processed": 3,
            "success": 1,
            "partial": 1,
            "skipped": 0,
            "failed": 1,
            "on": date(2026, 7, 18),
        },
    )
    assert result["failed"] == 1
    row = db_session.execute(select(JobRun).where(JobRun.job_name == "partial-test")).scalar_one()
    assert row.status == "partial"
    assert row.partial_count == 1
    assert row.failure_count == 1
    assert row.result_json["on"] == "2026-07-18"


def test_tracked_job_recursively_redacts_secrets(db_session, monkeypatch):
    @contextmanager
    def test_session_scope():
        yield db_session
        db_session.commit()

    monkeypatch.setattr(observability, "init_db", lambda: None)
    monkeypatch.setattr(observability, "session_scope", test_session_scope)
    run_tracked_job(
        "redaction-test",
        lambda: {
            "nested": {"authorization": "Bearer top-secret", "safe": "ok"},
            "url": "https://test.invalid?api_key=secret",
        },
    )
    row = db_session.execute(select(JobRun).where(JobRun.job_name == "redaction-test")).scalar_one()
    serialized = str(row.result_json)
    assert "top-secret" not in serialized
    assert "api_key=secret" not in serialized
    assert row.result_json["nested"]["safe"] == "ok"


def test_stale_pending_notification_can_be_reacquired(db_session, monkeypatch):
    @contextmanager
    def test_session_scope():
        yield db_session
        db_session.commit()

    monkeypatch.setattr(observability, "init_db", lambda: None)
    monkeypatch.setattr(observability, "session_scope", test_session_scope)
    now = datetime(2026, 7, 18, tzinfo=timezone.utc)
    first, acquired = reserve_notification("telegram", "daily", "123", now=now)
    assert acquired is True
    second, acquired = reserve_notification(
        "telegram", "daily", "123", now=now + timedelta(seconds=10)
    )
    assert acquired is False
    third, acquired = reserve_notification(
        "telegram", "daily", "123", now=now + timedelta(minutes=6)
    )
    assert acquired is True
    assert first.id == second.id == third.id
    assert db_session.query(NotificationEvent).count() == 1
    assert third.attempt_count == 2


def test_latest_statuses_group_before_applying_limit(db_session, monkeypatch):
    @contextmanager
    def test_session_scope():
        yield db_session

    monkeypatch.setattr(observability, "init_db", lambda: None)
    monkeypatch.setattr(observability, "session_scope", test_session_scope)
    now = datetime.now(timezone.utc)
    for index in range(25):
        db_session.add(
            JobRun(
                run_id=f"dominant-{index}",
                job_name="dominant",
                status="succeeded",
                started_at=now - timedelta(minutes=index),
            )
        )
    db_session.add(
        JobRun(
            run_id="older-other",
            job_name="other",
            status="succeeded",
            started_at=now - timedelta(days=1),
        )
    )
    db_session.flush()

    assert {row["job_name"] for row in latest_job_statuses()} == {"dominant", "other"}
