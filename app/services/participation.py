from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Ipo, ParticipationExit, ScoreSnapshot, UserParticipation
from app.services.normalization import normalize_broker_name

DECISION_ACTIONS = {"CANDIDATE", "RESEARCH_ONLY", "VALUE_SKIP", "DATA_REQUIRED", "BLOCKED"}


@dataclass(frozen=True)
class ParticipationSummary:
    participation_id: int
    ipo_id: int
    corp_name: str
    broker_name: str
    decision_action: str
    score_snapshot_id: Optional[int]
    offer_price: Optional[float]
    user_cash: int
    cash_committed: int
    applied_shares: int
    allocated_shares: Optional[int]
    sold_shares: int
    remaining_shares: Optional[int]
    gross_realized_profit: Optional[float]
    net_realized_profit: Optional[float]
    realized_return_on_committed_cash_pct: Optional[float]
    realization_status: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ParticipationPortfolioSummary:
    participation_count: int
    realized_sales_count: int
    fully_realized_count: int
    gross_realized_profit: Optional[float]
    net_realized_profit: Optional[float]
    realized_cash_committed: int
    realized_return_on_committed_cash_pct: Optional[float]

    def to_dict(self) -> dict:
        return asdict(self)


def record_participation(
    session: Session,
    ipo: Ipo,
    *,
    broker_name: str,
    decision_action: str,
    user_cash: int,
    applied_shares: int,
    cash_committed: int,
    allocated_shares: Optional[int] = None,
    subscription_fee: int = 0,
    score: Optional[ScoreSnapshot] = None,
    decision_at: Optional[datetime] = None,
    note: str = "",
) -> UserParticipation:
    broker = normalize_broker_name(broker_name)
    _validate_participation(
        ipo,
        broker,
        decision_action,
        user_cash,
        applied_shares,
        cash_committed,
        allocated_shares,
        subscription_fee,
        score,
    )
    row = session.execute(
        select(UserParticipation).where(
            UserParticipation.ipo_id == ipo.id,
            UserParticipation.broker_name == broker,
        )
    ).scalar_one_or_none()
    if row is None:
        row = UserParticipation(
            ipo_id=ipo.id,
            broker_name=broker,
            score_snapshot_id=score.id if score else None,
            decision_at=decision_at or datetime.now(timezone.utc),
            decision_action=decision_action.strip().upper(),
            offer_price_snapshot=ipo.offer_price_final,
        )
        session.add(row)
    effective_allocation = row.allocated_shares if allocated_shares is None else allocated_shares
    sold_shares = sum(exit_row.sold_shares for exit_row in row.exits)
    _validate_effective_allocation(effective_allocation, applied_shares, sold_shares)
    row.user_cash = user_cash
    row.applied_shares = applied_shares
    row.cash_committed = cash_committed
    row.allocated_shares = effective_allocation
    row.subscription_fee = subscription_fee
    row.note = note.strip() or None
    session.flush()
    return row


def record_exit(
    session: Session,
    participation: UserParticipation,
    *,
    sold_shares: int,
    sell_price: float,
    strategy: str = "MANUAL",
    selling_cost: int = 0,
    sold_at: Optional[datetime] = None,
    note: str = "",
) -> ParticipationExit:
    if sold_shares <= 0 or sell_price <= 0:
        raise ValueError("sold_shares and sell_price must be positive")
    if selling_cost < 0:
        raise ValueError("selling_cost must be non-negative")
    if not strategy.strip():
        raise ValueError("strategy is required")
    if participation.allocated_shares is None:
        raise ValueError("allocated_shares must be recorded before an exit")
    already_sold = sum(row.sold_shares for row in participation.exits)
    if already_sold + sold_shares > participation.allocated_shares:
        raise ValueError("cumulative sold shares cannot exceed allocated_shares")
    row = ParticipationExit(
        participation=participation,
        sold_at=sold_at or datetime.now(timezone.utc),
        strategy=strategy.strip().upper(),
        sold_shares=sold_shares,
        sell_price=sell_price,
        selling_cost=selling_cost,
        note=note.strip() or None,
    )
    session.add(row)
    session.flush()
    return row


