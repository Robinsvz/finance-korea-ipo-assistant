from datetime import date, datetime, timedelta, timezone

import pytest

from app.models import (
    DemandForecast,
    FinancialMetric,
    Ipo,
    IpoStructureMetric,
    MarketConditionSnapshot,
    NewsMetric,
    ScoreSnapshot,
    SearchTrend,
    UnderwriterAllocation,
    ValuationMetric,
)
from app.services import scoring
from app.services.scoring import (
    IpoScoringService,
    load_scoring_rules,
    recommendation_band,
    score_market_condition,
    score_news_trend,
    score_risk_penalty,
    score_valuation,
)


def test_rules_weights_sum_to_100():
    assert sum(load_scoring_rules().weights.values()) == 100


def test_invalid_weight_configuration_is_rejected(monkeypatch):
    config = {
        "model_version": "bad",
        "score_weights": {
            "demand": 1,
            "financial": 1,
            "valuation": 1,
            "structure": 1,
            "news_trend": 1,
            "allocation": 1,
            "market_condition": 1,
            "max_risk_penalty": 20,
        },
        "recommendation_band": {"strong": 80, "watch": 65, "neutral": 50},
    }
    monkeypatch.setattr(scoring, "load_yaml_config", lambda _name: config)
    with pytest.raises(ValueError, match="sum to 100"):
        load_scoring_rules()


def test_empty_data_is_insufficient_not_avoid(db_session):
    ipo = Ipo(corp_name="빈기업", normalized_name="빈기업", status="scheduled")
    db_session.add(ipo)
    db_session.flush()
    result = IpoScoringService().score(db_session, ipo)
    assert result.recommendation_band == "Insufficient Data"
    assert result.confidence_score == 0
    assert result.total_score == 0


def test_yaml_band_change_controls_recommendation(monkeypatch):
    config = {
        "model_version": "custom",
        "score_weights": {
            "demand": 25,
            "financial": 15,
            "valuation": 15,
            "structure": 15,
            "news_trend": 10,
            "allocation": 10,
            "market_condition": 10,
            "max_risk_penalty": 20,
        },
        "confidence": {"minimum_for_recommendation": 70},
        "recommendation_band": {"strong": 90, "watch": 70, "neutral": 40},
        "market_condition": {},
    }
    monkeypatch.setattr(scoring, "load_yaml_config", lambda _name: config)
    rules = load_scoring_rules()
    assert recommendation_band(80, rules) == "Watch"


def test_score_snapshot_upsert_is_idempotent(db_session):
    ipo = Ipo(corp_name="기업", normalized_name="기업", status="scheduled")
    db_session.add(ipo)
    db_session.flush()
    service = IpoScoringService()
    first = service.score_and_save(
        db_session,
        ipo,
        date(2026, 7, 18),
        calculated_at=datetime(2026, 7, 18, 1, tzinfo=timezone.utc),
    )
    db_session.flush()
    original_calculated_at = first.calculated_at
    original_updated_at = first.updated_at
    second = service.score_and_save(
        db_session,
        ipo,
        date(2026, 7, 18),
        calculated_at=datetime(2026, 7, 18, 2, tzinfo=timezone.utc),
    )
    db_session.flush()
    assert first.id == second.id
    assert len(ipo.scores) == 1
    assert second.calculated_at == original_calculated_at
    assert second.updated_at == original_updated_at
    ipo.offer_price_final = 10_000
    third = service.score_and_save(
        db_session,
        ipo,
        date(2026, 7, 18),
        calculated_at=datetime(2026, 7, 18, 3, tzinfo=timezone.utc),
    )
    db_session.flush()
    assert third.id != first.id
    assert db_session.query(ScoreSnapshot).filter(ScoreSnapshot.ipo_id == ipo.id).count() == 2
    assert third.calculated_at != original_calculated_at


def test_news_risk_is_not_subtracted_inside_news_score():
    clean = NewsMetric(
        article_growth_rate_7d=1,
        major_media_count=3,
        positive_keyword_count=1,
        risk_keyword_count=0,
    )
    risky = NewsMetric(
        article_growth_rate_7d=1,
        major_media_count=3,
        positive_keyword_count=1,
        risk_keyword_count=5,
    )
    clean_score, _ = score_news_trend(clean, None)
    risky_score, _ = score_news_trend(risky, None)
    penalty, _ = score_risk_penalty(None, None, risky)
    assert clean_score == risky_score
    assert penalty == 5


