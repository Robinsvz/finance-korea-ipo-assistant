from datetime import date, datetime, timezone

import pytest

from app.models import Ipo, ScoreSnapshot, UnderwriterAllocation, UserParticipation
from app.services.participation import (
    list_participation_summaries,
    record_exit,
    record_participation,
    summarize_participation,
    summarize_participation_portfolio,
)


def _ipo(session, *, name="참여기업", offer_price=10_000):
    ipo = Ipo(
        corp_name=name,
        normalized_name=name,
        offer_price_final=offer_price,
        status="subscription_open",
    )
    ipo.underwriters.append(UnderwriterAllocation(broker_name="한국투자증권"))
    session.add(ipo)
    session.flush()
    return ipo


def _participation(session, *, allocated=3, cash_committed=50_000):
    ipo = _ipo(session)
    row = record_participation(
        session,
        ipo,
        broker_name="한국투자",
        decision_action="candidate",
        user_cash=1_000_000,
        applied_shares=10,
        cash_committed=cash_committed,
        allocated_shares=allocated,
        subscription_fee=2_000,
        decision_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
        note=" 최초 기록 ",
    )
    return ipo, row


def test_participation_upserts_and_preserves_score_provenance(db_session):
    ipo = _ipo(db_session)
    first_decision_at = datetime(2026, 7, 20, tzinfo=timezone.utc)
    score = ScoreSnapshot(
        ipo_id=ipo.id,
        score_date=date(2026, 7, 20),
        total_score=80,
        recommendation_band="Strong",
    )
    db_session.add(score)
    db_session.flush()

    first = record_participation(
        db_session,
        ipo,
        broker_name="한국투자증권",
        decision_action="candidate",
        user_cash=1_000_000,
        applied_shares=10,
        cash_committed=50_000,
        allocated_shares=3,
        score=score,
        decision_at=first_decision_at,
    )
    second = record_participation(
        db_session,
        ipo,
        broker_name="한국투자증권",
        decision_action="value_skip",
        user_cash=2_000_000,
        applied_shares=20,
        cash_committed=100_000,
        allocated_shares=None,
        decision_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        note="updated",
    )

    assert first.id == second.id
    assert second.score_snapshot_id == score.id
    assert second.decision_at == first_decision_at
    assert second.decision_action == "CANDIDATE"
    assert second.allocated_shares == 3
    assert float(second.offer_price_snapshot) == 10_000
    assert second.note == "updated"
    assert db_session.query(UserParticipation).count() == 1


def test_participation_update_rejects_allocation_inconsistent_with_application_or_exits(
    db_session,
):
    ipo, participation = _participation(db_session, allocated=3)
    record_exit(db_session, participation, sold_shares=2, sell_price=12_000)
    values = {
        "broker_name": "한국투자증권",
        "decision_action": "candidate",
        "user_cash": 1_000_000,
        "applied_shares": 10,
        "cash_committed": 50_000,
    }

    with pytest.raises(ValueError, match="already sold"):
        record_participation(db_session, ipo, allocated_shares=1, **values)

    values["applied_shares"] = 2
    with pytest.raises(ValueError, match="cannot exceed applied"):
        record_participation(db_session, ipo, allocated_shares=None, **values)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"broker_name": " "}, "broker_name"),
        ({"broker_name": "없는증권"}, "Broker not found"),
        ({"decision_action": " "}, "decision_action"),
        ({"decision_action": "BUY_NOW"}, "decision_action"),
        ({"user_cash": 0}, "positive"),
        ({"applied_shares": 0}, "positive"),
        ({"cash_committed": 0}, "positive"),
        ({"cash_committed": 2_000_000}, "cannot exceed"),
        ({"allocated_shares": -1}, "between zero"),
        ({"allocated_shares": 11}, "between zero"),
        ({"subscription_fee": -1}, "non-negative"),
    ],
)
def test_participation_validation(db_session, overrides, message):
    ipo = _ipo(db_session)
    values = {
        "broker_name": "한국투자증권",
        "decision_action": "candidate",
        "user_cash": 1_000_000,
        "applied_shares": 10,
        "cash_committed": 50_000,
        "allocated_shares": None,
        "subscription_fee": 0,
    }
    values.update(overrides)

    with pytest.raises(ValueError, match=message):
        record_participation(db_session, ipo, **values)


def test_participation_rejects_score_from_other_ipo(db_session):
    ipo = _ipo(db_session)
    other = _ipo(db_session, name="다른기업")
    score = ScoreSnapshot(
        ipo_id=other.id,
        score_date=date(2026, 7, 20),
        total_score=50,
        recommendation_band="Neutral",
    )
    db_session.add(score)
    db_session.flush()

    with pytest.raises(ValueError, match="different IPO"):
        record_participation(
            db_session,
            ipo,
            broker_name="한국투자증권",
            decision_action="candidate",
            user_cash=1_000_000,
            applied_shares=10,
            cash_committed=50_000,
            score=score,
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"sold_shares": 0}, "positive"),
        ({"sell_price": 0}, "positive"),
        ({"selling_cost": -1}, "non-negative"),
        ({"strategy": " "}, "strategy"),
    ],
)
def test_exit_validation(db_session, overrides, message):
    _ipo_row, participation = _participation(db_session)
    values = {
        "sold_shares": 1,
        "sell_price": 12_000,
        "selling_cost": 0,
        "strategy": "D0_OPEN",
    }
    values.update(overrides)

    with pytest.raises(ValueError, match=message):
        record_exit(db_session, participation, **values)


