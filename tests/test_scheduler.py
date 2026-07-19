from types import SimpleNamespace

import pytest

from app import config as config_module
from app.config import DEFAULT_EMAIL_RECIPIENT
from app.services import scheduler


class FakeScheduler:
    instances = []
    start_error = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.jobs = []
        type(self).instances.append(self)

    def add_job(self, fn, trigger, **kwargs):
        self.jobs.append((fn, trigger, kwargs))

    def start(self):
        if self.start_error:
            raise self.start_error


class FakeTrigger:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def test_email_job_definitions_keep_kst_delivery_times():
    definitions = {name: trigger for name, _fn, trigger in scheduler.job_definitions()}

    assert definitions["collect_kind_listings_for_current_month"] == {
        "hour": 7,
        "minute": 5,
    }
    assert definitions["collect_public_dart_for_current_month"] == {
        "hour": 7,
        "minute": 15,
    }
    assert definitions["reconcile_current_month_evidence"] == {
        "hour": 8,
        "minute": 10,
    }
    assert definitions["collect_kind_schedule_for_subscription_d_minus_one"] == {
        "hour": 17,
        "minute": 10,
    }
    assert definitions["collect_kind_listings_for_subscription_d_minus_one"] == {
        "hour": 17,
        "minute": 20,
    }
    assert definitions["collect_public_dart_for_subscription_d_minus_one"] == {
        "hour": 17,
        "minute": 30,
    }
    assert definitions["reconcile_subscription_d_minus_one_evidence"] == {
        "hour": 18,
        "minute": 10,
    }
    assert definitions["send_subscription_d_minus_one_emails"] == {
        "hour": 18,
        "minute": 30,
    }
    assert "send_weekly_email_summary" not in definitions


def test_real_default_email_config_registers_jobs_in_waiting_state(monkeypatch, caplog):
    caplog.set_level("INFO", logger="app.services.scheduler")
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
    settings = config_module.get_settings()
    assert settings.email_recipients == (DEFAULT_EMAIL_RECIPIENT,)
    assert settings.email_configured is False

    FakeScheduler.instances = []
    FakeScheduler.start_error = None
    monkeypatch.setattr(scheduler, "BlockingScheduler", FakeScheduler)
    monkeypatch.setattr(scheduler, "CronTrigger", FakeTrigger)

    scheduler.run_scheduler()

    registered = {entry[2]["id"] for entry in FakeScheduler.instances[0].jobs}
    assert "send_subscription_d_minus_one_emails" in registered
    assert "send_weekly_email_summary" not in registered
    assert "fixed_recipients=1" in caplog.text
    assert "d1_registered=True" in caplog.text
    assert "smtp_delivery=waiting" in caplog.text
    assert DEFAULT_EMAIL_RECIPIENT not in caplog.text


@pytest.mark.parametrize("start_error", [None, KeyboardInterrupt()])
@pytest.mark.parametrize("email_configured", [False, True])
def test_run_scheduler_registers_and_starts_all_jobs(
    monkeypatch, caplog, start_error, email_configured
):
    caplog.set_level("INFO", logger="app.services.scheduler")
    FakeScheduler.instances = []
    FakeScheduler.start_error = start_error
    monkeypatch.setattr(
        scheduler,
        "get_settings",
        lambda: SimpleNamespace(
            timezone="Asia/Seoul",
            email_recipients=("first@example.com", "second@example.com"),
            email_configured=email_configured,
        ),
    )
    monkeypatch.setattr(scheduler, "BlockingScheduler", FakeScheduler)
    monkeypatch.setattr(scheduler, "CronTrigger", FakeTrigger)
    monkeypatch.setattr(
        scheduler,
        "job_definitions",
        lambda: [
            (
                "send_subscription_d_minus_one_emails",
                lambda: {"ok": True},
                {"hour": 18, "minute": 30},
            ),
        ],
    )

    scheduler.run_scheduler()

    instance = FakeScheduler.instances[0]
    assert instance.kwargs["timezone"] == "Asia/Seoul"
    assert instance.jobs[0][2] == {
        "id": "send_subscription_d_minus_one_emails",
        "replace_existing": True,
    }
    assert instance.jobs[0][1].kwargs == {
        "timezone": "Asia/Seoul",
        "hour": 18,
        "minute": 30,
    }
    assert len(instance.jobs) == 1
    assert "fixed_recipients=2" in caplog.text
    assert "d1_registered=True" in caplog.text
    assert f"smtp_delivery={'enabled' if email_configured else 'waiting'}" in caplog.text
    assert "first@example.com" not in caplog.text
    assert "second@example.com" not in caplog.text


def test_wrapped_job_handles_success_partial_nondict_and_exception(monkeypatch):
    alerts = []
    monkeypatch.setattr(
        scheduler,
        "_send_operational_alert",
        lambda name, detail, *, status: alerts.append((name, detail, status)),
    )

    monkeypatch.setattr(scheduler, "run_tracked_job", lambda _name, _fn: {"success": 1})
    scheduler._job("success", lambda: None)()
    assert alerts == []

    monkeypatch.setattr(
        scheduler,
        "run_tracked_job",
        lambda _name, _fn: {"failed": 1, "partial": 2},
    )
    scheduler._job("partial", lambda: None)()
    assert alerts[-1][0::2] == ("partial", "partial")
    assert "3건" in alerts[-1][1]

    monkeypatch.setattr(scheduler, "run_tracked_job", lambda _name, _fn: "ok")
    scheduler._job("nondict", lambda: None)()
    assert len(alerts) == 1

    def fail(_name, _fn):
        raise RuntimeError("token=secret")

    monkeypatch.setattr(scheduler, "run_tracked_job", fail)
    scheduler._job("failed", lambda: None)()
    assert alerts[-1] == ("failed", "RuntimeError: token=secret", "failed")


def test_failure_count_ignores_non_integer_values():
    assert scheduler._failure_count({"failures": 2, "partial": 1}) == 3
    assert scheduler._failure_count({"failed": [], "partial": "1"}) == 0
    assert scheduler._failure_count({}) == 0


def test_operational_alert_is_disabled_sent_and_failure_isolated(monkeypatch):
    monkeypatch.setattr(
        scheduler,
        "get_settings",
        lambda: SimpleNamespace(telegram_configured=False),
    )
    scheduler._send_operational_alert("job", "token=secret", status="failed")

    sent = []
    monkeypatch.setattr(
        scheduler,
        "get_settings",
        lambda: SimpleNamespace(telegram_configured=True),
    )
    monkeypatch.setattr(
        "app.services.telegram.send_telegram_message",
        lambda text, *, event_key: sent.append((text, event_key)),
    )
    scheduler._send_operational_alert("job", "token=secret", status="partial")
    assert "secret" not in sent[0][0]
    assert ":partial:" in sent[0][1]

    def fail(*_args, **_kwargs):
        raise RuntimeError("alert down")

    monkeypatch.setattr("app.services.telegram.send_telegram_message", fail)
    scheduler._send_operational_alert("job", "detail", status="failed")