def test_valuation_can_reach_full_15_points():
    metric = ValuationMetric(
        valuation_discount_premium_ratio=-0.3,
        per=10,
        peer_median_per=20,
        psr=1,
        peer_median_psr=2,
        valuation_comment="충분한 근거",
    )
    assert score_valuation(metric)[0] == 15


def test_market_condition_can_reach_full_score():
    market = MarketConditionSnapshot(
        as_of_date=date(2026, 7, 18),
        kosdaq_momentum_20d=0.1,
        recent_ipo_success_rate=0.7,
        volatility_20d=0.1,
    )
    assert score_market_condition(market, load_scoring_rules().market)[0] == 10


def test_market_condition_negative_case_is_bounded():
    market = MarketConditionSnapshot(
        as_of_date=date(2026, 7, 18),
        kosdaq_momentum_20d=-0.2,
        recent_ipo_success_rate=0.1,
        volatility_20d=0.8,
    )
    score, _ = score_market_condition(market, load_scoring_rules().market)
    assert score == 0


def test_full_feature_set_can_reach_100(db_session):
    today = date(2026, 7, 18)
    ipo = Ipo(
        corp_name="만점기업",
        normalized_name="만점기업",
        status="scheduled",
        offer_price_final=1_000,
        subscription_end_date=today,
        refund_date=today + timedelta(days=1),
    )
    db_session.add(ipo)
    db_session.flush()
    for name in ("한국투자증권", "NH투자증권"):
        ipo.underwriters.append(
            UnderwriterAllocation(
                broker_name=name,
                retail_allocated_shares=1_000_000,
                equal_allocation_ratio=0.5,
                proportional_allocation_ratio=0.5,
                deposit_rate=0.5,
                subscription_fee=0,
                max_subscription_shares=100_000,
            )
        )
    db_session.add_all(
        [
            DemandForecast(
                ipo_id=ipo.id,
                receipt_no="R1",
                institutional_competition_ratio=2_000,
                lockup_commitment_ratio=0.4,
                final_price_position="above_band",
                participants_count=2_000,
                above_band_participation_ratio=0.8,
            ),
            FinancialMetric(
                ipo_id=ipo.id,
                fiscal_year=2025,
                source="test",
                revenue_yoy_growth=0.5,
                operating_profit=100,
                operating_margin=0.2,
                net_income=100,
                operating_cash_flow=100,
                debt_ratio=0.5,
            ),
            ValuationMetric(
                ipo_id=ipo.id,
                as_of_date=today,
                source="test",
                valuation_discount_premium_ratio=-0.3,
                per=10,
                peer_median_per=20,
                psr=1,
                peer_median_psr=2,
                valuation_comment="근거",
            ),
            IpoStructureMetric(
                ipo_id=ipo.id,
                as_of_date=today,
                source="test",
                free_float_ratio_on_listing=0.1,
                free_float_market_cap=10_000_000_000,
                secondary_share_ratio=0.05,
                use_of_proceeds_summary="시설 연구 성장 투자",
                vc_overhang_ratio=0.05,
                risk_flags_json=[],
            ),
            NewsMetric(
                ipo_id=ipo.id,
                source="test",
                window_start=today - timedelta(days=29),
                window_end=today,
                article_growth_rate_7d=2,
                major_media_count=5,
                positive_keyword_count=2,
                risk_keyword_count=0,
            ),
            SearchTrend(
                ipo_id=ipo.id,
                source="test",
                keyword_group="만점기업",
                window_start=today - timedelta(days=29),
                window_end=today,
                trend_growth_rate=2,
                peak_index=90,
                zero_data_ratio=0,
            ),
            MarketConditionSnapshot(
                as_of_date=today,
                kosdaq_momentum_20d=0.1,
                recent_ipo_success_rate=0.7,
                volatility_20d=0.1,
            ),
        ]
    )
    db_session.flush()
    result = IpoScoringService(default_user_cash=100_000_000).score(db_session, ipo)
    assert result.confidence_score == 100
    assert result.total_score == 100
    assert result.recommendation_band == "Strong"
