from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.services import jobs
from app.services.email import EmailContent


@pytest.fixture(autouse=True)
def _legacy_email_mode(monkeypatch):
    monkeypatch.setattr(
        jobs,
        "get_settings",
        lambda: SimpleNamespace(email_product_mode="v2", default_user_cash=10_000_000),
    )


def _content():
    return EmailContent(subject="subject", text="text", html="<p>html</p>")


def test_daily_and_weekly_email_senders_use_stable_period_keys(monkeypatch):
    monkeypatch.setattr(jobs, "local_today", lambda: date(2026, 7, 21))
    monkeypatch.setattr(jobs, "build_daily_email_content", lambda _target: (_content(), 2, 1))
    monkeypatch.setattr(jobs, "build_weekly_email_content", lambda _start: (_content(), 3, 2))
    calls = []

    def send(_content_value, *, event_key):
        calls.append(event_key)
        return {"sent": 1, "failed": 0}

    monkeypatch.setattr(jobs, "send_email_message", send)

    daily = jobs.send_daily_email_summary()
    weekly = jobs.send_weekly_email_summary()

    assert (daily["decision_items"], daily["schedule_items"], daily["items"]) == (2, 1, 3)
    assert (weekly["decision_items"], weekly["schedule_items"], weekly["items"]) == (3, 2, 5)
    assert calls == [
        "daily-decision-email:v2:2026-07-21",
        "weekly-decision-email:v2:2026-07-21",
    ]


def test_schedule_only_email_is_sent(monkeypatch):
    monkeypatch.setattr(jobs, "local_today", lambda: date(2026, 7, 21))
    monkeypatch.setattr(jobs, "build_daily_email_content", lambda _target: (_content(), 0, 2))
    calls = []
    monkeypatch.setattr(
        jobs,
        "send_email_message",
        lambda _content_value, *, event_key: calls.append(event_key) or {"sent": 1},
    )

    result = jobs.send_daily_email_summary()

    assert result["sent"] == 1
    assert result["decision_items"] == 0
    assert result["schedule_items"] == result["items"] == 2
    assert calls == ["daily-decision-email:v2:2026-07-21"]


@pytest.mark.parametrize(
    ("sender", "builder_name"),
    [
        (jobs.send_daily_email_summary, "build_daily_email_content"),
        (jobs.send_weekly_email_summary, "build_weekly_email_content"),
    ],
)
def test_completely_empty_email_skips_smtp(monkeypatch, sender, builder_name):
    monkeypatch.setattr(jobs, "local_today", lambda: date(2026, 7, 21))
    monkeypatch.setattr(jobs, builder_name, lambda _target: (_content(), 0, 0))
    monkeypatch.setattr(
        jobs,
        "send_email_message",
        lambda *_args, **_kwargs: pytest.fail("empty email must not open SMTP"),
    )

    result = sender()

    assert result["skipped"] is True
    assert result["reason"] == jobs.NO_EMAIL_ITEMS_REASON
    assert result["processed"] == result["sent"] == result["items"] == 0
    assert result["decision_items"] == result["schedule_items"] == 0


def test_monday_daily_email_is_replaced_by_weekly_without_building_or_sending(monkeypatch):
    monkeypatch.setattr(jobs, "local_today", lambda: date(2026, 7, 20))
    monkeypatch.setattr(
        jobs,
        "build_daily_email_content",
        lambda *_args: pytest.fail("Monday daily email must not be built"),
    )
    monkeypatch.setattr(
        jobs,
        "send_email_message",
        lambda *_args, **_kwargs: pytest.fail("Monday daily email must not open SMTP"),
    )

    result = jobs.send_daily_email_summary()

    assert result["skipped"] is True
    assert result["reason"] == jobs.MONDAY_DAILY_SKIP_REASON
    assert result["items"] == 0


def test_period_email_content_uses_configured_cash_and_dates(monkeypatch):
    monkeypatch.setattr(jobs, "init_db", lambda: None)
    monkeypatch.setattr(
        jobs,
        "get_settings",
        lambda: SimpleNamespace(default_user_cash=12_000_000),
    )
    generated = datetime(2026, 7, 20, 8, 32, tzinfo=ZoneInfo("Asia/Seoul"))
    monkeypatch.setattr(jobs, "local_now", lambda: generated)
    ipo = SimpleNamespace(
        is_sample=False,
        status="scheduled",
        subscription_start_date=date(2026, 7, 21),
        subscription_end_date=date(2026, 7, 21),
    )
    monkeypatch.setattr(jobs, "list_ipos_between", lambda _session, _start, _end: [ipo])
    captured = []

    class Scope:
        def __enter__(self):
            return "session"

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(jobs, "session_scope", lambda: Scope())
    schedule_item = object()
    monkeypatch.setattr(
        jobs,
        "build_schedule_email_items",
        lambda _ipos, _start, _end: (schedule_item,),
    )

    def build(_session, ipos, **kwargs):
        captured.append({**kwargs, "ipos": ipos})
        return _content()

    monkeypatch.setattr(jobs, "build_period_decision_email", build)

    daily, daily_decisions, daily_schedule = jobs.build_daily_email_content(date(2026, 7, 20))
    weekly, weekly_decisions, weekly_schedule = jobs.build_weekly_email_content(date(2026, 7, 20))

    assert daily.subject == weekly.subject == "subject"
    assert daily_decisions == weekly_decisions == 1
    assert daily_schedule == weekly_schedule == 1
    assert captured[0]["title"] == "[Korea IPO] 7월 20일 일간 브리핑"
    assert captured[0]["period_label"] == "2026-07-20"
    assert captured[1]["title"] == "[Korea IPO] 7월 20일–7월 26일 주간 브리핑"
    assert captured[1]["period_label"] == "2026-07-20 ~ 2026-07-26"
    assert all(call["user_cash"] == 12_000_000 for call in captured)
    assert all(call["generated_at"] == generated for call in captured)
    assert all(call["schedule_items"] == (schedule_item,) for call in captured)


