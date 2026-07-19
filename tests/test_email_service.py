from types import SimpleNamespace

import pytest

from app import config as config_module
from app.config import DEFAULT_EMAIL_RECIPIENT
from app.services import email as email_service
from app.services.email import EmailAuditContext, EmailContent, send_email_message


def _settings(**overrides):
    values = {
        "email_recipients": ("reader@example.com",),
        "email_from": "Korea IPO <bot@example.com>",
        "email_reply_to": "reply@example.com",
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "smtp_username": "smtp-user",
        "smtp_password": "smtp-secret",
        "smtp_security": "starttls",
        "smtp_timeout_sec": 12.5,
        "email_configured": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _content(**overrides):
    values = {
        "subject": "[IPO 판단] 오늘",
        "text": "텍스트 본문",
        "html": "<p>HTML 본문</p>",
    }
    values.update(overrides)
    return EmailContent(**values)


def _clear_email_environment(monkeypatch):
    monkeypatch.setattr(config_module, "load_dotenv", lambda *_args, **_kwargs: None)
    for name in (
        "EMAIL_TO",
        "EMAIL_FROM",
        "EMAIL_REPLY_TO",
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USERNAME",
        "SMTP_PASSWORD",
        "SMTP_SECURITY",
        "SMTP_TIMEOUT_SEC",
    ):
        monkeypatch.delenv(name, raising=False)


class FakeSMTP:
    instances = []
    refused_recipient = None
    failed_recipient = None

    def __init__(self, host, port, timeout=None, context=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.context = context
        self.calls = []
        self.messages = []
        type(self).instances.append(self)

    def __enter__(self):
        self.calls.append("enter")
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.calls.append("exit")

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self, *, context):
        assert context is not None
        self.calls.append("starttls")

    def login(self, username, password):
        self.calls.append(("login", username, password))

    def send_message(self, message, *, from_addr, to_addrs):
        recipient = to_addrs[0]
        self.calls.append(("send", from_addr, recipient))
        self.messages.append(message)
        if recipient == type(self).failed_recipient:
            raise RuntimeError(
                "password=plain-secret smtps://smtp-user:uri-secret@smtp.example.com"
            )
        if recipient == type(self).refused_recipient:
            return {recipient: (550, b"rejected")}
        return {}


@pytest.fixture(autouse=True)
def _reset_fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTP.refused_recipient = None
    FakeSMTP.failed_recipient = None
    monkeypatch.setattr(email_service, "record_notification_content", lambda *_a, **_k: None)


def test_email_is_safe_noop_when_configuration_is_absent(monkeypatch):
    settings = _settings(
        email_recipients=("dummy1.kim@gmail.com",),
        email_from="",
        email_reply_to="",
        smtp_host="",
        smtp_username="",
        smtp_password="",
        email_configured=False,
    )
    monkeypatch.setattr(email_service, "get_settings", lambda: settings)
    monkeypatch.setattr(
        email_service,
        "reserve_notification",
        lambda *_args, **_kwargs: pytest.fail("reservation must not run"),
    )

    result = send_email_message(_content(), event_key="daily:2026-07-19")

    assert result == {
        "processed": 0,
        "sent": 0,
        "duplicate": 0,
        "failed": 0,
        "delivery_state_unknown": 0,
        "skipped": True,
        "reason": "Email is not configured",
        "failures": [],
    }


def test_default_recipient_flows_from_real_settings_to_safe_noop(monkeypatch):
    _clear_email_environment(monkeypatch)
    settings = config_module.get_settings()
    assert settings.email_recipients == (DEFAULT_EMAIL_RECIPIENT,)
    assert settings.email_configured is False

    monkeypatch.setattr(
        email_service,
        "reserve_notification",
        lambda *_args, **_kwargs: pytest.fail("no-op must not reserve a notification"),
    )
    monkeypatch.setattr(
        email_service.smtplib,
        "SMTP",
        lambda *_args, **_kwargs: pytest.fail("no-op must not open SMTP"),
    )
    monkeypatch.setattr(
        email_service.smtplib,
        "SMTP_SSL",
        lambda *_args, **_kwargs: pytest.fail("no-op must not open SMTP_SSL"),
    )

    result = send_email_message(_content(), event_key="daily:2026-07-19")

    assert result["skipped"] is True
    assert result["processed"] == result["sent"] == 0
    assert result["reason"] == "Email is not configured"


def test_default_recipient_flows_through_complete_config_to_fake_smtp(monkeypatch):
    _clear_email_environment(monkeypatch)
    monkeypatch.setenv("EMAIL_FROM", "Korea IPO <bot@example.com>")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USERNAME", "smtp-user")
    monkeypatch.setenv("SMTP_PASSWORD", "smtp-secret")

    settings = config_module.get_settings()
    assert settings.email_recipients == (DEFAULT_EMAIL_RECIPIENT,)
    assert settings.email_configured is True

    finishes = []
    monkeypatch.setattr(
        email_service,
        "reserve_notification",
        lambda *_args, **_kwargs: (SimpleNamespace(id=1), True),
    )
    monkeypatch.setattr(
        email_service,
        "finish_notification",
        lambda event_id, **kwargs: finishes.append((event_id, kwargs)),
    )
    monkeypatch.setattr(email_service.smtplib, "SMTP", FakeSMTP)

    result = send_email_message(_content(), event_key="daily:2026-07-19")

    assert result["processed"] == result["sent"] == 1
    assert result["failed"] == result["duplicate"] == 0
    assert len(FakeSMTP.instances) == 1
    delivered = FakeSMTP.instances[0]
    assert delivered.messages[0]["To"] == DEFAULT_EMAIL_RECIPIENT
    assert ("send", "bot@example.com", DEFAULT_EMAIL_RECIPIENT) in delivered.calls
    assert finishes[0][1]["status"] == "sent"


def test_audit_context_is_persisted_before_smtp_delivery(monkeypatch):
    settings = _settings(email_recipients=("reader@example.com",))
    audit_calls = []
    order = []
    monkeypatch.setattr(email_service, "get_settings", lambda: settings)
    monkeypatch.setattr(
        email_service,
        "reserve_notification",
        lambda *_args, **_kwargs: (SimpleNamespace(id=41), True),
    )

    def record(event_id, **kwargs):
        order.append("audit")
        audit_calls.append((event_id, kwargs))

    class OrderedSMTP(FakeSMTP):
        def send_message(self, message, *, from_addr, to_addrs):
            order.append("smtp")
            return super().send_message(message, from_addr=from_addr, to_addrs=to_addrs)

    monkeypatch.setattr(email_service, "record_notification_content", record)
    monkeypatch.setattr(email_service, "finish_notification", lambda *_a, **_k: None)
    monkeypatch.setattr(email_service.smtplib, "SMTP", OrderedSMTP)
    context = EmailAuditContext(
        universe_hash="a" * 64,
        evidence_hash="b" * 64,
        metadata={"period": "2026-07", "part_number": 1, "part_count": 2},
    )

    result = send_email_message(_content(), event_key="monthly:part:1", audit_context=context)

    assert result["sent"] == 1
    assert order == ["audit", "smtp"]
    assert audit_calls == [
        (
            41,
            {
                "subject": "[IPO 판단] 오늘",
                "text_body": "텍스트 본문",
                "html_body": "<p>HTML 본문</p>",
                "universe_hash": "a" * 64,
                "evidence_hash": "b" * 64,
                "metadata": {"period": "2026-07", "part_number": 1, "part_count": 2},
            },
        )
    ]


@pytest.mark.parametrize(
    "context",
    [
        EmailAuditContext(universe_hash="short"),
        EmailAuditContext(evidence_hash="A" * 64),
        EmailAuditContext(metadata=[]),
    ],
)
def test_invalid_audit_context_is_rejected_before_reservation(monkeypatch, context):
    monkeypatch.setattr(email_service, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        email_service,
        "reserve_notification",
        lambda *_a, **_k: pytest.fail("invalid audit context must not reserve"),
    )

    with pytest.raises(ValueError):
        send_email_message(_content(), event_key="event", audit_context=context)


@pytest.mark.parametrize("finish_fails", [False, True])
def test_audit_persistence_failure_blocks_smtp_and_is_redacted(
    monkeypatch,
    finish_fails,
):
    monkeypatch.setattr(email_service, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        email_service,
        "reserve_notification",
        lambda *_a, **_k: (SimpleNamespace(id=42), True),
    )
    monkeypatch.setattr(
        email_service,
        "record_notification_content",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("smtp_password=audit-secret")),
    )

    def finish(*_args, **_kwargs):
        if finish_fails:
            raise RuntimeError("password=finish-secret")

    monkeypatch.setattr(email_service, "finish_notification", finish)
    monkeypatch.setattr(
        email_service.smtplib,
        "SMTP",
        lambda *_a, **_k: pytest.fail("SMTP must not run without an audit artifact"),
    )

    result = send_email_message(_content(), event_key="event")

    assert result["sent"] == 0
    assert result["failed"] == 1
    assert "audit persistence failed" in result["failures"][0]["error"]
    assert "audit-secret" not in str(result)
    assert "finish-secret" not in str(result)


