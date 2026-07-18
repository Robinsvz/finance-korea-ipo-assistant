from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy import desc, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.db import init_db, session_scope
from app.models import JobRun, NotificationEvent

SECRET_PATTERNS = (
    re.compile(r"(?i)(crtfc_key|api[_-]?key|token|client_secret)=([^&\s]+)"),
    re.compile(r"/bot[^/\s]+/"),
    re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]+"),
)
SECRET_KEYS = {"authorization", "password", "passwd", "token", "api_key", "client_secret"}


def run_tracked_job(job_name: str, fn: Callable[[], Any]) -> Any:
    init_db()
    run_id = uuid.uuid4().hex
    with session_scope() as session:
        session.add(JobRun(run_id=run_id, job_name=job_name, status="running"))
    try:
        result = fn()
    except Exception as exc:
        with session_scope() as session:
            row = session.execute(select(JobRun).where(JobRun.run_id == run_id)).scalar_one()
            row.status = "failed"
            row.finished_at = datetime.now(timezone.utc)
            row.failure_count = 1
            row.error_summary = mask_secrets(f"{type(exc).__name__}: {exc}")[:2000]
        raise
    with session_scope() as session:
        row = session.execute(select(JobRun).where(JobRun.run_id == run_id)).scalar_one()
        row.finished_at = datetime.now(timezone.utc)
        row.result_json = _json_safe_result(result)
        row.processed_count = _extract_count(
            result, ("processed", "records", "checked", "scored", "items")
        )
        row.success_count = _extract_count(
            result, ("success", "updated", "scored", "records", "items")
        )
        row.partial_count = _extract_count(result, ("partial",))
        row.skipped_count = _extract_count(result, ("skipped",))
        row.failure_count = _extract_count(result, ("failed", "failures"))
        if row.failure_count or row.partial_count:
            row.status = "partial"
        elif row.skipped_count and not row.success_count:
            row.status = "skipped"
        else:
            row.status = "succeeded"
    return result


def latest_job_statuses(limit: int = 20) -> list[dict[str, Any]]:
    init_db()
    with session_scope() as session:
        ranked = select(
            JobRun.id.label("job_run_id"),
            func.row_number()
            .over(
                partition_by=JobRun.job_name,
                order_by=(JobRun.started_at.desc(), JobRun.id.desc()),
            )
            .label("row_number"),
        ).subquery()
        rows = list(
            session.execute(
                select(JobRun)
                .join(ranked, ranked.c.job_run_id == JobRun.id)
                .where(ranked.c.row_number == 1)
                .order_by(desc(JobRun.started_at), desc(JobRun.id))
                .limit(limit)
            ).scalars()
        )
        stale_cutoff = datetime.now(timezone.utc) - timedelta(hours=6)
        return [
            {
                "job_name": row.job_name,
                "status": "stale" if _is_stale_running(row, stale_cutoff) else row.status,
                "started_at": row.started_at,
                "finished_at": row.finished_at,
                "processed_count": row.processed_count,
                "success_count": row.success_count,
                "partial_count": row.partial_count,
                "skipped_count": row.skipped_count,
                "failure_count": row.failure_count,
                "error_summary": row.error_summary,
            }
            for row in rows
        ]


def reserve_notification(
    channel: str,
    event_key: str,
    recipient: str,
    *,
    lease_seconds: int = 300,
    now: Optional[datetime] = None,
) -> tuple[NotificationEvent, bool]:
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    init_db()
    now = now or datetime.now(timezone.utc)
    lease_until = now + timedelta(seconds=lease_seconds)
    with session_scope() as session:
        event = NotificationEvent(
            channel=channel,
            event_key=event_key,
            recipient=recipient,
            status="pending",
            lease_until=lease_until,
            attempt_count=1,
        )
        try:
            with session.begin_nested():
                session.add(event)
                session.flush()
            return event, True
        except IntegrityError:
            pass

        predicate = (
            NotificationEvent.channel == channel,
            NotificationEvent.event_key == event_key,
            NotificationEvent.recipient == recipient,
        )
        acquired = session.execute(
            update(NotificationEvent)
            .where(
                *predicate,
                NotificationEvent.status != "sent",
                or_(
                    NotificationEvent.status == "failed",
                    NotificationEvent.lease_until.is_(None),
                    NotificationEvent.lease_until <= now,
                ),
            )
            .values(
                status="pending",
                lease_until=lease_until,
                attempt_count=NotificationEvent.attempt_count + 1,
                updated_at=now,
                error_summary=None,
            )
        ).rowcount
        existing = session.execute(select(NotificationEvent).where(*predicate)).scalar_one()
        return existing, bool(acquired)


def finish_notification(
    event_id: int,
    *,
    status: str,
    response: Optional[dict[str, Any]] = None,
    error: Optional[str] = None,
) -> None:
    with session_scope() as session:
        event = session.get(NotificationEvent, event_id)
        if event is None:
            return
        event.status = status
        event.sent_at = datetime.now(timezone.utc) if status == "sent" else None
        event.lease_until = None
        event.response_json = sanitize_data(response)
        event.error_summary = mask_secrets(error or "")[:2000] or None


def mask_secrets(value: str) -> str:
    masked = value
    for pattern in SECRET_PATTERNS:
        if pattern.pattern.startswith("/bot"):
            masked = pattern.sub("/bot***REDACTED***/", masked)
        elif pattern.pattern.startswith("(?i)bearer"):
            masked = pattern.sub("Bearer ***REDACTED***", masked)
        else:
            masked = pattern.sub(lambda match: f"{match.group(1)}=***REDACTED***", masked)
    return masked


def sanitize_data(value: Any, key: Optional[str] = None) -> Any:
    normalized_key = (key or "").lower().replace("-", "_")
    if normalized_key in SECRET_KEYS or any(
        marker in normalized_key for marker in ("password", "secret", "token", "api_key")
    ):
        return "***REDACTED***"
    if isinstance(value, dict):
        return {
            str(item_key): sanitize_data(item, str(item_key)) for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_data(item) for item in value]
    if isinstance(value, str):
        return mask_secrets(value)
    return value


def _extract_count(result: Any, keys: tuple[str, ...]) -> int:
    if not isinstance(result, dict):
        return 0
    for key in keys:
        value = result.get(key)
        if isinstance(value, int):
            return value
    return 0


def _json_safe_result(result: Any) -> dict[str, Any]:
    payload = result if isinstance(result, dict) else {"result": result}
    json_safe = json.loads(json.dumps(payload, default=str, ensure_ascii=False))
    return sanitize_data(json_safe)


def _is_stale_running(row: JobRun, cutoff: datetime) -> bool:
    if row.status != "running" or row.finished_at is not None:
        return False
    started_at = row.started_at
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    return started_at < cutoff