def test_decision_email_excludes_samples_and_already_ended_subscriptions(monkeypatch):
    monkeypatch.setattr(jobs, "init_db", lambda: None)
    monkeypatch.setattr(
        jobs,
        "get_settings",
        lambda: SimpleNamespace(default_user_cash=10_000_000),
    )
    generated = datetime(2026, 7, 20, 8, 32, tzinfo=ZoneInfo("Asia/Seoul"))
    monkeypatch.setattr(jobs, "local_now", lambda: generated)
    rows = [
        SimpleNamespace(
            name="sample",
            is_sample=True,
            status="scheduled",
            subscription_start_date=date(2026, 7, 21),
            subscription_end_date=date(2026, 7, 21),
        ),
        SimpleNamespace(
            name="ended-date",
            is_sample=False,
            status="scheduled",
            subscription_start_date=date(2026, 7, 21),
            subscription_end_date=date(2026, 7, 19),
        ),
        SimpleNamespace(
            name="ended-status",
            is_sample=False,
            status="subscription_closed",
            subscription_start_date=date(2026, 7, 18),
            subscription_end_date=None,
        ),
        SimpleNamespace(
            name="active-through-today",
            is_sample=False,
            status="subscription_open",
            subscription_start_date=date(2026, 7, 19),
            subscription_end_date=date(2026, 7, 20),
        ),
        SimpleNamespace(
            name="starts-today",
            is_sample=False,
            status="scheduled",
            subscription_start_date=date(2026, 7, 20),
            subscription_end_date=date(2026, 7, 21),
        ),
        SimpleNamespace(
            name="unknown-end",
            is_sample=False,
            status="scheduled",
            subscription_start_date=None,
            subscription_end_date=None,
        ),
        SimpleNamespace(
            name="withdrawn",
            is_sample=False,
            status="withdrawn",
            subscription_start_date=date(2026, 7, 21),
            subscription_end_date=date(2026, 7, 22),
        ),
    ]
    monkeypatch.setattr(jobs, "list_ipos_between", lambda *_args: rows)
    captured = {}

    class Scope:
        def __enter__(self):
            return "session"

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(jobs, "session_scope", lambda: Scope())
    schedule_names = []

    def schedules(ipos, _start, _end):
        schedule_names.extend(ipo.name for ipo in ipos)
        return tuple(object() for _ipo in ipos)

    monkeypatch.setattr(jobs, "build_schedule_email_items", schedules)

    def build(_session, ipos, **_kwargs):
        captured["names"] = [ipo.name for ipo in ipos]
        return _content()

    monkeypatch.setattr(jobs, "build_period_decision_email", build)

    _content_value, decision_count, schedule_count = jobs.build_weekly_email_content(
        date(2026, 7, 20)
    )

    assert decision_count == 2
    assert schedule_count == 5
    assert captured["names"] == ["starts-today", "unknown-end"]
    assert schedule_names == [
        "ended-date",
        "ended-status",
        "active-through-today",
        "starts-today",
        "unknown-end",
    ]


def test_scoring_stops_at_subscription_open_cutoff(monkeypatch):
    ipo = SimpleNamespace(subscription_start_date=date(2026, 7, 20))
    no_schedule = SimpleNamespace(subscription_start_date=None)

    monkeypatch.setattr(
        jobs,
        "local_now",
        lambda: datetime(2026, 7, 20, 8, 59, tzinfo=ZoneInfo("Asia/Seoul")),
    )
    assert jobs._before_subscription_cutoff(no_schedule) is True
    assert jobs._before_subscription_cutoff(ipo) is True

    monkeypatch.setattr(
        jobs,
        "local_now",
        lambda: datetime(2026, 7, 20, 9, 0, tzinfo=ZoneInfo("Asia/Seoul")),
    )
    assert jobs._before_subscription_cutoff(ipo) is False


def test_monthly_backtest_uses_operational_bundle_and_delivery_response(monkeypatch):
    monkeypatch.setattr(jobs, "init_db", lambda: None)
    monkeypatch.setattr(
        jobs,
        "get_settings",
        lambda: SimpleNamespace(telegram_configured=True),
    )
    monkeypatch.setattr(jobs, "local_today", lambda: date(2026, 7, 20))
    bundle = SimpleNamespace(
        readiness=SimpleNamespace(validation_ready=False, complete_pair_count=0),
        gross_outcomes=SimpleNamespace(ipo_count=26),
    )

    class Scope:
        def __enter__(self):
            return "session"

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(jobs, "session_scope", lambda: Scope())
    monkeypatch.setattr(
        jobs,
        "build_operational_backtest",
        lambda session: bundle if session == "session" else None,
    )
    monkeypatch.setattr(
        jobs,
        "format_operational_backtest",
        lambda value: "honest report" if value is bundle else "unexpected",
    )
    calls = []

    def send(text, *, event_key):
        calls.append((text, event_key))
        return {"ok": False}

    monkeypatch.setattr(jobs, "send_telegram_message", send)

    result = jobs.send_monthly_backtest()

    assert result == {
        "sent": False,
        "validation_ready": False,
        "eligible_pairs": 0,
        "gross_outcomes": 26,
    }
    assert calls == [("honest report", "monthly-backtest:2026-07")]
