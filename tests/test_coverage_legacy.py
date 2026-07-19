from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from app.models import (
    FinancialMetric,
    Ipo,
    IpoStructureMetric,
    MarketConditionSnapshot,
    NewsMetric,
    ScoreSnapshot,
    SearchTrend,
    ValuationMetric,
)
from app.repositories import (
    derive_status,
    find_ipo,
    refresh_ipo_statuses,
    upsert_diligence_review,
    upsert_outcome_evaluation,
)
from app.services import scoring
from app.services.allocation import round_down_by_unit
from app.services.diligence import _age_days
from app.services.scoring import (
    IpoScoringService,
    load_scoring_rules,
    score_financial,
    score_market_condition,
    score_news_trend,
    score_structure,
    score_valuation,
)

TODAY = date(2026, 7, 18)


def _ipo(**values):
    defaults = {"corp_name": "분기테스트", "normalized_name": "분기테스트", "status": "scheduled"}
    return Ipo(**{**defaults, **values})


def test_repository_fallback_and_no_change_paths(db_session):
    row = _ipo(
        subscription_start_date=TODAY + timedelta(days=1),
        subscription_end_date=TODAY + timedelta(days=2),
    )
    db_session.add(row)
    db_session.flush()

    assert find_ipo(db_session, row.corp_name, dart_corp_code="missing") is row
    assert derive_status(row, TODAY) == "scheduled"
    assert refresh_ipo_statuses(db_session, TODAY) == {"checked": 1, "changed": 0}


def test_existing_diligence_review_accepts_non_regressive_update(db_session):
    ipo = _ipo()
    db_session.add(ipo)
    db_session.flush()
    first = upsert_diligence_review(
        db_session,
        ipo_id=ipo.id,
        review_type="articles",
        values={"review_status": "collected"},
    )
    db_session.flush()

    second = upsert_diligence_review(
        db_session,
        ipo_id=ipo.id,
        review_type="articles",
        values={"review_status": "reviewed", "reviewed_by": "tester"},
    )

    assert second is first
    assert second.review_status == "reviewed"
    assert second.reviewed_by == "tester"


def test_outcome_upsert_with_score_snapshot(db_session):
    ipo = _ipo()
    db_session.add(ipo)
    db_session.flush()
    score = ScoreSnapshot(
        ipo_id=ipo.id,
        score_date=TODAY,
        total_score=50,
        recommendation_band="Neutral",
        model_version="coverage",
    )
    db_session.add(score)
    db_session.flush()

    outcome = upsert_outcome_evaluation(
        db_session,
        ipo_id=ipo.id,
        score_snapshot_id=score.id,
        evaluation_version="coverage",
        values={"offer_price": 10_000},
    )

    assert outcome.score_snapshot_id == score.id


def test_round_down_uses_last_unit_when_shares_exceed_all_bounds():
    assert round_down_by_unit(101, ((100, 10),)) == 100


def test_age_days_accepts_timezone_aware_datetime():
    assert _age_days(datetime.now(timezone.utc)) == 0


def test_optional_financial_valuation_and_structure_fields_can_all_be_absent():
    assert score_financial(FinancialMetric())[0] == 0
    assert score_valuation(ValuationMetric())[0] == 0
    assert score_structure(IpoStructureMetric())[0] == 0


def test_optional_news_and_trend_signals_can_be_inactive():
    score, _ = score_news_trend(
        NewsMetric(positive_keyword_count=0),
        SearchTrend(peak_index=0, zero_data_ratio=0),
    )
    assert score == 0


def test_optional_market_signals_can_be_absent_or_neutral():
    rules = load_scoring_rules().market
    absent = MarketConditionSnapshot(as_of_date=TODAY)
    neutral = MarketConditionSnapshot(
        as_of_date=TODAY,
        kosdaq_momentum_20d=0,
        recent_ipo_success_rate=0.5,
        volatility_20d=0.3,
    )

    assert score_market_condition(absent, rules)[0] == 5
    assert score_market_condition(neutral, rules)[0] == 5


def test_allocation_score_accepts_zero_share_expectations(db_session, monkeypatch):
    ipo = _ipo(offer_price_final=10_000)
    db_session.add(ipo)
    db_session.flush()
    monkeypatch.setattr(
        scoring,
        "derive_allocation_assumptions",
        lambda _session, _underwriter: SimpleNamespace(),
    )
    monkeypatch.setattr(
        scoring,
        "build_allocation_scenario",
        lambda *_args, **_kwargs: SimpleNamespace(
            expected_equal_shares_mid=0,
            expected_proportional_shares_base=0,
            estimated_fee=3_000,
            broker_name="미지정",
            apply_shares=0,
            assumption_source="test",
        ),
    )

    score, detail = IpoScoringService()._score_allocation(db_session, ipo)

    assert score == 0
    assert detail["apply_shares"] == 0
