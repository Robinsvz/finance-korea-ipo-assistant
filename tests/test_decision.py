from datetime import date, datetime, timedelta, timezone

import pytest

from app.models import Ipo, ScoreSnapshot, UnderwriterAllocation
from app.services.backtest import BacktestReport, BandBacktestMetrics
from app.services.decision import DecisionPolicy, assess_ipo_decision


def _ipo(session, *, multiple_brokers=False):
    ipo = Ipo(
        corp_name="판단기업",
        normalized_name="판단기업",
        status="scheduled",
        offer_price_final=10_000,
        subscription_end_date=date(2026, 7, 20),
        refund_date=date(2026, 7, 22),
    )
    ipo.underwriters.append(
        UnderwriterAllocation(
            broker_name="한국투자증권",
            retail_allocated_shares=600_000,
            equal_allocation_ratio=0.5,
            proportional_allocation_ratio=0.5,
            deposit_rate=0.5,
            subscription_fee=2_000,
            expected_applicants_count=300_000,
            proportional_competition_base=1_000,
        )
    )
    if multiple_brokers:
        ipo.underwriters.append(
            UnderwriterAllocation(
                broker_name="KB증권",
                retail_allocated_shares=1_200_000,
                equal_allocation_ratio=0.5,
                proportional_allocation_ratio=0.5,
                deposit_rate=0.5,
                subscription_fee=3_000,
                expected_applicants_count=300_000,
                proportional_competition_base=1_000,
            )
        )
    session.add(ipo)
    session.flush()
    return ipo


def _score(ipo, *, band="Strong", confidence=90, gates=None, missing=None):
    return ScoreSnapshot(
        ipo_id=ipo.id,
        score_date=date(2026, 7, 19),
        calculated_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
        total_score=80,
        confidence_score=confidence,
        recommendation_band=band,
        model_version="rules-v3-simple",
        rules_hash="hash",
        explanation_json={"gates": gates or {}},
        missing_features_json=missing or [],
    )


def _report(
    *,
    sample_count=3,
    median_return=20,
    loss_probability=0.2,
    positive_probability=0.8,
):
    band = BandBacktestMetrics(
        sample_count=sample_count,
        prediction_accuracy=0.8,
        average_d1_return_pct=median_return,
        median_d1_return_pct=median_return,
        loss_probability=loss_probability,
        small_sample_warning=sample_count < 10,
        positive_probability=positive_probability,
    )
    return BacktestReport(
        evaluated_count=sample_count,
        prediction_accuracy=0.8,
        average_open_return_pct=10,
        average_d1_return_pct=median_return,
        strong_watch_accuracy=0.8,
        avoid_accuracy=None,
        band_metrics={"Strong": band, "Watch": band},
        validation_status="ready",
    )


def test_missing_score_requires_data(db_session):
    ipo = _ipo(db_session)

    result = assess_ipo_decision(db_session, ipo, None, user_cash=10_000_000)

    assert result.action == "DATA_REQUIRED"
    assert result.to_dict()["validation_status"] == "missing-score"


def test_hard_block_gate_overrides_score(db_session):
    ipo = _ipo(db_session)
    score = _score(
        ipo,
        gates={"hard_block_reasons": ["상장철회"], "review_reasons": []},
    )

    result = assess_ipo_decision(db_session, ipo, score, user_cash=10_000_000)

    assert result.action == "BLOCKED"
    assert result.risks == ("상장철회",)


def test_blocked_band_without_reason_uses_fallback_risk(db_session):
    ipo = _ipo(db_session)
    result = assess_ipo_decision(
        db_session,
        ipo,
        _score(ipo, band="Blocked"),
        user_cash=10_000_000,
    )
    assert result.risks == ("중대 위험 gate",)


def test_low_confidence_and_missing_features_require_data(db_session):
    ipo = _ipo(db_session)
    score = _score(ipo, confidence=50, missing=["demand_forecast"])

    result = assess_ipo_decision(db_session, ipo, score, user_cash=10_000_000)

    assert result.action == "DATA_REQUIRED"
    assert result.risks == ("demand_forecast",)


