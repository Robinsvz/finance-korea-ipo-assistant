from app.models import DemandForecast, FinancialMetric, NewsMetric, SearchTrend
from app.services.scoring import (
    recommendation_band,
    score_demand,
    score_financial,
    score_institutional_competition,
    score_news_trend,
)


def test_score_institutional_competition_thresholds():
    assert score_institutional_competition(None) == 0
    assert score_institutional_competition(1500) == 10
    assert score_institutional_competition(1000) == 8
    assert score_institutional_competition(500) == 6
    assert score_institutional_competition(100) == 3
    assert score_institutional_competition(99) == 0


def test_score_demand_combines_fields():
    demand = DemandForecast(
        institutional_competition_ratio=1200,
        lockup_commitment_ratio=0.2,
        final_price_position="top_band",
        participants_count=1800,
        above_band_participation_ratio=0.6,
    )
    score, explanation = score_demand(demand)
    assert score == 20
    assert explanation["ratio_score"] == 8


def test_score_demand_normalizes_percentage_lockup():
    demand = DemandForecast(lockup_commitment_ratio=30)
    score, explanation = score_demand(demand)
    assert score == 7
    assert explanation["lockup_score"] == 7


def test_score_financial_profitable_growth():
    metric = FinancialMetric(
        revenue_yoy_growth=0.35,
        operating_profit=10,
        operating_margin=0.2,
        net_income=8,
        operating_cash_flow=7,
        debt_ratio=0.7,
    )
    score, _ = score_financial(metric)
    assert score == 15


def test_score_news_trend_caps_at_ten():
    news = NewsMetric(
        article_growth_rate_7d=1.2,
        major_media_count=4,
        risk_keyword_count=0,
        positive_keyword_count=2,
    )
    trend = SearchTrend(trend_growth_rate=2.0, peak_index=90, zero_data_ratio=0.1)
    score, _ = score_news_trend(news, trend)
    assert score == 10


def test_recommendation_band():
    assert recommendation_band(80) == "Strong"
    assert recommendation_band(65) == "Watch"
    assert recommendation_band(50) == "Neutral"
    assert recommendation_band(49.9) == "Avoid"
