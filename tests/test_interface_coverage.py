from __future__ import annotations

import asyncio
import runpy
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


class Scope:
    def __init__(self, session="session"):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, *_args):
        return None


def test_db_engine_lifecycle_and_migration(monkeypatch, tmp_path):
    from app import db

    sqlite_path = tmp_path / "coverage.db"
    monkeypatch.setattr(
        db,
        "get_settings",
        lambda: SimpleNamespace(database_url=f"sqlite:///{sqlite_path}"),
    )
    engine = db.get_engine(echo=True)
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    engine.dispose()

    sentinel = object()
    captured = {}

    def fake_create_engine(url, **kwargs):
        captured.update(url=url, **kwargs)
        return sentinel

    monkeypatch.setattr(db, "create_engine", fake_create_engine)
    monkeypatch.setattr(
        db,
        "get_settings",
        lambda: SimpleNamespace(database_url="postgresql://example.invalid/db"),
    )
    assert db.get_engine() is sentinel
    assert captured["connect_args"] == {}

    created = []
    monkeypatch.setattr(db.Base.metadata, "create_all", lambda *, bind: created.append(bind))
    monkeypatch.setattr(db, "engine", sentinel)
    db.init_db()
    assert created == [sentinel]

    settings = SimpleNamespace(database_url="sqlite:///migrate.db", project_root=Path("/project"))
    config = SimpleNamespace(set_main_option=lambda key, value: captured.update({key: value}))
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    monkeypatch.setattr("alembic.config.Config", lambda path: captured.update(path=path) or config)
    monkeypatch.setattr(
        "alembic.command.upgrade",
        lambda value, revision: captured.update(config=value, revision=revision),
    )
    db.migrate_db("base")
    assert captured["revision"] == "base"
    assert captured["sqlalchemy.url"] == settings.database_url


def test_db_session_scope_commit_rollback_and_close(monkeypatch):
    from app import db

    events = []

    class Session:
        def commit(self):
            events.append("commit")

        def rollback(self):
            events.append("rollback")

        def close(self):
            events.append("close")

    monkeypatch.setattr(db, "SessionLocal", Session)
    with db.session_scope() as session:
        assert isinstance(session, Session)
    assert events == ["commit", "close"]

    events.clear()
    with pytest.raises(RuntimeError, match="boom"):
        with db.session_scope():
            raise RuntimeError("boom")
    assert events == ["rollback", "close"]


