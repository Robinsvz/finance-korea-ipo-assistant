from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import JobRun, NotificationContentArtifact, NotificationEvent
from app.services import observability
from app.services.observability import (
    finish_notification,
    latest_job_statuses,
    record_notification_content,
    reserve_notification,
    run_tracked_job,
)


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


def test_tracked_job_marks_mixed_success_and_skips_as_partial(db_session, monkeypatch):
    @contextmanager
    def test_session_scope():
        yield db_session
        db_session.commit()

    monkeypatch.setattr(observability, "init_db", lambda: None)
    monkeypatch.setattr(observability, "session_scope", test_session_scope)

    run_tracked_job(
        "mixed-test",
        lambda: {"processed": 2, "success": 1, "skipped": 1},
    )

    row = db_session.execute(select(JobRun).where(JobRun.job_name == "mixed-test")).scalar_one()
    assert row.status == "partial"
    assert row.success_count == 1
    assert row.skipped_count == 1


def test_tracked_email_job_maps_recipient_counts_instead_of_item_or_boolean_counts(
    db_session, monkeypatch
):
    @contextmanager
    def test_session_scope():
        yield db_session
        db_session.commit()

    monkeypatch.setattr(observability, "init_db", lambda: None)
    monkeypatch.setattr(observability, "session_scope", test_session_scope)

    run_tracked_job(
        "email-partial",
        lambda: {
            "processed": 3,
            "sent": 1,
            "failed": 1,
            "duplicate": 1,
            "skipped": False,
            "items": 12,
        },
    )
    run_tracked_job(
        "email-disabled",
        lambda: {
            "processed": 0,
            "sent": 0,
            "failed": 0,
            "duplicate": 0,
            "skipped": True,
            "items": 12,
        },
    )

    partial = db_session.execute(
        select(JobRun).where(JobRun.job_name == "email-partial")
    ).scalar_one()
    assert partial.status == "partial"
    assert partial.processed_count == 3
    assert partial.success_count == 1
    assert partial.failure_count == 1
    assert partial.skipped_count == 1

    disabled = db_session.execute(
        select(JobRun).where(JobRun.job_name == "email-disabled")
    ).scalar_one()
    assert disabled.status == "skipped"
    assert disabled.success_count == 0
    assert disabled.skipped_count == 1


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


def test_secret_masking_redacts_smtp_passwords_and_uris():
    value = "password=plain smtp_password=second smtps://mailer:uri-secret@smtp.example.com/path"

    masked = observability.mask_secrets(value)

    assert "plain" not in masked
    assert "second" not in masked
    assert "uri-secret" not in masked
    assert masked.count("***REDACTED***") == 3


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


def test_stale_email_with_audited_content_requires_manual_delivery_review(db_session, monkeypatch):
    @contextmanager
    def test_session_scope():
        yield db_session
        db_session.commit()

    monkeypatch.setattr(observability, "init_db", lambda: None)
    monkeypatch.setattr(observability, "session_scope", test_session_scope)
    now = datetime(2026, 7, 18, tzinfo=timezone.utc)
    event, acquired = reserve_notification("email", "monthly", "recipient@example.com", now=now)
    assert acquired is True
    record_notification_content(
        event.id,
        subject="subject",
        text_body="text",
        html_body="<p>html</p>",
    )

    same, reacquired = reserve_notification(
        "email",
        "monthly",
        "recipient@example.com",
        now=now + timedelta(minutes=6),
    )

    assert same.id == event.id
    assert reacquired is False
    assert same.attempt_count == 1


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


def test_tracked_job_records_masked_failure(db_session, monkeypatch):
    @contextmanager
    def test_session_scope():
        yield db_session
        db_session.commit()

    monkeypatch.setattr(observability, "init_db", lambda: None)
    monkeypatch.setattr(observability, "session_scope", test_session_scope)

    def fail():
        raise RuntimeError("api_key=never-log-this")

    try:
        run_tracked_job("failed-test", fail)
    except RuntimeError:
        pass

    row = db_session.execute(select(JobRun).where(JobRun.job_name == "failed-test")).scalar_one()
    assert row.status == "failed"
    assert row.failure_count == 1
    assert "never-log-this" not in row.error_summary