@pytest.mark.parametrize(
    "settings",
    [
        _settings(email_recipients=(), email_from="", smtp_host="smtp.example.com"),
        _settings(email_recipients=(), email_from="bot@example.com", smtp_host=""),
        _settings(email_recipients=(), email_from="", smtp_host=""),
    ],
)
def test_partial_configuration_is_rejected(monkeypatch, settings):
    monkeypatch.setattr(email_service, "get_settings", lambda: settings)

    with pytest.raises(ValueError, match="incomplete"):
        send_email_message(_content(), event_key="daily:2026-07-19")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"smtp_security": "none"}, "SMTP_SECURITY"),
        ({"smtp_port": 0}, "SMTP_PORT"),
        ({"smtp_timeout_sec": 0}, "SMTP_TIMEOUT_SEC"),
        ({"smtp_password": ""}, "configured together"),
        ({"smtp_username": "", "smtp_password": "secret"}, "configured together"),
    ],
)
def test_invalid_delivery_settings_are_rejected(monkeypatch, overrides, message):
    monkeypatch.setattr(email_service, "get_settings", lambda: _settings(**overrides))

    with pytest.raises(ValueError, match=message):
        send_email_message(_content(), event_key="daily:2026-07-19")


@pytest.mark.parametrize(
    ("content", "event_key", "message"),
    [
        (_content(), " ", "event_key"),
        (_content(subject="safe\r\nBcc: stolen@example.com"), "event", "newlines"),
        (_content(subject=" "), "event", "subject is required"),
        (_content(text=""), "event", "both text and HTML"),
        (_content(html=""), "event", "both text and HTML"),
    ],
)
def test_invalid_content_is_rejected(monkeypatch, content, event_key, message):
    monkeypatch.setattr(email_service, "get_settings", lambda: _settings())

    with pytest.raises(ValueError, match=message):
        send_email_message(content, event_key=event_key)


