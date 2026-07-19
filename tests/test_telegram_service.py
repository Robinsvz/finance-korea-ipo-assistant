from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services.observability import mask_secrets
from app.services.scheduler import job_definitions
from app.services.telegram import (
    _parse_amount,
    _run_refresh_operations,
    acquire_refresh_cooldown,
    help_text,
    is_chat_authorized,
    split_telegram_message,
)


def test_message_split_preserves_content():
    text = "line1\n" + "x" * 20 + "\nline3"
    chunks = split_telegram_message(text, limit=10)
    assert all(len(chunk) <= 10 for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_short_message_is_unchanged():
    assert split_telegram_message("hello") == ["hello"]


def test_invalid_chunk_limit_is_rejected():
    with pytest.raises(ValueError):
        split_telegram_message("hello", limit=0)


def test_chat_allowlist():
    assert is_chat_authorized(123, ("123", "456"))
    assert not is_chat_authorized(999, ("123", "456"))
    assert not is_chat_authorized(None, ("123",))
    assert is_chat_authorized(-100123456, ("-100123456",))
    assert not is_chat_authorized(-100999999, ("-100123456",))


@pytest.mark.parametrize(
    ("text", "expected"),
    [("100만원", 1_000_000), ("1.5억", 150_000_000), ("10,000", 10_000)],
)
def test_amount_parser(text, expected):
    assert _parse_amount(text) == expected


@pytest.mark.parametrize("text", ["abc", "0", "-10", "10000000000001"])
def test_amount_parser_rejects_invalid_values(text):
    with pytest.raises(ValueError):
        _parse_amount(text)


def test_secret_masking():
    value = "https://api.test?api_key=secret /bot123456:ABC/send token=xyz Bearer abc.def"
    masked = mask_secrets(value)
    assert "secret" not in masked
    assert "123456:ABC" not in masked
    assert "xyz" not in masked
    assert "abc.def" not in masked


def test_scheduler_contains_full_pipeline():
    names = [name for name, _fn, _trigger in job_definitions()]
    assert len(names) == 20
    assert names.index("collect_kind_schedule") < names.index("score_all_active")
    assert names.index("collect_kind_schedule") < names.index(
        "collect_kind_listings_for_current_month"
    )
    assert names.index("collect_kind_schedule") < names.index(
        "collect_public_dart_for_current_month"
    )
    assert names.index("collect_prices_due") < names.index("evaluate_outcomes_due")
    assert "collect_dart_for_active" in names
    assert "collect_news_for_active" in names
    assert names.index("send_daily_summary") < names.index("send_subscription_d_minus_one_emails")
    assert names.index("collect_kind_schedule_for_subscription_d_minus_one") < names.index(
        "collect_public_dart_for_subscription_d_minus_one"
    )
    assert names.index("collect_public_dart_for_subscription_d_minus_one") < names.index(
        "reconcile_subscription_d_minus_one_evidence"
    )
    assert names.index("reconcile_subscription_d_minus_one_evidence") < names.index(
        "send_subscription_d_minus_one_emails"
    )
    assert "send_weekly_email_summary" not in names


def test_help_exposes_discoverable_commands():
    text = help_text()
    assert "/today" in text
    assert "/calc" in text
    assert "/backtest" in text
    assert "/refresh" in text


def test_refresh_cooldown_uses_persistent_lease(monkeypatch):
    now = datetime(2026, 7, 19, tzinfo=timezone.utc)
    calls = []

    def reserve(_channel, _event_key, recipient, *, lease_seconds, now):
        calls.append((recipient, lease_seconds))
        if len(calls) == 1:
            return SimpleNamespace(lease_until=now + timedelta(seconds=lease_seconds)), True
        return SimpleNamespace(lease_until=now + timedelta(seconds=42)), False

    monkeypatch.setattr("app.services.telegram.reserve_notification", reserve)

    assert acquire_refresh_cooldown(1, now=now) == 0
    assert acquire_refresh_cooldown(1, now=now) == 42
    assert calls == [("1", 60), ("1", 60)]


def test_manual_refresh_result_counts_failures_and_skips(monkeypatch):
    from app.services import jobs

    monkeypatch.setattr(jobs, "collect_dart_for_ipo", lambda _name: {"updated": True})
    monkeypatch.setattr(
        jobs,
        "collect_financials_for_ipo",
        lambda _name: {"skipped": True, "reason": "credential missing"},
    )
    monkeypatch.setattr(
        jobs,
        "collect_news_trend_for_ipo",
        lambda _name: {"updated": True, "errors": ["trend failed"]},
    )

    def fail_score(_name):
        raise RuntimeError("score failed")

    monkeypatch.setattr(jobs, "score_one", fail_score)

    result = _run_refresh_operations("테스트")

    assert result["processed"] == 4
    assert result["success"] == 1
    assert result["partial"] == 1
    assert result["skipped"] == 1
    assert result["failed"] == 1


def test_telegram_backtest_uses_operational_model_bundle(monkeypatch):
    from app.services import telegram

    bundle = object()

    class Scope:
        def __enter__(self):
            return "session"

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(telegram, "session_scope", lambda: Scope())
    monkeypatch.setattr(
        telegram,
        "build_operational_backtest",
        lambda session: bundle if session == "session" else None,
    )
    monkeypatch.setattr(
        telegram,
        "format_operational_backtest",
        lambda value: "operational-only" if value is bundle else "unexpected",
    )

    assert telegram._backtest_text() == "operational-only"