def test_formatter_all_paths():
    from app.models import (
        FinancialMetric,
        Ipo,
        NewsMetric,
        OutcomeEvaluation,
        ScoreSnapshot,
        UnderwriterAllocation,
    )
    from app.schemas import AllocationResult, HealthStatus
    from app.services.backtest import (
        BacktestReport,
        BandBacktestMetrics,
        GrossOutcomeSummary,
        OperationalBacktest,
        StrategyBacktestMetrics,
    )
    from app.services.formatter import (
        _bool,
        _date_range,
        _financial_yoy,
        _fmt_date,
        _fmt_datetime,
        _money,
        _pct,
        format_allocation,
        format_backtest,
        format_financial,
        format_health,
        format_ipo_summary,
        format_operational_backtest,
        format_result,
        format_schedule,
        format_score,
        format_trend,
    )

    healthy = HealthStatus(True, True, "head", "head", True, True, True, True, "db")
    unhealthy = HealthStatus(False, False, None, None, False, False, False, False, "db")
    assert "database: ok" in format_health(healthy)
    assert "missing / expected -" in format_health(unhealthy)

    underwriter = UnderwriterAllocation(broker_name="증권")
    ipo = Ipo(
        corp_name="기업",
        normalized_name="기업",
        market="KOSDAQ",
        demand_start_date=date(2026, 1, 1),
        demand_end_date=date(2026, 1, 2),
        subscription_start_date=date(2026, 1, 3),
        subscription_end_date=date(2026, 1, 4),
        refund_date=date(2026, 1, 5),
        payment_date=date(2026, 1, 6),
        listing_date=date(2026, 1, 7),
        offer_price_final=10_000,
        status="scheduled",
    )
    ipo.underwriters = [underwriter]
    assert "주관사: 증권" in format_ipo_summary(ipo)
    empty_ipo = Ipo(corp_name="빈기업", normalized_name="빈기업", status="scheduled")
    empty_ipo.underwriters = []
    assert "주관사: -" in format_ipo_summary(empty_ipo)
    assert format_schedule("없음", []) == "[없음]\n일정 없음"
    assert "수요예측 시작" in format_schedule("전체", [ipo])
    assert "일정 미정" in format_schedule("미정", [empty_ipo])
    assert (
        format_schedule("범위밖", [ipo], date(2027, 1, 1), date(2027, 1, 2))
        == "[범위밖]\n일정 없음"
    )
    one_day = Ipo(
        corp_name="하루",
        normalized_name="하루",
        subscription_start_date=date(2026, 2, 1),
    )
    one_day.underwriters = []
    assert "2026-02-01 · 청약 시작" in format_schedule("하루", [one_day])

    assert "스냅샷 없음" in format_score(ipo, None)
    legacy = ScoreSnapshot(
        ipo_id=1,
        score_date=date(2026, 1, 1),
        total_score=1,
        confidence_score=2,
        recommendation_band="Neutral",
        model_version="legacy",
        missing_features_json=["x"],
        demand_score=1,
        financial_score=1,
        valuation_score=1,
        structure_score=1,
        news_trend_score=1,
        allocation_score=1,
        market_condition_score=1,
        risk_penalty_score=1,
    )
    assert "세부 점수" in format_score(ipo, legacy)
    v3 = ScoreSnapshot(
        ipo_id=1,
        score_date=date(2026, 1, 1),
        total_score=80,
        confidence_score=90,
        recommendation_band="Blocked",
        model_version="rules-v3-simple",
        demand_score=1,
        structure_score=2,
        valuation_score=3,
        market_condition_score=4,
        explanation_json={
            "pillars": {"demand": {"score": 0, "max_score": 0}, "supply": "bad"},
            "gates": {"hard_block_reasons": ["block"], "review_reasons": ["warn"]},
        },
    )
    v3.missing_features_json = []
    text = format_score(ipo, v3)
    assert "차단 사유: block" in text and "검토 경고: warn" in text
    v3.explanation_json = {"pillars": {}, "gates": {}}
    assert "차단 사유" not in format_score(ipo, v3)

    allocation = AllocationResult(
        ipo_id=1,
        broker_name="증권",
        user_cash=10_000_000,
        offer_price=10_000,
        apply_shares=100,
        expected_equal_shares_low=0,
        expected_equal_shares_mid=1,
        expected_equal_shares_high=2,
        expected_proportional_shares_conservative=0.1,
        expected_proportional_shares_base=0.2,
        expected_proportional_shares_optimistic=0.3,
        expected_total_shares_base=1.2,
        estimated_fee=2_000,
        estimated_required_deposit=500_000,
        assumption_source="test",
        assumption_updated_at=datetime(2026, 1, 1),
    )
    assert "균등 예상" in format_allocation(ipo, allocation)

    news = NewsMetric(
        deduped_article_count=3,
        recent_7d_article_count=None,
        article_growth_rate_7d=None,
        risk_keyword_count=None,
        summary="요약",
    )
    trend = SimpleNamespace(
        recent_avg_index=1, baseline_avg_index=2, trend_growth_rate=0.5, zero_data_ratio=0.1
    )
    assert "요약" in format_trend(ipo, news, trend)
    assert "뉴스 데이터 없음" in format_trend(ipo, None, None)
    news.summary = None
    assert "요약" not in format_trend(ipo, news, trend)

    assert "재무 데이터 없음" in format_financial(ipo, None)
    financial = FinancialMetric(
        fiscal_year=2025,
        revenue=100,
        revenue_yoy_growth=0.1,
        revenue_yoy_status="calculated",
        operating_profit=10,
        operating_margin=0.1,
        net_income=5,
        net_margin=0.05,
        debt_ratio=0.2,
        operating_cash_flow=-1,
        has_negative_ocf=True,
    )
    assert "영업현금흐름 음수" in format_financial(ipo, financial)
    financial.has_negative_ocf = False
    assert "특이사항 없음" in format_financial(ipo, financial)
    financial.revenue_yoy_status = "missing_prior"
    assert _financial_yoy(financial) == "전년도 데이터 없음"

    assert "검증 결과 없음" in format_result(ipo, None)
    evaluation = OutcomeEvaluation(
        ipo_id=1,
        offer_price=10_000,
        prediction_was_correct=False,
    )
    assert "예측 적중: 아니오" in format_result(ipo, evaluation)

    metrics = BandBacktestMetrics(1, 1.0, 2.0, 2.0, 0.0, True)
    report = BacktestReport(
        evaluated_count=1,
        prediction_accuracy=1.0,
        average_open_return_pct=2,
        average_d1_return_pct=3,
        strong_watch_accuracy=1.0,
        avoid_accuracy=None,
        neutral_accuracy=0,
        median_open_return_pct=2,
        median_d1_return_pct=3,
        loss_probability=0,
        band_metrics={"Strong": metrics},
        model_versions=("m",),
        rules_hashes=("h",),
        validation_status="ready",
    )
    assert "소표본" in format_backtest(report)
    metrics.small_sample_warning = False
    assert "소표본" not in format_backtest(report)
    empty_report = BacktestReport(0, None, None, None, None, None)
    assert "모델: -" in format_backtest(empty_report)

    strategy = StrategyBacktestMetrics(1, 1, 1, 1, 1, 1, False)
    gross = GrossOutcomeSummary(1, strategy, strategy)
    blocked = SimpleNamespace(
        validation_ready=False,
        complete_pair_count=0,
        minimum_samples=30,
        blockers=("block",),
    )
    ready = SimpleNamespace(
        validation_ready=True,
        complete_pair_count=30,
        minimum_samples=30,
        blockers=(),
    )
    assert "차단 요인" in format_operational_backtest(
        OperationalBacktest("m", "h" * 64, empty_report, blocked, gross)
    )
    assert "검증 표본" in format_operational_backtest(
        OperationalBacktest("m", "h" * 64, report, ready, gross)
    )
    mismatch = SimpleNamespace(**vars(ready))
    mismatch.validation_ready = True
    report.validation_status = "blocked"
    assert "준비도 gate 미충족" in format_operational_backtest(
        OperationalBacktest("m", "h" * 64, report, mismatch, gross)
    )

    assert _fmt_date(None) == "-"
    assert _date_range(date(2026, 1, 1), date(2026, 1, 2)).startswith("2026")
    assert _date_range(None, date(2026, 1, 2)) == "2026-01-02"
    assert _money(None) == "-" and _money(1) == "1원"
    assert _pct(None) == "-" and _pct(0.1) == "+10.0%" and _pct(1, True) == "+1.0%"
    assert _bool(None) == "-" and _bool(True) == "예" and _bool(False) == "아니오"
    assert _fmt_datetime(None) == "-" and _fmt_datetime(datetime(2026, 1, 1)).startswith("2026")


