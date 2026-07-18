from datetime import datetime, timezone

from app.services.clock import format_local_datetime, local_today


def test_local_today_uses_configured_business_timezone():
    utc_value = datetime(2026, 7, 17, 23, 30, tzinfo=timezone.utc)
    assert local_today(utc_value, "Asia/Seoul").isoformat() == "2026-07-18"


def test_format_local_datetime_converts_utc_to_kst(monkeypatch):
    monkeypatch.setenv("IPO_TIMEZONE", "Asia/Seoul")
    value = datetime(2026, 7, 17, 23, 30, tzinfo=timezone.utc)
    assert format_local_datetime(value) == "2026-07-18 08:30 KST"