def test_notification_validation_finish_and_default_clock(db_session, monkeypatch):
    @contextmanager
    def test_session_scope():
        yield db_session
        db_session.commit()

    monkeypatch.setattr(observability, "init_db", lambda: None)
    monkeypatch.setattr(observability, "session_scope", test_session_scope)

    with pytest.raises(ValueError, match="positive"):
        reserve_notification("email", "invalid", "a@example.com", lease_seconds=0)

    event, acquired = reserve_notification("email", "daily", "a@example.com")
    assert acquired is True
    finish_notification(
        event.id,
        status="sent",
        response={"Authorization": "Bearer secret", "nested": ["token=hidden", 1]},
        error="smtp_password=hidden",
    )
    db_session.expire_all()
    finished = db_session.get(NotificationEvent, event.id)
    assert finished.status == "sent"
    assert finished.sent_at is not None
    assert finished.lease_until is None
    assert finished.response_json["Authorization"] == "***REDACTED***"
    assert finished.response_json["nested"][0] == "token=***REDACTED***"
    assert "hidden" not in finished.error_summary

    finish_notification(event.id, status="failed", response=None, error=None)
    db_session.expire_all()
    failed = db_session.get(NotificationEvent, event.id)
    assert failed.sent_at is None
    assert failed.response_json is None
    assert failed.error_summary is None
    finish_notification(999_999, status="sent")


def test_notification_content_recording_redacts_secrets_and_preserves_context(
    db_session,
    monkeypatch,
):
    @contextmanager
    def test_session_scope():
        yield db_session
        db_session.commit()

    monkeypatch.setattr(observability, "session_scope", test_session_scope)
    event = NotificationEvent(
        channel="email",
        event_key="monthly:2026-07:1",
        recipient="reader@example.com",
        status="pending",
    )
    db_session.add(event)
    db_session.commit()

    record_notification_content(
        event.id,
        subject="7월 IPO",
        text_body="safe password=body-secret",
        html_body="<p>smtp_password=html-secret</p>",
        universe_hash="a" * 64,
        evidence_hash="b" * 64,
        metadata={
            "period": "2026-07",
            "SMTP_PASSWORD": "metadata-secret",
            "part_number": 1,
        },
    )

    artifact = db_session.execute(select(NotificationContentArtifact)).scalar_one()
    assert artifact.notification_event_id == event.id
    assert artifact.content_redacted is True
    assert "body-secret" not in artifact.text_body
    assert "html-secret" not in artifact.html_body
    assert artifact.metadata_json["SMTP_PASSWORD"] == "***REDACTED***"
    assert artifact.metadata_json["part_number"] == 1
    assert artifact.universe_hash == "a" * 64
    assert artifact.evidence_hash == "b" * 64

    with pytest.raises(ValueError, match="mapping"):
        record_notification_content(
            event.id,
            subject="subject",
            text_body="text",
            html_body="html",
            metadata=[],
        )


def test_observability_helper_edge_cases():
    assert observability.sanitize_data(("ok", 1)) == ["ok", 1]
    assert observability.sanitize_data("Bearer abc") == "Bearer ***REDACTED***"
    assert observability.sanitize_data(3) == 3
    assert observability.sanitize_data("x", "client-secret") == "***REDACTED***"

    assert observability._extract_count([], ("processed",)) == 0
    assert (
        observability._extract_count({"processed": True, "items": 4}, ("processed", "items")) == 4
    )
    assert observability._extract_count({"processed": "4"}, ("processed",)) == 0

    assert observability._extract_skipped_count([]) == 0
    assert observability._extract_skipped_count({"duplicate": True, "skipped": "yes"}) == 0
    assert observability._extract_skipped_count({"duplicate": 2, "skipped": 3}) == 5
    assert observability._json_safe_result(date(2026, 7, 19)) == {"result": "2026-07-19"}

    cutoff = datetime(2026, 7, 19, tzinfo=timezone.utc)
    assert not observability._is_stale_running(
        JobRun(job_name="x", run_id="1", status="succeeded"), cutoff
    )
    assert not observability._is_stale_running(
        JobRun(
            job_name="x",
            run_id="2",
            status="running",
            started_at=datetime(2026, 7, 18),
            finished_at=datetime(2026, 7, 18),
        ),
        cutoff,
    )
    assert observability._is_stale_running(
        JobRun(
            job_name="x",
            run_id="3",
            status="running",
            started_at=datetime(2026, 7, 18),
        ),
        cutoff,
    )
    assert not observability._is_stale_running(
        JobRun(
            job_name="x",
            run_id="4",
            status="running",
            started_at=datetime(2026, 7, 19, 1, tzinfo=timezone.utc),
        ),
        cutoff,
    )


def test_latest_status_marks_old_running_job_stale(db_session, monkeypatch):
    @contextmanager
    def test_session_scope():
        yield db_session

    monkeypatch.setattr(observability, "init_db", lambda: None)
    monkeypatch.setattr(observability, "session_scope", test_session_scope)
    db_session.add(
        JobRun(
            run_id="stale",
            job_name="stale-job",
            status="running",
            started_at=datetime.now(timezone.utc) - timedelta(hours=7),
        )
    )
    db_session.flush()

    assert latest_job_statuses(limit=1)[0]["status"] == "stale"
