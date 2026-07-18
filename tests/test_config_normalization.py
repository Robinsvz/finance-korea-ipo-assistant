import pytest

from app.config import get_settings, load_yaml_config
from app.services.normalization import (
    normalize_broker_name,
    normalize_company_name,
    parse_date,
    parse_float,
    parse_int,
    pct_ratio,
)


def test_invalid_timezone_is_rejected(monkeypatch):
    monkeypatch.setenv("IPO_TIMEZONE", "Not/AZone")
    with pytest.raises(ValueError, match="Invalid IPO_TIMEZONE"):
        get_settings()


def test_allowed_chats_fall_back_to_default_chat(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    assert get_settings().allowed_telegram_chat_ids == ("123",)


def test_scoring_config_totals_100():
    config = load_yaml_config("scoring_rules.yaml")
    weights = config["score_weights"]
    assert sum(value for key, value in weights.items() if key != "max_risk_penalty") == 100


def test_spac_names_do_not_collapse():
    assert normalize_company_name("한국제16호스팩") != normalize_company_name("한국제17호스팩")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("한투", "한국투자증권"), ("엔에이치투자증권(주)", "NH투자증권"), ("KB", "KB증권")],
)
def test_broker_aliases_come_from_config(raw, expected):
    assert normalize_broker_name(raw) == expected


def test_numeric_parsers_handle_parentheses():
    assert parse_int("(1,200)") == -1200
    assert parse_float("(1,200.5)") == -1200.5


def test_invalid_calendar_date_returns_none():
    assert parse_date("2026-02-30") is None


def test_negative_percent_is_normalized():
    assert pct_ratio(-20) == -0.2
