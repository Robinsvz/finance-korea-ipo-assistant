from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.config import DEFAULT_EMAIL_RECIPIENT
from app.services import jobs
from app.services.email import EmailContent


def _content(part: int = 1) -> EmailContent:
    return EmailContent(
        subject=f"part {part}",
        text=f"text {part}",
        html=f"<p>part {part}</p>",
    )


def _bundle(*, core: int = 2, appendix: int = 1, parts: int = 2):
    read_model = SimpleNamespace(
        month_start=date(2026, 7, 1),
        core=tuple(object() for _ in range(core)),
        filing_appendix=tuple(object() for _ in range(appendix)),
        reconciliation_state="cross_checked",
    )
    return SimpleNamespace(
        read_model=read_model,
        parts=tuple(_content(index) for index in range(1, parts + 1)),
        universe_hash="a" * 64,
        evidence_hash="b" * 64,
    )


def test_public_dart_month_job_respects_gate_and_calendar_bounds(monkeypatch):
    monkeypatch.setattr(
        jobs,
        "get_settings",
        lambda: SimpleNamespace(dart_public_fallback_enabled=False),
    )
    assert jobs.collect_public_dart_for_current_month()["skipped"] is True

    captured = {}
    monkeypatch.setattr(
        jobs,
        "get_settings",
        lambda: SimpleNamespace(dart_public_fallback_enabled=True),
    )
    monkeypatch.setattr(
        "app.services.official_evidence.collect_public_dart_evidence_for_month",
        lambda start, end: captured.update(start=start, end=end) or {"success": 4},
    )

    assert jobs.collect_public_dart_for_current_month(date(2026, 2, 8)) == {"success": 4}
    assert captured == {"start": date(2026, 2, 1), "end": date(2026, 2, 28)}