def test_cli_main_and_simple_commands(monkeypatch, capsys):
    from app import cli

    called = []
    parser = SimpleNamespace(
        parse_args=lambda: SimpleNamespace(command=None),
        print_help=lambda: called.append("help"),
    )
    monkeypatch.setattr(cli, "build_parser", lambda: parser)
    cli.main()
    assert called == ["help"]
    parser.parse_args = lambda: SimpleNamespace(
        command="x", func=lambda args: called.append(args.command)
    )
    cli.main()
    assert called[-1] == "x"

    monkeypatch.setattr(cli, "print_json", lambda value: called.append(value))
    simple_cases = [
        (cli.cmd_seed_sample, "seed_sample_data", SimpleNamespace(), {"seed": 1}),
        (
            cli.cmd_collect_dart,
            "collect_dart_for_ipo",
            SimpleNamespace(name="n"),
            {"dart": 1},
        ),
        (
            cli.cmd_collect_financials,
            "collect_financials_for_ipo",
            SimpleNamespace(name="n", years=2),
            {"financial": 1},
        ),
        (
            cli.cmd_collect_news_trend,
            "collect_news_trend_for_ipo",
            SimpleNamespace(name="n"),
            {"news": 1},
        ),
        (
            cli.cmd_collect_prices,
            "collect_prices_for_ipo",
            SimpleNamespace(name="n"),
            {"price": 1},
        ),
        (
            cli.cmd_resolve_ticker,
            "resolve_ticker_for_ipo",
            SimpleNamespace(name="n"),
            {"ticker": 1},
        ),
        (cli.cmd_collect_market, "collect_market_condition", SimpleNamespace(), {"market": 1}),
        (cli.cmd_refresh_statuses, "refresh_statuses", SimpleNamespace(), {"refresh": 1}),
        (
            cli.cmd_evaluate,
            "evaluate_outcome_for_ipo",
            SimpleNamespace(name="n"),
            {"evaluate": 1},
        ),
        (cli.cmd_status, "latest_job_statuses", SimpleNamespace(), [1]),
        (cli.cmd_send_daily_summary, "send_daily_summary", SimpleNamespace(), {"daily": 1}),
        (
            cli.cmd_send_daily_email,
            "send_daily_email_summary",
            SimpleNamespace(),
            {"email": 1},
        ),
        (
            cli.cmd_send_weekly_email,
            "send_weekly_email_summary",
            SimpleNamespace(),
            {"weekly": 1},
        ),
    ]
    for function, dependency, args, result in simple_cases:
        if dependency == "collect_financials_for_ipo":
            monkeypatch.setattr(cli, dependency, lambda *a, _result=result, **kw: _result)
        else:
            monkeypatch.setattr(cli, dependency, lambda *a, _result=result, **kw: _result)
        function(args)
    assert any(value == {"jobs": [1]} for value in called)

    monkeypatch.setattr(cli, "migrate_db", lambda: called.append("migrate"))
    cli.cmd_init_db(None)
    assert "database migrated" in capsys.readouterr().out


def test_cli_health_schedule_score_review_and_history(monkeypatch, capsys):
    from app import cli

    good = SimpleNamespace(database_ok=True, schema_current=True)
    bad = SimpleNamespace(database_ok=False, schema_current=True)
    monkeypatch.setattr(cli, "format_health", lambda status: str(status.database_ok))
    monkeypatch.setattr(cli, "healthcheck", lambda: good)
    cli.cmd_healthcheck(None)
    monkeypatch.setattr(cli, "healthcheck", lambda: bad)
    with pytest.raises(SystemExit):
        cli.cmd_healthcheck(None)

    outputs = []
    monkeypatch.setattr(cli, "print_json", outputs.append)
    monkeypatch.setattr(cli, "collect_kind_schedule", lambda start, end: (start, end))
    cli.cmd_collect_schedule(SimpleNamespace(from_date="2026-01-01", to_date=None))
    assert outputs[-1] == (date(2026, 1, 1), None)
    monkeypatch.setattr(cli, "local_today", lambda: date(2026, 1, 9))
    monkeypatch.setattr(cli, "backfill_historical_outcomes", lambda start, end: (start, end))
    cli.cmd_backfill_history(SimpleNamespace(from_date="2026-01-01", to_date=None))
    cli.cmd_backfill_history(SimpleNamespace(from_date="2026-01-01", to_date="2026-01-02"))
    assert outputs[-1][1] == date(2026, 1, 2)

    monkeypatch.setattr(cli, "score_all_active", lambda: {"all": 1})
    cli.cmd_score(SimpleNamespace(all=True, name="n"))
    cli.cmd_score(SimpleNamespace(all=False, name=None))
    monkeypatch.setattr(cli, "score_one", lambda name: outputs.append(name))
    monkeypatch.setattr(cli, "session_scope", lambda: Scope())
    ipo = SimpleNamespace(id=1)
    monkeypatch.setattr(cli, "_find_or_exit", lambda *_: ipo)
    monkeypatch.setattr(cli, "latest_score", lambda *_: "score")
    monkeypatch.setattr(cli, "format_score", lambda *_: "formatted")
    cli.cmd_score(SimpleNamespace(all=False, name="n"))
    assert "formatted" in capsys.readouterr().out

    monkeypatch.setattr(cli, "review_diligence", lambda **kwargs: kwargs)
    cli.cmd_review_diligence(SimpleNamespace(name="n", min_news_articles=2, max_age_days=3))
    monkeypatch.setattr(cli, "mark_diligence_review", lambda *args, **kwargs: (args, kwargs))
    cli.cmd_mark_reviewed(
        SimpleNamespace(name="n", review_type="articles", reviewed_by="u", note="x", evidence=["e"])
    )
    monkeypatch.setattr(cli, "record_allocation_actual", lambda *args, **kwargs: (args, kwargs))
    cli.cmd_record_allocation_actual(
        SimpleNamespace(name="n", broker="b", applicants=1, competition=2, source="s")
    )