def test_insufficient_band_uses_default_missing_risk(db_session):
    ipo = _ipo(db_session)
    result = assess_ipo_decision(
        db_session,
        ipo,
        _score(ipo, band="Insufficient Data", missing=[]),
        user_cash=10_000_000,
    )
    assert result.risks == ("데이터 완성도 부족",)


def test_non_positive_band_is_value_skip(db_session):
    ipo = _ipo(db_session)
    score = _score(ipo, band="Neutral", gates={"review_reasons": ["영업손실"]})

    result = assess_ipo_decision(db_session, ipo, score, user_cash=10_000_000)

    assert result.action == "VALUE_SKIP"
    assert "Neutral" in result.reasons[0]
    assert result.risks == ("영업손실",)


def test_zero_application_is_value_skip(db_session):
    ipo = _ipo(db_session)

    result = assess_ipo_decision(
        db_session,
        ipo,
        _score(ipo),
        user_cash=2_000,
    )

    assert result.action == "VALUE_SKIP"
    assert result.expected_shares == 0
    assert result.break_even_return_pct is None


def test_small_validation_sample_is_research_only(db_session):
    ipo = _ipo(db_session, multiple_brokers=True)
    policy = DecisionPolicy(minimum_samples=3)

    result = assess_ipo_decision(
        db_session,
        ipo,
        _score(ipo),
        user_cash=10_000_000,
        policy=policy,
        report=_report(sample_count=2),
    )

    assert result.action == "RESEARCH_ONLY"
    assert result.broker_name == "KB증권"
    assert result.positive_probability is None
    assert result.validation_sample_count == 2


def test_non_ready_or_mixed_backtest_can_never_produce_candidate(db_session):
    ipo = _ipo(db_session)
    report = _report(sample_count=30)
    report.validation_status = "mixed-rules"

    result = assess_ipo_decision(
        db_session,
        ipo,
        _score(ipo),
        user_cash=10_000_000,
        policy=DecisionPolicy(minimum_samples=1),
        report=report,
    )

    assert result.action == "RESEARCH_ONLY"
    assert result.validation_status == "mixed-rules"
    assert "mixed-rules" in result.reasons[0]


def test_missing_band_metrics_is_research_only(db_session):
    ipo = _ipo(db_session)
    report = _report()
    report.band_metrics = {}

    result = assess_ipo_decision(
        db_session,
        ipo,
        _score(ipo),
        user_cash=10_000_000,
        policy=DecisionPolicy(minimum_samples=1),
        report=report,
    )

    assert result.action == "RESEARCH_ONLY"
    assert result.validation_sample_count == 0


def test_validated_candidate_uses_net_value_and_probability(db_session):
    ipo = _ipo(db_session)
    policy = DecisionPolicy(
        minimum_samples=3,
        minimum_expected_net_profit=0,
        minimum_positive_probability=0.6,
    )

    result = assess_ipo_decision(
        db_session,
        ipo,
        _score(ipo),
        user_cash=10_000_000,
        policy=policy,
        report=_report(sample_count=3, median_return=20, positive_probability=0.8),
    )

    assert result.action == "CANDIDATE"
    assert result.positive_probability == pytest.approx(0.8)
    assert result.expected_net_profit is not None and result.expected_net_profit > 0
    assert result.break_even_return_pct is not None
    assert result.validation_status == "validated"


def test_incomplete_outcomes_are_research_only_with_both_risks(db_session):
    ipo = _ipo(db_session)
    policy = DecisionPolicy(
        minimum_samples=3,
        minimum_expected_net_profit=1_000_000,
        minimum_positive_probability=0.9,
    )

    result = assess_ipo_decision(
        db_session,
        ipo,
        _score(ipo),
        user_cash=10_000_000,
        policy=policy,
        report=_report(
            sample_count=3,
            median_return=None,
            loss_probability=None,
            positive_probability=None,
        ),
    )

    assert result.action == "RESEARCH_ONLY"
    assert result.expected_net_profit is None
    assert result.positive_probability is None
    assert result.validation_status == "incomplete-outcomes"
    assert len(result.risks) == 2


