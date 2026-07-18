import pytest

from app.services.observability import mask_secrets
from app.services.scheduler import job_definitions
from app.services.telegram import (
    _parse_amount,
    consume_refresh_cooldown,
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
    assert len(names) == 12
    assert names.index("collect_kind_schedule") < names.index("score_all_active")
    assert names.index("collect_prices_due") < names.index("evaluate_outcomes_due")
    assert "collect_dart_for_active" in names
    assert "collect_news_for_active" in names


def test_help_exposes_discoverable_commands():
    text = help_text()
    assert "/today" in text
    assert "/calc" in text
    assert "/refresh" in text


def test_refresh_cooldown_is_per_user():
    last_used = {}
    assert consume_refresh_cooldown(1, last_used, now=100) == 0
    assert consume_refresh_cooldown(1, last_used, now=110) == 50
    assert consume_refresh_cooldown(2, last_used, now=110) == 0
    assert consume_refresh_cooldown(1, last_used, now=160) == 0