@pytest.mark.parametrize(
    ("overrides", "recipients", "field"),
    [
        ({"email_from": "not-an-address"}, None, "EMAIL_FROM"),
        ({"email_reply_to": "reply@example.com\nBcc: x@example.com"}, None, "EMAIL_REPLY_TO"),
        ({}, ("invalid",), "email recipient"),
        ({}, ("@example.com",), "email recipient"),
        ({}, ("local@",), "email recipient"),
    ],
)
def test_invalid_mailboxes_are_rejected(monkeypatch, overrides, recipients, field):
    monkeypatch.setattr(email_service, "get_settings", lambda: _settings(**overrides))

    with pytest.raises(ValueError, match=field):
        send_email_message(_content(), event_key="event", recipients=recipients)


def test_starttls_delivery_tracks_success_duplicate_and_failures(monkeypatch):
    recipients = (
        "Alpha@example.com",
        "alpha@example.com",
        "failed@example.com",
        "duplicate@example.com",
        "reserve-error@example.com",
        " ",
    )
    settings = _settings(email_recipients=recipients)
    events = []
    finishes = []

    def reserve(channel, event_key, recipient, *, lease_seconds):
        events.append((channel, event_key, recipient, lease_seconds))
        if recipient == "reserve-error@example.com":
            raise RuntimeError("database unavailable")
        event = SimpleNamespace(id=len(events), status="sent")
        return event, recipient != "duplicate@example.com"

    monkeypatch.setattr(email_service, "get_settings", lambda: settings)
    monkeypatch.setattr(email_service, "reserve_notification", reserve)
    monkeypatch.setattr(
        email_service,
        "finish_notification",
        lambda event_id, **kwargs: finishes.append((event_id, kwargs)),
    )
    monkeypatch.setattr(email_service.smtplib, "SMTP", FakeSMTP)
    FakeSMTP.failed_recipient = "failed@example.com"

    result = send_email_message(_content(), event_key="daily:2026-07-19")

    assert result["processed"] == 4
    assert result["sent"] == 1
    assert result["duplicate"] == 1
    assert result["failed"] == 2
    assert result["skipped"] is False
    serialized = str(result)
    assert "plain-secret" not in serialized
    assert "uri-secret" not in serialized
    assert "***REDACTED***" in serialized
    assert "f***@example.com" in serialized
    assert "r***@example.com" in serialized
    assert all(row[3] == 900 for row in events)

    successful = FakeSMTP.instances[0]
    assert successful.host == "smtp.example.com"
    assert successful.port == 587
    assert successful.timeout == 12.5
    assert successful.context is None
    assert successful.calls[:5] == [
        "enter",
        "ehlo",
        "starttls",
        "ehlo",
        ("login", "smtp-user", "smtp-secret"),
    ]
    message = successful.messages[0]
    assert message["To"] == "Alpha@example.com"
    assert message["From"] == "Korea IPO <bot@example.com>"
    assert message["Reply-To"] == "reply@example.com"
    assert message.is_multipart()
    assert message.get_body(preferencelist=("plain",)).get_content().strip() == "텍스트 본문"
    assert "HTML 본문" in message.get_body(preferencelist=("html",)).get_content()
    assert str(message["Message-ID"]).endswith("@example.com>")
    assert [entry[1]["status"] for entry in finishes] == ["sent", "failed"]