def summarize_participation(participation: UserParticipation) -> ParticipationSummary:
    ipo = participation.ipo
    offer_price_value = participation.offer_price_snapshot
    offer_price = float(offer_price_value) if offer_price_value is not None else None
    sold_shares = sum(row.sold_shares for row in participation.exits)
    remaining = (
        participation.allocated_shares - sold_shares
        if participation.allocated_shares is not None
        else None
    )
    gross_profit = None
    net_profit = None
    return_on_cash = None
    if sold_shares and offer_price is not None:
        gross_profit = sum(
            row.sold_shares * (float(row.sell_price) - offer_price) for row in participation.exits
        )
        net_profit = (
            gross_profit
            - participation.subscription_fee
            - sum(row.selling_cost for row in participation.exits)
        )
        if participation.cash_committed > 0:
            return_on_cash = net_profit / participation.cash_committed * 100
    if participation.allocated_shares is None:
        status = "allocation-pending"
    elif participation.allocated_shares == 0:
        status = "not-allocated"
    elif sold_shares == 0:
        status = "holding"
    elif remaining == 0:
        status = "realized"
    else:
        status = "partially-realized"
    return ParticipationSummary(
        participation_id=participation.id,
        ipo_id=participation.ipo_id,
        corp_name=ipo.corp_name,
        broker_name=participation.broker_name,
        decision_action=participation.decision_action,
        score_snapshot_id=participation.score_snapshot_id,
        offer_price=offer_price,
        user_cash=participation.user_cash,
        cash_committed=participation.cash_committed,
        applied_shares=participation.applied_shares,
        allocated_shares=participation.allocated_shares,
        sold_shares=sold_shares,
        remaining_shares=remaining,
        gross_realized_profit=gross_profit,
        net_realized_profit=net_profit,
        realized_return_on_committed_cash_pct=return_on_cash,
        realization_status=status,
    )


def list_participation_summaries(
    session: Session,
    *,
    ipo_id: Optional[int] = None,
) -> list[ParticipationSummary]:
    stmt = select(UserParticipation).order_by(
        UserParticipation.decision_at,
        UserParticipation.id,
    )
    if ipo_id is not None:
        stmt = stmt.where(UserParticipation.ipo_id == ipo_id)
    return [summarize_participation(row) for row in session.execute(stmt).scalars()]


def summarize_participation_portfolio(
    summaries: list[ParticipationSummary],
) -> ParticipationPortfolioSummary:
    realized = [row for row in summaries if row.net_realized_profit is not None]
    cash = sum(row.cash_committed for row in realized)
    net = sum(row.net_realized_profit for row in realized) if realized else None
    gross = sum(row.gross_realized_profit for row in realized) if realized else None
    return ParticipationPortfolioSummary(
        participation_count=len(summaries),
        realized_sales_count=len(realized),
        fully_realized_count=sum(row.realization_status == "realized" for row in summaries),
        gross_realized_profit=gross,
        net_realized_profit=net,
        realized_cash_committed=cash,
        realized_return_on_committed_cash_pct=(
            net / cash * 100 if net is not None and cash else None
        ),
    )


def _validate_participation(
    ipo: Ipo,
    broker: str,
    decision_action: str,
    user_cash: int,
    applied_shares: int,
    cash_committed: int,
    allocated_shares: Optional[int],
    subscription_fee: int,
    score: Optional[ScoreSnapshot],
) -> None:
    if not broker:
        raise ValueError("broker_name is required")
    available_brokers = {row.broker_name for row in ipo.underwriters}
    if broker not in available_brokers:
        raise ValueError(f"Broker not found for IPO: {broker}")
    if decision_action.strip().upper() not in DECISION_ACTIONS:
        raise ValueError("decision_action is invalid")
    if user_cash <= 0 or applied_shares <= 0 or cash_committed <= 0:
        raise ValueError("cash and applied shares must be positive")
    if cash_committed > user_cash:
        raise ValueError("cash_committed cannot exceed user_cash")
    if allocated_shares is not None and not 0 <= allocated_shares <= applied_shares:
        raise ValueError("allocated_shares must be between zero and applied_shares")
    if subscription_fee < 0:
        raise ValueError("subscription_fee must be non-negative")
    if score is not None and score.ipo_id != ipo.id:
        raise ValueError("score snapshot belongs to a different IPO")


def _validate_effective_allocation(
    allocated_shares: Optional[int],
    applied_shares: int,
    sold_shares: int,
) -> None:
    if allocated_shares is None:
        return
    if allocated_shares > applied_shares:
        raise ValueError("allocated_shares cannot exceed applied_shares")
    if allocated_shares < sold_shares:
        raise ValueError("allocated_shares cannot be lower than already sold shares")