@pytest.mark.parametrize(
    ("median_return", "positive_probability", "expected_risk"),
    [
        (None, 0.8, "D1 중앙값 수익률"),
        (20, None, "D1 양수 확률"),
    ],
)
def test_incomplete_outcomes_identify_the_single_missing_metric(
    db_session,
    median_return,
    positive_probability,
    expected_risk,
):
    ipo = _ipo(db_session)

    result = assess_ipo_decision(
        db_session,
        ipo,
        _score(ipo),
        user_cash=10_000_000,
        policy=DecisionPolicy(minimum_samples=1),
        report=_report(
            sample_count=1,
            median_return=median_return,
            positive_probability=positive_probability,
        ),
    )

    assert result.action == "RESEARCH_ONLY"
    assert len(result.risks) == 1
    assert expected_risk in result.risks[0]


def test_validated_value_skip_lists_probability_and_profit_failures(db_session):
    ipo = _ipo(db_session)

    result = assess_ipo_decision(
        db_session,
        ipo,
        _score(ipo),
        user_cash=10_000_000,
        policy=DecisionPolicy(
            minimum_samples=1,
            minimum_positive_probability=0.9,
            minimum_expected_net_profit=1_000_000,
        ),
        report=_report(sample_count=1, median_return=1, positive_probability=0.8),
    )

    assert result.action == "VALUE_SKIP"
    assert result.risks == (
        "검증된 양수 확률이 정책 기준보다 낮습니다.",
        "예상 순이익이 정책 기준보다 낮습니다.",
    )


def test_decision_uses_direct_positive_probability_not_one_minus_loss(db_session):
    ipo = _ipo(db_session)

    result = assess_ipo_decision(
        db_session,
        ipo,
        _score(ipo),
        user_cash=10_000_000,
        policy=DecisionPolicy(
            minimum_samples=1,
            minimum_positive_probability=0.6,
            minimum_expected_net_profit=0,
        ),
        report=_report(
            sample_count=1,
            median_return=20,
            loss_probability=0,
            positive_probability=0,
        ),
    )

    assert result.action == "VALUE_SKIP"
    assert result.positive_probability == 0
    assert result.risks == ("검증된 양수 확률이 정책 기준보다 낮습니다.",)


def test_payment_date_and_non_positive_lock_period_do_not_add_opportunity_cost(db_session):
    ipo = _ipo(db_session)
    ipo.refund_date = None
    ipo.payment_date = ipo.subscription_end_date - timedelta(days=1)
    policy = DecisionPolicy(minimum_samples=1, minimum_expected_net_profit=0)

    result = assess_ipo_decision(
        db_session,
        ipo,
        _score(ipo),
        user_cash=10_000_000,
        policy=policy,
        report=_report(sample_count=1, median_return=20, positive_probability=1),
    )

    assert result.action == "CANDIDATE"


def test_missing_release_date_does_not_add_opportunity_cost(db_session):
    ipo = _ipo(db_session)
    ipo.refund_date = None
    ipo.payment_date = None

    result = assess_ipo_decision(
        db_session,
        ipo,
        _score(ipo),
        user_cash=10_000_000,
        policy=DecisionPolicy(minimum_samples=1, minimum_expected_net_profit=0),
        report=_report(sample_count=1, median_return=20, positive_probability=1),
    )

    assert result.action == "CANDIDATE"


@pytest.mark.parametrize(
    ("policy", "cash", "message"),
    [
        (DecisionPolicy(), 0, "user_cash"),
        (DecisionPolicy(minimum_samples=0), 1, "minimum_samples"),
        (DecisionPolicy(minimum_confidence=101), 1, "minimum_confidence"),
        (
            DecisionPolicy(minimum_positive_probability=1.1),
            1,
            "minimum_positive_probability",
        ),
        (DecisionPolicy(annual_opportunity_cost_rate=-1), 1, "non-negative"),
    ],
)
def test_decision_policy_validation(db_session, policy, cash, message):
    ipo = _ipo(db_session)
    with pytest.raises(ValueError, match=message):
        assess_ipo_decision(db_session, ipo, None, user_cash=cash, policy=policy)