def test_cli_database_commands_and_errors(monkeypatch, capsys, tmp_path):
    from app import cli

    monkeypatch.setattr(cli, "init_db", lambda: None)
    monkeypatch.setattr(cli, "session_scope", lambda: Scope(SimpleNamespace(get=lambda *_: None)))
    with pytest.raises(SystemExit, match="Participation not found"):
        cli.cmd_record_exit(
            SimpleNamespace(
                participation_id=1,
                shares=1,
                price="1",
                strategy="M",
                cost=0,
                sold_at=None,
                note="",
            )
        )
    participation = object()
    session = SimpleNamespace(get=lambda *_: participation)
    monkeypatch.setattr(cli, "session_scope", lambda: Scope(session))
    monkeypatch.setattr(cli, "record_exit", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        cli, "summarize_participation", lambda row: SimpleNamespace(to_dict=lambda: {"id": 1})
    )
    cli.cmd_record_exit(
        SimpleNamespace(
            participation_id=1,
            shares=1,
            price="1만원",
            strategy="M",
            cost=0,
            sold_at="2026-01-01T00:00:00Z",
            note="",
        )
    )

    ipo = SimpleNamespace(id=9)
    monkeypatch.setattr(cli, "_find_or_exit", lambda *_: ipo)
    monkeypatch.setattr(cli, "list_participation_summaries", lambda *_a, **_k: [])
    summary = SimpleNamespace(to_dict=lambda: {"total": 0})
    monkeypatch.setattr(cli, "summarize_participation_portfolio", lambda rows: summary)
    cli.cmd_participation_summary(SimpleNamespace(name="n"))
    cli.cmd_participation_summary(SimpleNamespace(name=None))

    monkeypatch.setattr(cli, "local_today", lambda: date(2026, 1, 1))
    monkeypatch.setattr(cli, "list_ipos_between", lambda *_: [ipo])
    monkeypatch.setattr(cli, "format_schedule", lambda *args: "schedule")
    cli.cmd_today(SimpleNamespace(days=1))
    cli.cmd_week(None)
    monkeypatch.setattr(cli, "format_ipo_summary", lambda *_: "ipo")
    cli.cmd_ipo(SimpleNamespace(name="n"))

    monkeypatch.setattr(cli, "resolve_underwriter", lambda *_: "uw")
    monkeypatch.setattr(cli, "derive_allocation_assumptions", lambda *_: "assumptions")
    monkeypatch.setattr(cli, "build_allocation_scenario", lambda *_a, **_k: "result")
    monkeypatch.setattr(cli, "save_allocation_result", lambda *_: None)
    monkeypatch.setattr(cli, "format_allocation", lambda *_: "allocation")
    cli.cmd_calc(SimpleNamespace(name="n", amount="1만원", broker=None))
    monkeypatch.setattr(
        cli,
        "resolve_underwriter",
        lambda *_: (_ for _ in ()).throw(ValueError("uw")),
    )
    with pytest.raises(SystemExit, match="uw"):
        cli.cmd_calc(SimpleNamespace(name="n", amount="1", broker=None))

    monkeypatch.setattr(cli, "latest_news_metric", lambda *_: "news")
    monkeypatch.setattr(cli, "latest_search_trend", lambda *_: "trend")
    monkeypatch.setattr(cli, "format_trend", lambda *_: "trend-text")
    cli.cmd_trend(SimpleNamespace(name="n"))
    monkeypatch.setattr(cli, "latest_financial_metric", lambda *_: "financial")
    monkeypatch.setattr(cli, "format_financial", lambda *_: "financial-text")
    cli.cmd_financial(SimpleNamespace(name="n"))
    monkeypatch.setattr(cli, "latest_outcome", lambda *_: "outcome")
    monkeypatch.setattr(cli, "format_result", lambda *_: "result-text")
    cli.cmd_result(SimpleNamespace(name="n"))

    readiness = SimpleNamespace(to_dict=lambda: {"ready": False})
    monkeypatch.setattr(cli, "resolve_scoring_identity", lambda *_: ("m", "h"))
    monkeypatch.setattr(cli, "assess_data_readiness", lambda *_a, **_k: readiness)
    monkeypatch.setattr(cli, "build_validation_dataset", lambda *_a, **_k: [1])
    monkeypatch.setattr(cli, "write_validation_csv", lambda rows, path: path)
    cli.cmd_validation_report(
        SimpleNamespace(model_version=None, rules_hash=None, minimum_samples=1, csv_path=None)
    )
    cli.cmd_validation_report(
        SimpleNamespace(
            model_version="m", rules_hash="h", minimum_samples=1, csv_path=str(tmp_path / "x.csv")
        )
    )
    monkeypatch.setattr(
        cli, "resolve_scoring_identity", lambda *_: (_ for _ in ()).throw(ValueError("identity"))
    )
    with pytest.raises(SystemExit, match="identity"):
        cli.cmd_validation_report(
            SimpleNamespace(model_version=None, rules_hash=None, minimum_samples=1, csv_path=None)
        )
    assert "schedule" in capsys.readouterr().out