def test_ssl_delivery_without_auth_handles_recipient_refusal(monkeypatch):
    settings = _settings(
        email_recipients=("ok@example.com", "refused@example.com"),
        email_reply_to="",
        smtp_security=" SSL ",
        smtp_port=465,
        smtp_username="",
        smtp_password="",
    )
    finishes = []
    counter = iter((10, 11))
    monkeypatch.setattr(email_service, "get_settings", lambda: settings)
    monkeypatch.setattr(
        email_service,
        "reserve_notification",
        lambda *_args, **_kwargs: (SimpleNamespace(id=next(counter)), True),
    )
    monkeypatch.setattr(
        email_service,
        "finish_notification",
        lambda event_id, **kwargs: finishes.append((event_id, kwargs)),
    )
    monkeypatch.setattr(email_service.smtplib, "SMTP_SSL", FakeSMTP)
    FakeSMTP.refused_recipient = "refused@example.com"

    result = send_email_message(_content(), event_key="weekly:2026-07-20")

    assert result["sent"] == 1
    assert result["failed"] == 1
    assert len(FakeSMTP.instances) == 2
    assert all(instance.context is not None for instance in FakeSMTP.instances)
    assert all(
        not any(call == "starttls" for call in instance.calls) for instance in FakeSMTP.instances
    )
    assert all(
        not any(isinstance(call, tuple) and call[0] == "login" for call in instance.calls)
        for instance in FakeSMTP.instances
    )
    assert finishes[0][1]["status"] == "sent"
    assert finishes[1][1]["status"] == "failed"
    assert "SMTPRecipientsRefused" in result["failures"][0]["error"]


def test_notification_finalization_failures_are_isolated_and_report_unknown_state(
    monkeypatch,
):
    settings = _settings(
        email_recipients=(
            "accepted-unknown@example.com",
            "failed-unknown@example.com",
            "ok@example.com",
        ),
        email_reply_to="",
    )
    counter = iter((1, 2, 3))

    monkeypatch.setattr(email_service, "get_settings", lambda: settings)
    monkeypatch.setattr(
        email_service,
        "reserve_notification",
        lambda *_args, **_kwargs: (SimpleNamespace(id=next(counter)), True),
    )

    def finish(event_id, **_kwargs):
        if event_id in {1, 2}:
            raise RuntimeError("password=tracking-secret")

    monkeypatch.setattr(email_service, "finish_notification", finish)
    monkeypatch.setattr(email_service.smtplib, "SMTP", FakeSMTP)
    FakeSMTP.failed_recipient = "failed-unknown@example.com"

    result = send_email_message(_content(), event_key="daily:2026-07-19")

    assert result["processed"] == 3
    assert result["sent"] == 1
    assert result["failed"] == 2
    assert result["delivery_state_unknown"] == 2
    assert len(FakeSMTP.instances) == 3
    assert {failure["state"] for failure in result["failures"]} == {"delivery-state-unknown"}
    assert "SMTP accepted the message" in result["failures"][0]["error"]
    assert "delivery error" in result["failures"][1]["error"]
    assert "tracking-secret" not in str(result)


def test_message_id_is_stable_and_recipient_specific():
    first = email_service._stable_message_id("event", "USER@example.com", "bot@example.com")
    same = email_service._stable_message_id("event", "user@example.com", "bot@example.com")
    different = email_service._stable_message_id("event", "other@example.com", "bot@example.com")

    assert first == same
    assert first != different
    assert email_service._mask_recipient("invalid") == "***"