def test_exit_requires_allocation_and_never_oversells(db_session):
    _ipo_row, pending = _participation(db_session, allocated=None)
    with pytest.raises(ValueError, match="allocated_shares"):
        record_exit(db_session, pending, sold_shares=1, sell_price=12_000)

    pending.allocated_shares = 2
    record_exit(db_session, pending, sold_shares=1, sell_price=12_000)
    with pytest.raises(ValueError, match="cannot exceed"):
        record_exit(db_session, pending, sold_shares=2, sell_price=12_000)


def test_realized_profit_supports_partial_and_complete_exits(db_session):
    _ipo_row, participation = _participation(db_session, allocated=3)
    first = record_exit(
        db_session,
        participation,
        sold_shares=1,
        sell_price=12_000,
        strategy="d0_open",
        selling_cost=100,
        sold_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
        note=" first ",
    )
    partial = summarize_participation(participation)

    assert first.strategy == "D0_OPEN"
    assert first.note == "first"
    assert partial.realization_status == "partially-realized"
    assert partial.gross_realized_profit == 2_000
    assert partial.net_realized_profit == -100
    assert partial.realized_return_on_committed_cash_pct == pytest.approx(-0.2)
    assert partial.to_dict()["remaining_shares"] == 2

    record_exit(
        db_session,
        participation,
        sold_shares=2,
        sell_price=15_000,
        selling_cost=200,
    )
    realized = summarize_participation(participation)

    assert realized.realization_status == "realized"
    assert realized.sold_shares == 3
    assert realized.remaining_shares == 0
    assert realized.gross_realized_profit == 12_000
    assert realized.net_realized_profit == 9_700


def test_summary_states_and_immutable_final_offer_price(db_session):
    ipo, pending = _participation(db_session, allocated=None)
    assert summarize_participation(pending).realization_status == "allocation-pending"

    pending.allocated_shares = 0
    assert summarize_participation(pending).realization_status == "not-allocated"
    pending.allocated_shares = 1
    assert summarize_participation(pending).realization_status == "holding"
    ipo.offer_price_final = 20_000
    ipo.offer_price_max = 9_000
    assert summarize_participation(pending).offer_price == 10_000
    record_exit(db_session, pending, sold_shares=1, sell_price=12_000)
    assert summarize_participation(pending).gross_realized_profit == 2_000

    no_final_ipo = _ipo(db_session, name="공모가미확정", offer_price=None)
    no_final_ipo.offer_price_max = 9_000
    no_basis = record_participation(
        db_session,
        no_final_ipo,
        broker_name="한국투자증권",
        decision_action="candidate",
        user_cash=1_000_000,
        applied_shares=10,
        cash_committed=50_000,
        allocated_shares=1,
    )
    record_exit(db_session, no_basis, sold_shares=1, sell_price=12_000)
    summary = summarize_participation(no_basis)
    assert summary.offer_price is None
    assert summary.net_realized_profit is None


def test_zero_committed_cash_does_not_divide_and_list_filter_works(db_session):
    first_ipo, first = _participation(db_session, allocated=1, cash_committed=50_000)
    second_ipo = _ipo(db_session, name="두번째")
    second = UserParticipation(
        ipo_id=second_ipo.id,
        broker_name="한국투자증권",
        decision_action="CANDIDATE",
        user_cash=1,
        applied_shares=1,
        cash_committed=0,
        allocated_shares=1,
        offer_price_snapshot=10_000,
        subscription_fee=0,
    )
    db_session.add(second)
    db_session.flush()
    record_exit(db_session, second, sold_shares=1, sell_price=12_000)

    assert summarize_participation(second).realized_return_on_committed_cash_pct is None
    assert len(list_participation_summaries(db_session)) == 2
    filtered = list_participation_summaries(db_session, ipo_id=first_ipo.id)
    assert len(filtered) == 1
    assert filtered[0].participation_id == first.id


def test_portfolio_summary_rolls_up_only_known_realized_results(db_session):
    assert summarize_participation_portfolio([]).to_dict() == {
        "participation_count": 0,
        "realized_sales_count": 0,
        "fully_realized_count": 0,
        "gross_realized_profit": None,
        "net_realized_profit": None,
        "realized_cash_committed": 0,
        "realized_return_on_committed_cash_pct": None,
    }

    _first_ipo, first = _participation(db_session, allocated=1, cash_committed=50_000)
    record_exit(db_session, first, sold_shares=1, sell_price=12_000)
    _pending_ipo, pending = _participation(db_session, allocated=None, cash_committed=70_000)
    portfolio = summarize_participation_portfolio(
        [summarize_participation(first), summarize_participation(pending)]
    )

    assert portfolio.participation_count == 2
    assert portfolio.realized_sales_count == 1
    assert portfolio.fully_realized_count == 1
    assert portfolio.gross_realized_profit == 2_000
    assert portfolio.net_realized_profit == 0
    assert portfolio.realized_cash_committed == 50_000
    assert portfolio.realized_return_on_committed_cash_pct == 0