def test_cli_preview_send_and_helpers(monkeypatch, capsys):
    from app import cli

    daily = (SimpleNamespace(text="daily-text", html="daily-html"), 1)
    weekly = (SimpleNamespace(text="weekly-text", html="weekly-html"), 1)
    monkeypatch.setattr(cli, "build_daily_email_content", lambda: daily)
    monkeypatch.setattr(cli, "build_weekly_email_content", lambda: weekly)
    for weekly_flag, html in ((False, False), (False, True), (True, False), (True, True)):
        cli.cmd_preview_email(SimpleNamespace(weekly=weekly_flag, html=html))
    output = capsys.readouterr().out
    assert "daily-text" in output and "weekly-html" in output

    captured = []
    monkeypatch.setattr(cli, "print_json", captured.append)
    monkeypatch.setattr(cli, "send_email_message", lambda *_a, **_k: {"sent": True})
    cli.cmd_send_test_email(SimpleNamespace(strict=False))
    monkeypatch.setattr(
        cli, "send_email_message", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("email"))
    )
    cli.cmd_send_test_email(SimpleNamespace(strict=False))
    with pytest.raises(RuntimeError, match="email"):
        cli.cmd_send_test_email(SimpleNamespace(strict=True))

    monkeypatch.setattr(cli, "send_telegram_message", lambda *_: {"sent": True})
    cli.cmd_send_test_telegram(SimpleNamespace(strict=False))
    monkeypatch.setattr(
        cli, "send_telegram_message", lambda *_: (_ for _ in ()).throw(RuntimeError("telegram"))
    )
    cli.cmd_send_test_telegram(SimpleNamespace(strict=False))
    with pytest.raises(RuntimeError, match="telegram"):
        cli.cmd_send_test_telegram(SimpleNamespace(strict=True))

    calls = []
    monkeypatch.setattr(cli, "init_db", lambda: calls.append("init"))
    monkeypatch.setattr(cli, "run_bot", lambda: calls.append("bot"))
    monkeypatch.setattr(cli, "run_scheduler", lambda: calls.append("scheduler"))
    cli.cmd_run_bot(None)
    cli.cmd_run_scheduler(None)
    assert calls == ["init", "bot", "scheduler"]

    monkeypatch.setattr(cli, "get_ipo_or_raise", lambda *_: "ipo")
    assert cli._find_or_exit("s", "n") == "ipo"
    monkeypatch.setattr(
        cli, "get_ipo_or_raise", lambda *_: (_ for _ in ()).throw(ValueError("missing"))
    )
    with pytest.raises(SystemExit, match="missing"):
        cli._find_or_exit("s", "n")
    assert cli._parse_date(None) is None
    assert cli._parse_date("2026-01-01") == date(2026, 1, 1)
    assert cli._parse_amount("100만원") == 1_000_000
    assert cli._parse_amount("1억") == 100_000_000
    with pytest.raises(SystemExit, match="Invalid amount"):
        cli._parse_amount("bad")
    with pytest.raises(SystemExit, match="greater than 0"):
        cli._parse_amount("0")
    cli.print_json({"when": date(2026, 1, 1)})


@pytest.mark.filterwarnings("ignore:'app.cli' found in sys.modules:RuntimeWarning")
def test_cli_module_entrypoint(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["app.cli"])
    runpy.run_module("app.cli", run_name="__main__")


class FakeScalars:
    def __init__(self, values):
        self.values = list(values)

    def first(self):
        return self.values[0] if self.values else None

    def __iter__(self):
        return iter(self.values)


class FakeExecution:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return FakeScalars(self.values)


class FakeSession:
    def __init__(self, executions=()):
        self.executions = list(executions)

    def execute(self, _query):
        return FakeExecution(self.executions.pop(0))


