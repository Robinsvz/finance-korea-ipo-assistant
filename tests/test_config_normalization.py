from types import SimpleNamespace

import pytest

from app import config as config_module
from app.config import (
    DEFAULT_EMAIL_RECIPIENT,
    get_settings,
    load_yaml_config,
    normalize_database_url,
    validate_email_mailbox,
)
from app.services import normalization
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


def test_non_relative_database_url_is_preserved():
    database_url = "postgresql://db.example.com/ipo"
    assert normalize_database_url(database_url) == database_url


def test_settings_parse_configured_services_and_numeric_values(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.setenv("DART_API_KEY", "dart")
    monkeypatch.setenv("NAVER_CLIENT_ID", "naver-id")
    monkeypatch.setenv("NAVER_CLIENT_SECRET", "naver-secret")
    monkeypatch.setenv("HTTP_TIMEOUT_SEC", "12.5")
    monkeypatch.setenv("HTTP_MAX_RETRIES", "4")

    settings = get_settings()

    assert settings.telegram_configured is True
    assert settings.dart_configured is True
    assert settings.naver_configured is True
    assert settings.http_timeout_sec == 12.5
    assert settings.http_max_retries == 4


def test_settings_parse_complete_email_configuration(monkeypatch):
    monkeypatch.setenv("EMAIL_TO", DEFAULT_EMAIL_RECIPIENT)
    monkeypatch.setenv("EMAIL_FROM", "Korea IPO <bot@example.com>")
    monkeypatch.setenv("EMAIL_REPLY_TO", "reply@example.com")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "465")
    monkeypatch.setenv("SMTP_USERNAME", "smtp-user")
    monkeypatch.setenv("SMTP_PASSWORD", "smtp-secret")
    monkeypatch.setenv("SMTP_SECURITY", "SSL")
    monkeypatch.setenv("SMTP_TIMEOUT_SEC", "12.5")

    settings = get_settings()

    assert settings.email_recipients == (DEFAULT_EMAIL_RECIPIENT,)
    assert settings.email_from == "Korea IPO <bot@example.com>"
    assert settings.email_reply_to == "reply@example.com"
    assert settings.smtp_host == "smtp.example.com"
    assert settings.smtp_port == 465
    assert settings.smtp_username == "smtp-user"
    assert settings.smtp_password == "smtp-secret"
    assert settings.smtp_security == "SSL"
    assert settings.smtp_timeout_sec == 12.5
    assert settings.email_configured is True


def test_mailbox_validation_accepts_one_display_address_and_rejects_malformed_values():
    assert validate_email_mailbox("Korea IPO <bot@example.com>", "EMAIL_FROM") == "bot@example.com"
    for value in (
        "",
        "not-an-address",
        "local@",
        "a b@example.com",
        "first@example.com, second@example.com",
        "safe@example.com\r\nBcc: stolen@example.com",
    ):
        with pytest.raises(ValueError, match="EMAIL_FROM"):
            validate_email_mailbox(value, "EMAIL_FROM")


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("EMAIL_FROM", "not-an-address"),
        ("EMAIL_REPLY_TO", "reply@example.com extra"),
    ],
)
def test_malformed_mailboxes_are_not_reported_as_configured(monkeypatch, name, value):
    monkeypatch.setenv("EMAIL_TO", DEFAULT_EMAIL_RECIPIENT)
    monkeypatch.setenv("EMAIL_FROM", "bot@example.com")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv(name, value)

    assert get_settings().email_configured is False


def test_absent_or_partial_email_configuration_does_not_break_settings(monkeypatch):
    monkeypatch.setattr(config_module, "load_dotenv", lambda *_args, **_kwargs: None)
    email_variables = (
        "EMAIL_TO",
        "EMAIL_FROM",
        "EMAIL_REPLY_TO",
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USERNAME",
        "SMTP_PASSWORD",
        "SMTP_SECURITY",
        "SMTP_TIMEOUT_SEC",
    )
    for name in email_variables:
        monkeypatch.delenv(name, raising=False)

    absent = get_settings()
    assert absent.email_recipients == (DEFAULT_EMAIL_RECIPIENT,)
    assert absent.smtp_port == 587
    assert absent.smtp_security == "starttls"
    assert absent.smtp_timeout_sec == 20
    assert absent.email_configured is False

    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    partial = get_settings()
    assert partial.smtp_host == "smtp.example.com"
    assert partial.email_configured is False


@pytest.mark.parametrize(("name", "value"), [("SMTP_PORT", "0"), ("SMTP_TIMEOUT_SEC", "0")])
def test_invalid_smtp_numeric_settings_are_rejected(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match="SMTP port and timeout"):
        get_settings()


@pytest.mark.parametrize(
    ("overrides", "configured"),
    [
        ({"SMTP_SECURITY": "none"}, False),
        ({"SMTP_USERNAME": "smtp-user", "SMTP_PASSWORD": ""}, False),
        ({"SMTP_USERNAME": "", "SMTP_PASSWORD": "smtp-secret"}, False),
        ({"SMTP_USERNAME": "", "SMTP_PASSWORD": ""}, True),
    ],
)
def test_email_configured_requires_supported_security_and_complete_auth_pair(
    monkeypatch, overrides, configured
):
    monkeypatch.setenv("EMAIL_TO", DEFAULT_EMAIL_RECIPIENT)
    monkeypatch.setenv("EMAIL_FROM", "bot@example.com")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    for name, value in overrides.items():
        monkeypatch.setenv(name, value)

    assert get_settings().email_configured is configured