def test_build_monthly_email_content_uses_calendar_month_and_v3_limits(monkeypatch):
    generated = datetime(2026, 7, 19, 8, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    universe = SimpleNamespace(core_count=4)
    expected = object()
    captured = {}

    class Scope:
        def __enter__(self):
            return "session"

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(jobs, "init_db", lambda: None)
    monkeypatch.setattr(jobs, "local_now", lambda: generated)
    monkeypatch.setattr(jobs, "session_scope", lambda: Scope())
    monkeypatch.setattr(
        jobs,
        "get_settings",
        lambda: SimpleNamespace(email_max_html_bytes=12_345),
    )
    monkeypatch.setattr(
        jobs,
        "list_monthly_universe",
        lambda session, start, end: (
            captured.update(session=session, start=start, end=end) or universe
        ),
    )

    def build(session, value, **kwargs):
        captured.update(build_session=session, universe=value, **kwargs)
        return expected

    monkeypatch.setattr(jobs, "build_monthly_ipo_email", build)

    assert jobs.build_monthly_email_content(date(2026, 7, 31)) is expected
    assert captured["start"] == date(2026, 7, 1)
    assert captured["end"] == date(2026, 7, 31)
    assert captured["month_start"] == captured["start"]
    assert captured["month_end"] == captured["end"]
    assert captured["generated_at"] == generated
    assert captured["title"] == "[Korea IPO] 2026년 7월"
    assert captured["max_html_bytes"] == 12_345


def test_monthly_sender_aggregates_parts_and_uses_fixed_recipient(monkeypatch):
    monkeypatch.setattr(jobs, "build_monthly_email_content", lambda _target: _bundle())
    calls = []

    def send(content, *, event_key, recipients, audit_context):
        calls.append((content.subject, event_key, recipients, audit_context))
        if content.subject == "part 1":
            return {"processed": 1, "sent": 1, "failures": [], "skipped": False}
        return {
            "processed": 1,
            "sent": 0,
            "failed": 1,
            "delivery_state_unknown": 1,
            "failures": [{"recipient": DEFAULT_EMAIL_RECIPIENT}],
            "skipped": False,
        }

    monkeypatch.setattr(jobs, "send_email_message", send)

    result = jobs.send_monthly_email_snapshot(date(2026, 7, 5), trigger="canary")

    assert result["processed"] == 2
    assert result["sent"] == 1
    assert result["failed"] == result["delivery_state_unknown"] == 1
    assert result["items"] == 3
    assert result["core_items"] == 2
    assert result["filing_appendix_items"] == 1
    assert result["parts"] == 2
    assert result["recipient"] == DEFAULT_EMAIL_RECIPIENT
    assert all(call[2] == (DEFAULT_EMAIL_RECIPIENT,) for call in calls)
    assert calls[0][1].endswith("2026-07:aaaaaaaaaaaa:bbbbbbbbbbbb:1")
    assert ":canary:" in calls[0][1]
    assert calls[0][3].universe_hash == "a" * 64
    assert calls[0][3].evidence_hash == "b" * 64
    assert calls[0][3].metadata == {
        "product": jobs.MONTHLY_EMAIL_EVENT_VERSION,
        "period": "2026-07",
        "trigger": "canary",
        "part_number": 1,
        "part_count": 2,
    }


def test_monthly_sender_handles_empty_skip_and_transport_skip(monkeypatch):
    monkeypatch.setattr(
        jobs,
        "build_monthly_email_content",
        lambda _target: _bundle(core=0, appendix=0, parts=1),
    )
    monkeypatch.setattr(
        jobs,
        "send_email_message",
        lambda *_args, **_kwargs: pytest.fail("empty roster must not send"),
    )
    result = jobs.send_monthly_email_snapshot(trigger="manual")
    assert result["skipped"] is True
    assert result["reason"] == jobs.NO_EMAIL_ITEMS_REASON

    monkeypatch.setattr(
        jobs,
        "build_monthly_email_content",
        lambda _target: _bundle(core=1, appendix=0, parts=1),
    )
    monkeypatch.setattr(
        jobs,
        "send_email_message",
        lambda *_args, **_kwargs: {
            "processed": 0,
            "sent": 0,
            "skipped": True,
            "reason": "Email is not configured",
        },
    )
    result = jobs.send_monthly_email_snapshot(trigger="manual")
    assert result["skipped"] is True
    assert result["reason"] == "Email is not configured"

    unreconciled = _bundle(core=1, appendix=0, parts=1)
    unreconciled.read_model.reconciliation_state = "reported"
    monkeypatch.setattr(jobs, "build_monthly_email_content", lambda _target: unreconciled)
    monkeypatch.setattr(
        jobs,
        "send_email_message",
        lambda *_args, **_kwargs: pytest.fail("unreconciled roster must not send"),
    )
    result = jobs.send_monthly_email_snapshot(trigger="manual")
    assert result["skipped"] is True
    assert result["reason"] == jobs.MONTHLY_RECONCILIATION_REQUIRED_REASON


def test_v3_routing_weekly_key_validation_and_month_bounds(monkeypatch):
    calls = []
    real_sender = jobs.send_monthly_email_snapshot
    monkeypatch.setattr(
        jobs,
        "get_settings",
        lambda: SimpleNamespace(email_product_mode="v3-facts"),
    )
    monkeypatch.setattr(
        jobs,
        "send_subscription_d_minus_one_emails",
        lambda: calls.append("d1") or {"sent": 1},
    )
    assert jobs.send_daily_email_summary() == {"sent": 1}
    weekly = jobs.send_weekly_email_summary()
    assert weekly["skipped"] is True
    assert weekly["reason"] == jobs.WEEKLY_V3_SKIP_REASON
    assert calls == ["d1"]

    with pytest.raises(ValueError, match="trigger"):
        real_sender(trigger="bad")
    assert jobs._calendar_month_bounds(date(2024, 2, 15)) == (
        date(2024, 2, 1),
        date(2024, 2, 29),
    )


def test_weekly_monthly_event_key_is_stable_for_identical_evidence(monkeypatch):
    monkeypatch.setattr(
        jobs,
        "build_monthly_email_content",
        lambda _target: _bundle(core=1, appendix=0, parts=1),
    )
    calls = []
    monkeypatch.setattr(
        jobs,
        "send_email_message",
        lambda _content, **kwargs: calls.append(kwargs) or {"processed": 1, "sent": 1},
    )
    result = jobs.send_monthly_email_snapshot(trigger="weekly")
    assert result["sent"] == 1
    assert ":weekly:" in calls[0]["event_key"]
    assert "weekly-2026" not in calls[0]["event_key"]
