from datetime import datetime, timezone

import pytest

from app.models import Ipo, UnderwriterAllocation
from app.services.allocation import (
    _percentile,
    build_allocation_scenario,
    calculate_apply_shares,
    calculate_equal_expected,
    calculate_proportional_expected,
    derive_allocation_assumptions,
    equal_expectation_label,
    resolve_underwriter,
    round_down_by_unit,
)


def test_round_down_by_unit():
    assert round_down_by_unit(97) == 90
    assert round_down_by_unit(527) == 500
    assert round_down_by_unit(1234) == 1000


def test_round_down_rejects_invalid_units_and_handles_zero():
    assert round_down_by_unit(0) == 0
    with pytest.raises(ValueError, match="positive bounds"):
        round_down_by_unit(100, ((0, 10),))


def test_calculate_apply_shares_uses_deposit_and_unit():
    shares = calculate_apply_shares(
        user_cash=10_000_000,
        offer_price=20_000,
        deposit_rate=0.5,
        max_shares=2_000,
        min_shares=10,
    )
    assert shares == 1000


def test_equal_and_proportional_expected():
    assert calculate_equal_expected(500_000, 0.5, 250_000) == 1
    assert calculate_proportional_expected(1000, 1000) == 1
    assert equal_expectation_label(0.1) == "0주 가능성 높음"
    assert equal_expectation_label(0.5) == "0~1주 가능성"
    assert equal_expectation_label(1.0) == "1주 가능성"


def test_subscription_fee_is_reserved_before_share_calculation():
    shares = calculate_apply_shares(
        user_cash=102_000,
        offer_price=10_000,
        deposit_rate=0.5,
        subscription_fee=2_000,
    )
    assert shares == 20


def test_fee_or_minimum_shortfall_returns_zero():
    assert (
        calculate_apply_shares(
            user_cash=2_000,
            offer_price=10_000,
            deposit_rate=0.5,
            subscription_fee=2_000,
        )
        == 0
    )
    assert calculate_apply_shares(49_999, 10_000, 0.5, min_shares=10) == 0


def test_max_shares_is_applied_before_unit_rounding():
    shares = calculate_apply_shares(
        user_cash=100_000_000,
        offer_price=1_000,
        deposit_rate=0.5,
        max_shares=550,
    )
    assert shares == 500


def test_invalid_deposit_rate_returns_zero():
    assert calculate_apply_shares(1_000_000, 10_000, 1.5) == 0


def test_proportional_ratio_is_used():
    assert calculate_proportional_expected(1000, 1000, 0.5) == 0.5


def test_proportional_expected_rejects_invalid_inputs():
    assert calculate_proportional_expected(0, 1000) == 0
    assert calculate_proportional_expected(1000, 1000, 1.5) == 0


def test_invalid_equal_ratio_returns_zero():
    assert calculate_equal_expected(1000, 1.5, 100) == 0


def test_historical_broker_actuals_drive_assumptions(db_session):
    current_ipo = Ipo(corp_name="현재", normalized_name="현재", status="scheduled")
    db_session.add(current_ipo)
    db_session.flush()
    current = UnderwriterAllocation(ipo_id=current_ipo.id, broker_name="한국투자증권")
    db_session.add(current)
    for index, competition in enumerate((500, 1000, 1500), start=1):
        past_ipo = Ipo(corp_name=f"과거{index}", normalized_name=f"과거{index}", status="evaluated")
        db_session.add(past_ipo)
        db_session.flush()
        db_session.add(
            UnderwriterAllocation(
                ipo_id=past_ipo.id,
                broker_name="한국투자증권",
                actual_applicants_count=index * 100_000,
                actual_proportional_competition=competition,
                actual_recorded_at=None,
            )
        )
    db_session.flush()
    assumptions = derive_allocation_assumptions(db_session, current)
    assert assumptions.expected_applicants_count == 200_000
    assert assumptions.proportional_competition_base == 1000
    assert assumptions.source == "historical:한국투자증권:n=3"