def test_email_recipient_is_fixed_and_v3_feature_flags_are_parsed(monkeypatch):
    monkeypatch.delenv("UNSET_BOOLEAN_FOR_TEST", raising=False)
    assert config_module._env_bool("UNSET_BOOLEAN_FOR_TEST", True) is True
    monkeypatch.setenv("UNSET_BOOLEAN_FOR_TEST", "   ")
    assert config_module._env_bool("UNSET_BOOLEAN_FOR_TEST", False) is False

    monkeypatch.setenv("EMAIL_TO", "someone-else@example.com")
    with pytest.raises(ValueError, match="EMAIL_TO is fixed"):
        get_settings()

    monkeypatch.setenv("EMAIL_TO", DEFAULT_EMAIL_RECIPIENT)
    monkeypatch.setenv("EMAIL_PRODUCT_MODE", "v3-facts")
    monkeypatch.setenv("HISTORICAL_EVIDENCE_ENABLED", "yes")
    monkeypatch.setenv("DART_PUBLIC_FALLBACK_ENABLED", "off")
    monkeypatch.setenv("EMAIL_MAX_HTML_BYTES", "12345")
    settings = get_settings()
    assert settings.email_product_mode == "v3-facts"
    assert settings.historical_evidence_enabled is True
    assert settings.dart_public_fallback_enabled is False
    assert settings.email_max_html_bytes == 12345

    monkeypatch.setenv("EMAIL_REPLY_TO", "")
    monkeypatch.setenv("EMAIL_FROM", "bot@example.com")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    assert get_settings().email_configured is True


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("EMAIL_PRODUCT_MODE", "unknown", "EMAIL_PRODUCT_MODE"),
        ("EMAIL_MAX_HTML_BYTES", "999", "EMAIL_MAX_HTML_BYTES"),
        ("HISTORICAL_EVIDENCE_ENABLED", "sometimes", "must be a boolean"),
    ],
)
def test_invalid_v3_feature_settings_are_rejected(monkeypatch, name, value, message):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=message):
        get_settings()


def test_invalid_http_settings_are_rejected(monkeypatch):
    monkeypatch.setenv("HTTP_TIMEOUT_SEC", "0")
    with pytest.raises(ValueError, match="HTTP timeout/retry"):
        get_settings()


def test_invalid_ipo_window_settings_are_rejected(monkeypatch):
    monkeypatch.setenv("KIND_LOOKBACK_DAYS", "-1")
    with pytest.raises(ValueError, match="IPO cash and KIND date window"):
        get_settings()


def test_yaml_config_must_be_a_mapping(tmp_path, monkeypatch):
    (tmp_path / "bad.yaml").write_text("- list-item\n", encoding="utf-8")
    monkeypatch.setattr(
        config_module,
        "get_settings",
        lambda: SimpleNamespace(config_dir=tmp_path),
    )

    with pytest.raises(ValueError, match="must contain a mapping"):
        load_yaml_config("bad.yaml")


def test_scoring_config_totals_100():
    config = load_yaml_config("scoring_rules.yaml")
    weights = config["score_weights"]
    assert sum(value for key, value in weights.items() if key != "max_risk_penalty") == 100


def test_spac_names_do_not_collapse():
    assert normalize_company_name("한국제16호스팩") != normalize_company_name("한국제17호스팩")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("한투", "한국투자증권"),
        ("엔에이치투자증권(주)", "NH투자증권"),
        ("KB", "KB증권"),
        ("케이비증권㈜", "KB증권"),
        ("아이비케이투자증권㈜", "IBK투자증권"),
    ],
)
def test_broker_aliases_come_from_config(raw, expected):
    assert normalize_broker_name(raw) == expected


def test_broker_normalization_falls_back_when_config_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        normalization,
        "load_yaml_config",
        lambda _name: (_ for _ in ()).throw(OSError("missing")),
    )
    assert normalize_broker_name("알 수 없는 증권") == "알수없는증권"


def test_numeric_parsers_handle_parentheses():
    assert parse_int("(1,200)") == -1200
    assert parse_float("(1,200.5)") == -1200.5


def test_numeric_parsers_reject_empty_and_non_numeric_values():
    assert parse_int(None) is None
    assert parse_int("N/A") is None
    assert parse_int("숫자없음") is None
    assert parse_float(None) is None
    assert parse_float("-") is None
    assert parse_float("숫자없음") is None


def test_invalid_calendar_date_returns_none():
    assert parse_date("2026-02-30") is None


def test_date_parser_rejects_empty_and_unrecognized_values():
    assert parse_date(None) is None
    assert parse_date("&nbsp;") is None
    assert parse_date("날짜없음") is None


def test_negative_percent_is_normalized():
    assert pct_ratio(-20) == -0.2
    assert pct_ratio(None) is None
