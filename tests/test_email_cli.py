from datetime import date
from types import SimpleNamespace

import pytest

from app import cli
from app.services.email import EmailContent


def test_smtp_connection_test_uses_a_fresh_idempotency_key_each_time(monkeypatch):
    event_keys = []

    def send(_content, *, event_key):
        event_keys.append(event_key)
        return {"sent": 1}

    monkeypatch.setattr(cli, "send_email_message", send)
    monkeypatch.setattr(cli, "print_json", lambda _result: None)

    cli.cmd_send_test_email(SimpleNamespace(strict=True))
    cli.cmd_send_test_email(SimpleNamespace(strict=True))

    assert len(set(event_keys)) == 2
    assert all(key.startswith("smtp-connection-test:") for key in event_keys)


def test_email_preview_accepts_v2_decision_and_schedule_counts(monkeypatch, capsys):
    daily = EmailContent(subject="daily", text="daily text", html="<p>daily html</p>")
    weekly = EmailContent(subject="weekly", text="weekly text", html="<p>weekly html</p>")
    monkeypatch.setattr(cli, "build_daily_email_content", lambda: (daily, 1, 2))
    monkeypatch.setattr(cli, "build_weekly_email_content", lambda: (weekly, 0, 4))

    cli.cmd_preview_email(SimpleNamespace(weekly=False, html=False))
    cli.cmd_preview_email(SimpleNamespace(weekly=True, html=True))

    output = capsys.readouterr().out
    assert "daily text" in output
    assert "weekly html" in output


def test_monthly_email_preview_and_sender_use_calendar_month(monkeypatch, capsys):
    bundle = SimpleNamespace(
        parts=(
            EmailContent(subject="one", text="text one", html="<p>html one</p>"),
            EmailContent(subject="two", text="text two", html="<p>html two</p>"),
        )
    )
    targets = []
    monkeypatch.setattr(
        cli,
        "build_monthly_email_content",
        lambda target: targets.append(target) or bundle,
    )

    cli.cmd_preview_monthly_email(SimpleNamespace(month="2026-07", html=False, part=None))
    cli.cmd_preview_monthly_email(SimpleNamespace(month=None, html=True, part=2))
    output = capsys.readouterr().out
    assert "text one" in output and "text two" in output
    assert "html two" in output
    assert targets == [date(2026, 7, 1), None]

    with pytest.raises(SystemExit, match="part must be between"):
        cli.cmd_preview_monthly_email(SimpleNamespace(month="2026-07", html=False, part=3))

    sent = []
    monkeypatch.setattr(
        cli,
        "send_monthly_email_snapshot",
        lambda target, *, trigger: sent.append((target, trigger)) or {"sent": 1},
    )
    monkeypatch.setattr(cli, "print_json", lambda value: sent.append(value))
    cli.cmd_send_monthly_email(SimpleNamespace(month="2026-07", canary=True))
    cli.cmd_send_monthly_email(SimpleNamespace(month=None, canary=False))
    assert sent[0] == (date(2026, 7, 1), "canary")
    assert sent[2] == (None, "manual")


def test_month_parser_and_cli_contracts_reject_invalid_values():
    parser = cli.build_parser()
    preview = parser.parse_args(
        ["preview-monthly-email", "--month", "2026-07", "--html", "--part", "1"]
    )
    sender = parser.parse_args(["send-monthly-email", "--month", "2026-07", "--canary"])
    assert preview.month == sender.month == "2026-07"
    assert preview.html is True and preview.part == 1
    assert sender.canary is True
    assert cli._parse_month(None) is None
    with pytest.raises(SystemExit, match="Invalid month"):
        cli._parse_month("2026-13")


def test_d1_preview_prints_manifest_and_each_report_without_sending(monkeypatch, capsys):
    first = SimpleNamespace(
        ipo_id=11,
        offering_id="offering:11",
        corp_name="첫째",
        subscription_start_date=date(2026, 7, 20),
        evidence_warnings=("기관 가격분포",),
        bundle=SimpleNamespace(
            parts=(EmailContent("첫째 제목", "첫째 본문", "<p>첫째 HTML</p>"),),
            universe_hash="a" * 64,
            evidence_hash="b" * 64,
        ),
    )
    second = SimpleNamespace(
        ipo_id=12,
        offering_id="offering:12",
        corp_name="둘째",
        subscription_start_date=date(2026, 7, 20),
        evidence_warnings=(),
        bundle=SimpleNamespace(
            parts=(EmailContent("둘째 제목", "둘째 본문", "<p>둘째 HTML</p>"),),
            universe_hash="c" * 64,
            evidence_hash="d" * 64,
        ),
    )
    batch = SimpleNamespace(
        run_date=date(2026, 7, 19),
        subscription_date=date(2026, 7, 20),
        total_candidates=3,
        ready=(first, second),
        blocked=({"ipo_id": 13, "reason": "KIND 불일치"},),
    )
    seen = []
    monkeypatch.setattr(
        cli,
        "build_subscription_d_minus_one_email_batch",
        lambda target: seen.append(target) or batch,
    )
    monkeypatch.setattr(
        cli,
        "send_email_message",
        lambda *_args, **_kwargs: pytest.fail("preview must not call SMTP"),
    )

    cli.cmd_preview_d1_emails(
        SimpleNamespace(
            send_date="2026-07-19",
            ipo=None,
            html=False,
            manifest_only=False,
        )
    )

    output = capsys.readouterr().out
    assert seen == [date(2026, 7, 19)]
    assert '"recipient": "dummy1.kim@gmail.com"' in output
    assert '"candidate_count": 3' in output
    assert '"ready_count": 2' in output
    assert '"external_send": false' in output
    assert "첫째 본문" in output and "둘째 본문" in output
    assert "첫째 HTML" not in output
    assert "subscription-d1-email:v3-facts-d1:2026-07-20:" in output

    cli.cmd_preview_d1_emails(
        SimpleNamespace(
            send_date=None,
            ipo="둘째",
            html=True,
            manifest_only=False,
        )
    )
    output = capsys.readouterr().out
    assert "<p>둘째 HTML</p>" in output
    assert "첫째 HTML" not in output


def test_d1_preview_manifest_only_empty_and_unknown_filter(monkeypatch, capsys):
    batch = SimpleNamespace(
        run_date=date(2026, 7, 19),
        subscription_date=date(2026, 7, 20),
        total_candidates=0,
        ready=(),
        blocked=(),
    )
    monkeypatch.setattr(cli, "local_today", lambda: date(2026, 7, 19))
    monkeypatch.setattr(
        cli,
        "build_subscription_d_minus_one_email_batch",
        lambda _target: batch,
    )
    cli.cmd_preview_d1_emails(
        SimpleNamespace(send_date=None, ipo=None, html=False, manifest_only=True)
    )
    output = capsys.readouterr().out
    assert '"selected_count": 0' in output
    assert "D-1 REPORT" not in output

    with pytest.raises(SystemExit, match="Ready D-1 IPO not found"):
        cli.cmd_preview_d1_emails(
            SimpleNamespace(send_date=None, ipo="없는기업", html=False, manifest_only=True)
        )


def test_d1_preview_parser_contract():
    args = cli.build_parser().parse_args(
        [
            "preview-d1-emails",
            "--send-date",
            "2026-07-19",
            "--ipo",
            "테스트기업",
            "--html",
            "--manifest-only",
        ]
    )
    assert args.send_date == "2026-07-19"
    assert args.ipo == "테스트기업"
    assert args.html is True
    assert args.manifest_only is True