def test_historical_assumptions_exclude_actuals_recorded_after_cutoff(db_session):
    current_ipo = Ipo(corp_name="현재시점", normalized_name="현재시점", status="scheduled")
    current = UnderwriterAllocation(broker_name="한국투자증권")
    current_ipo.underwriters.append(current)
    db_session.add(current_ipo)
    cutoff = datetime(2026, 7, 10, tzinfo=timezone.utc)
    for index, (competition, recorded_at) in enumerate(
        (
            (400, datetime(2026, 7, 1, tzinfo=timezone.utc)),
            (600, datetime(2026, 7, 2, tzinfo=timezone.utc)),
            (800, datetime(2026, 7, 3, tzinfo=timezone.utc)),
            (9_999, datetime(2026, 7, 11, tzinfo=timezone.utc)),
        )
    ):
        past = Ipo(corp_name=f"시점{index}", normalized_name=f"시점{index}", status="evaluated")
        db_session.add(past)
        db_session.flush()
        db_session.add(
            UnderwriterAllocation(
                ipo_id=past.id,
                broker_name="한국투자증권",
                actual_applicants_count=100_000,
                actual_proportional_competition=competition,
                actual_recorded_at=recorded_at,
            )
        )
    db_session.flush()

    assumptions = derive_allocation_assumptions(db_session, current, as_of=cutoff)

    assert assumptions.proportional_competition_base == 600
    assert assumptions.source == "historical:한국투자증권:n=3"


def test_same_ipo_has_different_broker_allocation_estimates():
    ipo = Ipo(id=1, corp_name="배정기업", normalized_name="배정기업", offer_price_final=10_000)
    broker_a = UnderwriterAllocation(
        broker_name="한국투자증권",
        retail_allocated_shares=600_000,
        subscription_fee=2_000,
        deposit_rate=0.5,
    )
    broker_b = UnderwriterAllocation(
        broker_name="KB증권",
        retail_allocated_shares=200_000,
        subscription_fee=1_500,
        deposit_rate=0.5,
    )
    result_a = build_allocation_scenario(ipo, broker_a, 10_000_000)
    result_b = build_allocation_scenario(ipo, broker_b, 10_000_000)
    assert result_a.expected_equal_shares_mid != result_b.expected_equal_shares_mid
    assert result_a.estimated_fee != result_b.estimated_fee
    assert result_a.assumption_updated_at is not None


def test_multiple_brokers_require_an_explicit_selection():
    ipo = Ipo(corp_name="복수주관", normalized_name="복수주관")
    first = UnderwriterAllocation(broker_name="한국투자증권")
    second = UnderwriterAllocation(broker_name="KB증권")
    ipo.underwriters.extend([first, second])

    with pytest.raises(ValueError, match="must be selected"):
        resolve_underwriter(ipo)
    assert resolve_underwriter(ipo, "한투") is first


def test_underwriter_resolution_handles_none_single_and_missing_request():
    ipo = Ipo(corp_name="주관", normalized_name="주관")
    assert resolve_underwriter(ipo) is None
    only = UnderwriterAllocation(broker_name="한국투자증권")
    ipo.underwriters.append(only)
    assert resolve_underwriter(ipo) is only
    with pytest.raises(ValueError, match="Broker not found"):
        resolve_underwriter(ipo, "없는증권")


def test_allocation_requires_offer_price_and_labels_large_expectation():
    ipo = Ipo(corp_name="가격없음", normalized_name="가격없음")
    with pytest.raises(ValueError, match="Offer price is missing"):
        build_allocation_scenario(ipo, None, 1_000_000)
    assert equal_expectation_label(2) == "1주 이상 가능성"


def test_empty_percentile_is_rejected():
    with pytest.raises(ValueError, match="empty list"):
        _percentile([], 0.5)


def test_offer_size_adjusts_broker_assumptions(db_session):
    ipo = Ipo(
        corp_name="대형공모",
        normalized_name="대형공모",
        status="scheduled",
        offer_amount_million=150_000,
    )
    broker = UnderwriterAllocation(broker_name="한국투자증권")
    ipo.underwriters.append(broker)
    db_session.add(ipo)
    db_session.flush()
    assumptions = derive_allocation_assumptions(db_session, broker)
    assert assumptions.expected_applicants_count == 360_000
    assert "offer-size:large" in assumptions.source


@pytest.mark.parametrize(
    ("offer_amount_million", "expected_applicants", "source_suffix"),
    [
        (10_000, 240_000, "offer-size:small"),
        (50_000, 300_000, None),
    ],
)
def test_small_and_medium_offer_size_assumptions(
    db_session,
    offer_amount_million,
    expected_applicants,
    source_suffix,
):
    ipo = Ipo(
        corp_name=f"공모{offer_amount_million}",
        normalized_name=f"공모{offer_amount_million}",
        status="scheduled",
        offer_amount_million=offer_amount_million,
    )
    broker = UnderwriterAllocation(broker_name="한국투자증권")
    ipo.underwriters.append(broker)
    db_session.add(ipo)
    db_session.flush()

    assumptions = derive_allocation_assumptions(db_session, broker)

    assert assumptions.expected_applicants_count == expected_applicants
    if source_suffix:
        assert source_suffix in assumptions.source
    else:
        assert "offer-size" not in assumptions.source
