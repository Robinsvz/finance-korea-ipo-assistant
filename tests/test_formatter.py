from datetime import date
from types import SimpleNamespace

import pytest

from app.models import FinancialMetric, Ipo, ScoreSnapshot
from app.schemas import HealthStatus
from app.services.backtest import (
    BacktestReport,
    GrossOutcomeSummary,
    OperationalBacktest,
    _strategy_metrics,
)
from app.services.formatter import (
    format_financial,
    format_health,
    format_operational_backtest,
    format_schedule,
    format_score,
)


def test_financial_formatter_explains_prior_zero_yoy():
    ipo = Ipo(corp_name="재무기업", normalized_name="재무기업")
    metric = FinancialMetric(
        fiscal_year=2025,
        revenue=100,
        revenue_yoy_growth=None,
        revenue_yoy_status="prior_zero",
    )
    assert "전년도 매출 0으로 산정 불가" in format_financial(ipo, metric)


def test_schedule_includes_refund_and_listing_events():
    ipo = Ipo(
        corp_name="일정기업",
        normalized_name="일정기업",
        refund_date=date(2026, 7, 20),
        listing_date=date(2026, 7, 24),
    )
    text = format_schedule("이번 주", [ipo])
    assert "2026-07-20 · 환불" in text
    assert "2026-07-24 · 상장" in text


def test_today_schedule_excludes_other_dates_for_same_ipo():
    ipo = Ipo(
        corp_name="오늘기업",
        normalized_name="오늘기업",
        refund_date=date(2026, 7, 20),
        listing_date=date(2026, 7, 24),
    )
    text = format_schedule("오늘", [ipo], date(2026, 7, 20), date(2026, 7, 20))
    assert "환불" in text
    assert "상장" not in text


@pytest.mark.parametrize(
    ("email_configured", "expected"),
    [(True, "email: configured"), (False, "email: missing")],
)
def test_health_formatter_reports_email_configuration(email_configured, expected):
    status = HealthStatus(
        database_ok=True,
        schema_current=True,
        schema_revision="head",
        expected_schema_revision="head",
        telegram_configured=False,
        email_configured=email_configured,
        dart_configured=False,
        naver_configured=False,
        database_url="sqlite:///safe.db",
    )

    assert expected in format_health(status)


def test_v3_score_formatter_shows_four_pillars_and_gates():
    ipo = Ipo(corp_name="단순점수", normalized_name="단순점수", offer_price_final=10_000)
    score = ScoreSnapshot(
        ipo_id=1,
        score_date=date(2026, 7, 19),
        total_score=80,
        confidence_score=100,
        recommendation_band="Strong",
        model_version="rules-v3-simple",
        demand_score=40,
        structure_score=25,
        valuation_score=10,
        market_condition_score=5,
        explanation_json={
            "pillars": {"demand": {"score": 41, "max_score": 45}},
            "gates": {
                "status": "pass",
                "review_warnings": ["사람 검토"],
                "blocked_reasons": [],
            },
        },
    )

    text = format_score(ipo, score)

    assert "4대 판단축" in text
    assert "기관 수요: 41.0/45" in text
    assert "재무·뉴스·예상 배정은 총점과 분리" in text
    assert "검토 경고: 사람 검토" in text


def _operational_bundle(*, ready: bool) -> OperationalBacktest:
    report = BacktestReport(
        evaluated_count=30 if ready else 0,
        prediction_accuracy=0.7 if ready else None,
        average_open_return_pct=10 if ready else None,
        average_d1_return_pct=5 if ready else None,
        strong_watch_accuracy=0.8 if ready else None,
        avoid_accuracy=0.6 if ready else None,
        median_d1_return_pct=4 if ready else None,
        loss_probability=0.3 if ready else None,
        validation_status="ready" if ready else "insufficient-sample",
    )
    readiness = SimpleNamespace(
        validation_ready=ready,
        complete_pair_count=30 if ready else 0,
        minimum_samples=30,
        blockers=() if ready else ("과거 수요예측 피처 커버리지가 부족합니다.",),
    )
    gross = GrossOutcomeSummary(
        ipo_count=26,
        d0_open=_strategy_metrics([10, -5]),
        d1_close=_strategy_metrics([20, -10]),
    )
    return OperationalBacktest(
        model_version="rules-v3-simple",
        rules_hash="a" * 64,
        validation=report,
        readiness=readiness,
        gross_outcomes=gross,
    )


def test_operational_backtest_separates_gross_outcomes_when_validation_is_blocked():
    text = format_operational_backtest(_operational_bundle(ready=False))

    assert "운영 모델: rules-v3-simple" in text
    assert "적격 점수-D0/D1 쌍: 0/30" in text
    assert "Gross 기술통계 — 모델 성과 아님" in text
    assert "실제 IPO 성과 레코드: 26" in text
    assert "준비도 gate 미충족" in text
    assert "검증 표본: 26" not in text
    assert "방향성 적중률" not in text


def test_operational_backtest_exposes_model_metrics_only_after_readiness_gate():
    text = format_operational_backtest(_operational_bundle(ready=True))

    assert "검증 준비: 완료" in text
    assert "검증 표본: 30" in text
    assert "방향성 적중률: +70.0%" in text
    assert "D1 평균/중앙: +5.0% / +4.0%" in text