def test_telegram_send_and_cooldown(monkeypatch):
    from app.services import telegram

    def settings(token="token", chat="chat"):
        return SimpleNamespace(
            telegram_bot_token=token,
            telegram_chat_id=chat,
            http_timeout_sec=1,
        )

    monkeypatch.setattr(telegram, "get_settings", lambda: settings(token=None))
    with pytest.raises(ValueError, match="BOT_TOKEN"):
        telegram.send_telegram_message("x")
    monkeypatch.setattr(telegram, "get_settings", lambda: settings(chat=None))
    with pytest.raises(ValueError, match="CHAT_ID"):
        telegram.send_telegram_message("x")

    event = SimpleNamespace(id=1, status="sent")
    monkeypatch.setattr(telegram, "get_settings", lambda: settings())
    monkeypatch.setattr(telegram, "reserve_notification", lambda *_a, **_k: (event, False))
    assert telegram.send_telegram_message("x", event_key="key")["duplicate"] is True

    responses = [{"ok": True}]
    posts = []

    class Client:
        def __init__(self, **kwargs):
            posts.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post_json(self, *args, **kwargs):
            posts.append((args, kwargs))
            result = responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

    monkeypatch.setattr(telegram, "ResilientHttpClient", Client)
    assert telegram.send_telegram_message("x", chat_id="explicit")["chunks"] == 1

    finished = []
    event.status = "failed"
    monkeypatch.setattr(telegram, "reserve_notification", lambda *_a, **_k: (event, True))
    monkeypatch.setattr(telegram, "finish_notification", lambda *a, **k: finished.append((a, k)))
    responses[:] = [{"ok": True}]
    assert telegram.send_telegram_message("x", event_key="key")["ok"]
    assert finished[-1][1]["status"] == "sent"
    responses[:] = [{"ok": False}]
    with pytest.raises(ValueError, match="rejected"):
        telegram.send_telegram_message("x", event_key="key")
    assert finished[-1][1]["status"] == "failed"
    responses[:] = [RuntimeError("post")]
    with pytest.raises(RuntimeError, match="post"):
        telegram.send_telegram_message("x")

    with pytest.raises(ValueError, match="positive"):
        telegram.acquire_refresh_cooldown(1, cooldown_seconds=0)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(
        telegram,
        "reserve_notification",
        lambda *_a, **_k: (SimpleNamespace(lease_until=None), False),
    )
    assert telegram.acquire_refresh_cooldown(1, now=now, cooldown_seconds=5) == 5
    monkeypatch.setattr(
        telegram,
        "reserve_notification",
        lambda *_a, **_k: (SimpleNamespace(lease_until=datetime(2026, 1, 1)), False),
    )
    assert telegram.acquire_refresh_cooldown(1, now=now) == 1


def test_telegram_text_helpers(monkeypatch):
    from app.services import telegram

    ipo = SimpleNamespace(id=1, corp_name="기업", underwriters=[])
    monkeypatch.setattr(telegram, "session_scope", lambda: Scope(FakeSession()))
    monkeypatch.setattr(telegram, "list_ipos_between", lambda *_: [ipo])
    monkeypatch.setattr(telegram, "format_schedule", lambda *_: "schedule")
    assert telegram._schedule_text("t", date.min, date.max) == "schedule"

    monkeypatch.setattr(telegram, "_find_or_raise", lambda *_: ipo)
    monkeypatch.setattr(telegram, "format_ipo_summary", lambda *_: "ipo")
    assert telegram._ipo_text("n") == "ipo"
    monkeypatch.setattr(telegram, "latest_score", lambda *_: "score")
    monkeypatch.setattr(telegram, "format_score", lambda *_: "score-text")
    assert telegram._score_text("n") == "score-text"
    monkeypatch.setattr(telegram, "resolve_underwriter", lambda *_: "uw")
    monkeypatch.setattr(telegram, "derive_allocation_assumptions", lambda *_: "assumptions")
    monkeypatch.setattr(telegram, "build_allocation_scenario", lambda *_a, **_k: "result")
    monkeypatch.setattr(telegram, "save_allocation_result", lambda *_: None)
    monkeypatch.setattr(telegram, "format_allocation", lambda *_: "allocation")
    assert telegram._allocation_text("n", 1, None) == "allocation"

    session = FakeSession([["news"], ["trend"]])
    monkeypatch.setattr(telegram, "session_scope", lambda: Scope(session))
    monkeypatch.setattr(telegram, "format_trend", lambda *_: "trend-text")
    assert telegram._trend_text("n") == "trend-text"
    session = FakeSession([["financial"]])
    monkeypatch.setattr(telegram, "session_scope", lambda: Scope(session))
    monkeypatch.setattr(telegram, "format_financial", lambda *_: "financial-text")
    assert telegram._financial_text("n") == "financial-text"
    session = FakeSession([["result"]])
    monkeypatch.setattr(telegram, "session_scope", lambda: Scope(session))
    monkeypatch.setattr(telegram, "format_result", lambda *_: "result-text")
    assert telegram._result_text("n") == "result-text"

    monkeypatch.setattr(telegram, "session_scope", lambda: Scope("s"))
    monkeypatch.setattr(telegram, "build_operational_backtest", lambda *_: "bundle")
    monkeypatch.setattr(telegram, "format_operational_backtest", lambda *_: "backtest")
    assert telegram._backtest_text() == "backtest"

    monkeypatch.setattr(telegram, "latest_job_statuses", lambda: [])
    assert "이력이 없습니다" in telegram._status_text()
    monkeypatch.setattr(
        telegram,
        "latest_job_statuses",
        lambda: [
            {
                "job_name": "job",
                "status": "done",
                "finished_at": None,
                "success_count": 1,
                "partial_count": 2,
                "skipped_count": 3,
                "failure_count": 4,
                "error_summary": None,
            },
            {
                "job_name": "bad",
                "status": "failed",
                "finished_at": datetime(2026, 1, 1),
                "success_count": 0,
                "partial_count": 0,
                "skipped_count": 0,
                "failure_count": 1,
                "error_summary": "error",
            },
        ],
    )
    monkeypatch.setattr(telegram, "format_local_datetime", lambda *_: "finished")
    status = telegram._status_text()
    assert "실행 중" in status and "오류: error" in status


