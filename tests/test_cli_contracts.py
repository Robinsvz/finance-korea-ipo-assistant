from datetime import timezone

import pytest

from app.cli import _parse_datetime, build_parser


def test_participation_cli_defaults_to_research_only_and_exposes_event_times():
    parser = build_parser()
    participation = parser.parse_args(
        [
            "record-participation",
            "회사",
            "증권사",
            "--applied-shares",
            "10",
            "--cash-committed",
            "100000",
            "--decision-at",
            "2026-07-19T08:00:00+09:00",
        ]
    )
    exit_args = parser.parse_args(
        [
            "record-exit",
            "1",
            "--shares",
            "1",
            "--price",
            "12000",
            "--sold-at",
            "2026-07-20T00:00:00Z",
        ]
    )

    assert participation.action == "RESEARCH_ONLY"
    assert participation.decision_at.endswith("+09:00")
    assert exit_args.sold_at.endswith("Z")


def test_cli_datetime_parser_requires_an_explicit_offset():
    assert _parse_datetime(None) is None
    assert _parse_datetime("2026-07-20T00:00:00Z").tzinfo == timezone.utc
    with pytest.raises(SystemExit, match="Invalid ISO-8601"):
        _parse_datetime("not-a-date")
    with pytest.raises(SystemExit, match="explicit UTC offset"):
        _parse_datetime("2026-07-20T00:00:00")


def test_cli_backtest_uses_operational_model_bundle(monkeypatch, capsys):
    from app import cli

    bundle = object()
    monkeypatch.setattr(cli, "init_db", lambda: None)

    class Scope:
        def __enter__(self):
            return "session"

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(cli, "session_scope", lambda: Scope())
    monkeypatch.setattr(
        cli,
        "build_operational_backtest",
        lambda session: bundle if session == "session" else None,
    )
    monkeypatch.setattr(
        cli,
        "format_operational_backtest",
        lambda value: "operational-only" if value is bundle else "unexpected",
    )

    cli.cmd_backtest(None)

    assert capsys.readouterr().out.strip() == "operational-only"


def test_validation_cli_defaults_to_active_lineage_and_requires_complete_override():
    parser = build_parser()
    defaults = parser.parse_args(["validation-report"])
    incomplete = parser.parse_args(["validation-report", "--model-version", "legacy-model"])

    assert defaults.model_version is None
    assert defaults.rules_hash is None
    with pytest.raises(ValueError, match="provided together"):
        from app.services.scoring_v3 import resolve_scoring_identity

        resolve_scoring_identity(incomplete.model_version, incomplete.rules_hash)


def test_email_preview_cli_exposes_weekly_and_html_modes():
    parser = build_parser()

    daily = parser.parse_args(["preview-email"])
    weekly_html = parser.parse_args(["preview-email", "--weekly", "--html"])

    assert daily.weekly is daily.html is False
    assert weekly_html.weekly is weekly_html.html is True