def test_telegram_sources_why_refresh_and_parsers(monkeypatch):
    from app.services import telegram

    actual_find_or_raise = telegram._find_or_raise
    ipo = SimpleNamespace(id=1, corp_name="기업")
    monkeypatch.setattr(telegram, "_find_or_raise", lambda *_: ipo)
    monkeypatch.setattr(telegram, "latest_score", lambda *_: None)
    monkeypatch.setattr(telegram, "session_scope", lambda: Scope(FakeSession([[], []])))
    assert "저장된 출처가 없습니다" in telegram._sources_text("n")

    snapshot = SimpleNamespace(source="kind", source_key="key", fetched_at=datetime(2026, 1, 1))
    review = SimpleNamespace(
        review_type="articles", review_status="reviewed", reviewed_at=datetime(2026, 1, 1)
    )
    score = SimpleNamespace(model_version="m", rules_hash=None)
    monkeypatch.setattr(telegram, "latest_score", lambda *_: score)
    monkeypatch.setattr(
        telegram, "session_scope", lambda: Scope(FakeSession([[snapshot], [review]]))
    )
    monkeypatch.setattr(telegram, "format_local_datetime", lambda *_: "when")
    sources = telegram._sources_text("n")
    assert "review/articles" in sources and "rules -" in sources

    monkeypatch.setattr(telegram, "session_scope", lambda: Scope("session"))
    monkeypatch.setattr(telegram, "latest_score", lambda *_: None)
    assert "스냅샷이 없습니다" in telegram._why_text("n")
    score = SimpleNamespace(
        demand_score=1,
        financial_score=None,
        valuation_score=3,
        structure_score=4,
        news_trend_score=5,
        allocation_score=6,
        market_condition_score=7,
        total_score=80,
        recommendation_band="Strong",
        confidence_score=90,
        risk_penalty_score=None,
        missing_features_json=[],
    )
    monkeypatch.setattr(telegram, "latest_score", lambda *_: score)
    assert "누락 데이터: 없음" in telegram._why_text("n")
    score.missing_features_json = ["x"]
    assert "누락 데이터: x" in telegram._why_text("n")

    tracked = {
        "ipo": "기업",
        "results": [
            {"label": "a", "status": "success", "detail": None},
            {"label": "b", "status": "partial", "detail": "p"},
            {"label": "c", "status": "skipped", "detail": None},
            {"label": "d", "status": "failed", "detail": None},
        ],
    }
    monkeypatch.setattr(telegram, "run_tracked_job", lambda *_: tracked)
    refresh = telegram._refresh_text("기업")
    assert "부분 성공 · p" in refresh and "건너뜀" in refresh

    assert telegram._refresh_result_detail({"errors": ["a", "b"]}) == "a; b"
    assert telegram._refresh_result_detail({"error": "x"}) == "x"
    assert telegram._refresh_result_detail({}) is None
    assert telegram._required_name(SimpleNamespace(args=[" a ", "b"]), "usage") == "a  b"
    with pytest.raises(ValueError, match="사용법"):
        telegram._required_name(SimpleNamespace(args=[]), "usage")

    monkeypatch.setattr(telegram, "get_ipo_or_raise", lambda *_: ipo)
    assert actual_find_or_raise("s", "n") is ipo
    monkeypatch.setattr(
        telegram,
        "get_ipo_or_raise",
        lambda *_: (_ for _ in ()).throw(ValueError("IPO not found: n")),
    )
    with pytest.raises(ValueError, match="IPO를 찾지 못했습니다"):
        actual_find_or_raise("s", "n")
    monkeypatch.setattr(
        telegram,
        "get_ipo_or_raise",
        lambda *_: (_ for _ in ()).throw(ValueError("IPO name is ambiguous: n")),
    )
    with pytest.raises(ValueError, match="같은 이름의 IPO"):
        actual_find_or_raise("s", "n")

    monkeypatch.setattr(telegram, "session_scope", lambda: Scope("session"))
    monkeypatch.setattr(telegram, "_find_or_raise", lambda *_: ipo)
    assert telegram._validated_ipo_name("n") == "기업"
    assert telegram._looks_like_amount("1만원")
    assert not telegram._looks_like_amount("bad")


def test_telegram_reply_and_split_extra_paths():
    from app.services import telegram

    assert telegram.split_telegram_message("a\nb", 2) == ["a", "b"]
    assert telegram.split_telegram_message("abc\ndef", 4) == ["abc", "def"]
    assert telegram.split_telegram_message("abcdefgh", 3) == ["abc", "def", "gh"]
    assert telegram.split_telegram_message("abcdef", 3) == ["abc", "def"]

    replies = []

    async def async_reply_text(text, **kwargs):
        replies.append((text, kwargs))

    message = SimpleNamespace(reply_text=async_reply_text)
    asyncio.run(telegram._reply(SimpleNamespace(effective_message=message), "hello"))
    asyncio.run(telegram._reply(SimpleNamespace(effective_message=None), "hello"))
    assert replies[0][0] == "hello"


def test_telegram_run_bot_handlers_are_fully_isolated(monkeypatch):
    from app.services import telegram

    class FakeUpdate:
        def __init__(self, *, chat_id="allowed", user_id="admin", message=True):
            self.effective_chat = SimpleNamespace(id=chat_id) if chat_id is not None else None
            self.effective_user = SimpleNamespace(id=user_id) if user_id is not None else None
            self.effective_message = FakeMessage() if message else None

    class FakeMessage:
        def __init__(self):
            self.replies = []

        async def reply_text(self, text, **_kwargs):
            self.replies.append(text)

    class FakeBot:
        def __init__(self):
            self.commands = None

        async def set_my_commands(self, commands):
            self.commands = commands

    class FakeApp:
        def __init__(self, post_init):
            self.handlers = {}
            self.error_handler = None
            self.bot = FakeBot()
            self.post_init = post_init
            self.polled = False

        def add_handler(self, handler):
            self.handlers[handler.command] = handler.callback

        def add_error_handler(self, handler):
            self.error_handler = handler

        def run_polling(self, **kwargs):
            self.polled = kwargs

    class Builder:
        def token(self, value):
            self.token_value = value
            return self

        def post_init(self, value):
            self.post_init_value = value
            return self

        def build(self):
            app = FakeApp(self.post_init_value)
            applications.append(app)
            return app

    class FakeApplication:
        @classmethod
        def builder(cls):
            return Builder()

    class FakeCommandHandler:
        def __init__(self, command, callback):
            self.command = command
            self.callback = callback

    telegram_module = ModuleType("telegram")
    telegram_module.Update = FakeUpdate
    ext_module = ModuleType("telegram.ext")
    ext_module.Application = FakeApplication
    ext_module.CommandHandler = FakeCommandHandler
    ext_module.ContextTypes = SimpleNamespace(DEFAULT_TYPE=object)
    monkeypatch.setitem(sys.modules, "telegram", telegram_module)
    monkeypatch.setitem(sys.modules, "telegram.ext", ext_module)

    settings = SimpleNamespace(
        telegram_bot_token="token",
        allowed_telegram_chat_ids=("allowed",),
        allowed_telegram_user_ids=("admin",),
    )
    monkeypatch.setattr(telegram, "get_settings", lambda: settings)
    applications = []
    telegram.run_bot()
    app = applications[0]
    assert app.polled == {"drop_pending_updates": False}

    asyncio.run(app.post_init(app))
    assert app.bot.commands == telegram.TELEGRAM_COMMANDS

    async def immediate(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(telegram.asyncio, "to_thread", immediate)
    monkeypatch.setattr(telegram, "local_today", lambda: date(2026, 1, 1))
    monkeypatch.setattr(telegram, "_schedule_text", lambda *_: "schedule")
    monkeypatch.setattr(telegram, "_ipo_text", lambda *_: "ipo")
    monkeypatch.setattr(telegram, "_score_text", lambda *_: "score")
    monkeypatch.setattr(telegram, "_allocation_text", lambda *_: "allocation")
    monkeypatch.setattr(telegram, "_trend_text", lambda *_: "trend")
    monkeypatch.setattr(telegram, "_financial_text", lambda *_: "financial")
    monkeypatch.setattr(telegram, "_result_text", lambda *_: "result")
    monkeypatch.setattr(telegram, "_backtest_text", lambda: "backtest")
    monkeypatch.setattr(telegram, "_status_text", lambda: "status")
    monkeypatch.setattr(telegram, "_sources_text", lambda *_: "sources")
    monkeypatch.setattr(telegram, "_why_text", lambda *_: "why")
    monkeypatch.setattr(telegram, "_validated_ipo_name", lambda name: name)
    monkeypatch.setattr(telegram, "acquire_refresh_cooldown", lambda *_: 0)
    monkeypatch.setattr(telegram, "_refresh_text", lambda *_: "refresh")

    def run(command, args=(), **update_kwargs):
        update = FakeUpdate(**update_kwargs)
        context = SimpleNamespace(args=list(args), error=None)
        asyncio.run(app.handlers[command](update, context))
        return update

    for command, args in (
        ("help", ()),
        ("today", ()),
        ("week", ()),
        ("ipo", ("name",)),
        ("score", ("name",)),
        ("calc", ("name", "1만원", "broker")),
        ("trend", ("name",)),
        ("financial", ("name",)),
        ("result", ("name",)),
        ("backtest", ()),
        ("status", ()),
        ("sources", ("name",)),
        ("why", ("name",)),
        ("refresh", ("name",)),
    ):
        assert run(command, args).effective_message.replies

    unauthorized = run("help", chat_id="denied")
    assert "허용되지 않은 요청입니다." in unauthorized.effective_message.replies
    for command in (
        "today",
        "week",
        "ipo",
        "score",
        "calc",
        "trend",
        "financial",
        "result",
        "backtest",
        "status",
        "sources",
        "why",
        "refresh",
    ):
        run(command, chat_id="denied")
    run("help", chat_id=None, message=False)
    with pytest.raises(ValueError, match="사용법"):
        run("calc", ("only",))
    with pytest.raises(ValueError, match="사용법"):
        run("calc", (" ", "1만원"))
    with pytest.raises(ValueError, match="권한"):
        run("refresh", ("name",), user_id=None)
    with pytest.raises(ValueError, match="권한"):
        run("refresh", ("name",), user_id="other")
    monkeypatch.setattr(telegram, "acquire_refresh_cooldown", lambda *_: 2)
    with pytest.raises(ValueError, match="2초"):
        run("refresh", ("name",))

    update = FakeUpdate()
    asyncio.run(app.error_handler(update, SimpleNamespace(error=ValueError("safe"))))
    asyncio.run(app.error_handler(update, SimpleNamespace(error=RuntimeError("secret"))))
    asyncio.run(app.error_handler(object(), SimpleNamespace(error=RuntimeError("ignored"))))
    assert "safe" in update.effective_message.replies
    assert "요청 처리 중 오류가 발생했습니다." in update.effective_message.replies

    settings.telegram_bot_token = None
    with pytest.raises(ValueError, match="BOT_TOKEN"):
        telegram.run_bot()
    settings.telegram_bot_token = "token"
    settings.allowed_telegram_chat_ids = ()
    with pytest.raises(ValueError, match="ALLOWED_CHAT_IDS"):
        telegram.run_bot()
