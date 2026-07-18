from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import median
from typing import Optional, Sequence, Tuple

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import get_settings, load_yaml_config
from app.models import Ipo, UnderwriterAllocation
from app.schemas import AllocationResult
from app.services.normalization import normalize_broker_name

DEFAULT_UNIT_RULE: Sequence[Tuple[int, int]] = (
    (100, 10),
    (500, 50),
    (1_000, 100),
    (5_000, 500),
    (10_000, 1_000),
    (10**12, 5_000),
)


@dataclass
class AllocationAssumptions:
    expected_applicants_count: int = 300_000
    proportional_competition_conservative: float = 2_000
    proportional_competition_base: float = 1_000
    proportional_competition_optimistic: float = 500
    equal_range_padding: float = 0.25
    source: str = "brokers.yaml"
    updated_at: Optional[datetime] = None


def resolve_underwriter(
    ipo: Ipo, requested_broker: Optional[str] = None
) -> Optional[UnderwriterAllocation]:
    if requested_broker:
        normalized = normalize_broker_name(requested_broker)
        matched = next(
            (item for item in ipo.underwriters if item.broker_name == normalized),
            None,
        )
        if matched is None:
            raise ValueError(f"Broker not found for IPO: {requested_broker}")
        return matched
    if len(ipo.underwriters) > 1:
        brokers = ", ".join(item.broker_name for item in ipo.underwriters)
        raise ValueError(f"Broker must be selected: {brokers}")
    return ipo.underwriters[0] if ipo.underwriters else None


def load_allocation_assumptions(
    broker_name: Optional[str] = None,
) -> tuple[AllocationAssumptions, Sequence[Tuple[int, int]]]:
    config = load_yaml_config("brokers.yaml")
    defaults = dict(config.get("defaults") or {})
    broker = dict((config.get("brokers") or {}).get(broker_name or "", {}) or {})
    merged = {**defaults, **broker}
    competitions = {
        **(defaults.get("proportional_competition") or {}),
        **(broker.get("proportional_competition") or {}),
    }
    assumptions = AllocationAssumptions(
        expected_applicants_count=int(merged.get("expected_applicants_count", 300_000)),
        proportional_competition_conservative=float(competitions.get("conservative", 2_000)),
        proportional_competition_base=float(competitions.get("base", 1_000)),
        proportional_competition_optimistic=float(competitions.get("optimistic", 500)),
        equal_range_padding=float(merged.get("equal_range_padding", 0.25)),
        updated_at=datetime.fromtimestamp(
            (get_settings().config_dir / "brokers.yaml").stat().st_mtime,
            timezone.utc,
        ),
    )
    raw_units = merged.get("subscription_units") or defaults.get("subscription_units") or []
    unit_rule = tuple((int(item["upper_bound"]), int(item["unit"])) for item in raw_units)
    return assumptions, unit_rule or DEFAULT_UNIT_RULE


def derive_allocation_assumptions(
    session: Session,
    underwriter: Optional[UnderwriterAllocation],
    history_limit: int = 20,
) -> AllocationAssumptions:
    configured, _units = load_allocation_assumptions(
        underwriter.broker_name if underwriter else None
    )
    if underwriter is None:
        return configured
    values = AllocationAssumptions(
        expected_applicants_count=underwriter.expected_applicants_count
        or configured.expected_applicants_count,
        proportional_competition_conservative=underwriter.proportional_competition_conservative
        or configured.proportional_competition_conservative,
        proportional_competition_base=underwriter.proportional_competition_base
        or configured.proportional_competition_base,
        proportional_competition_optimistic=underwriter.proportional_competition_optimistic
        or configured.proportional_competition_optimistic,
        equal_range_padding=configured.equal_range_padding,
        source=underwriter.assumption_source or configured.source,
        updated_at=underwriter.assumption_updated_at or configured.updated_at,
    )
    history = list(
        session.execute(
            select(UnderwriterAllocation)
            .where(
                UnderwriterAllocation.broker_name == underwriter.broker_name,
                UnderwriterAllocation.id != underwriter.id,
                UnderwriterAllocation.actual_proportional_competition.is_not(None),
            )
            .order_by(
                desc(UnderwriterAllocation.actual_recorded_at), desc(UnderwriterAllocation.id)
            )
            .limit(history_limit)
        ).scalars()
    )
    if len(history) < 3:
        return _apply_offer_size(values, underwriter)
    competitions = sorted(float(row.actual_proportional_competition) for row in history)
    applicants = [row.actual_applicants_count for row in history if row.actual_applicants_count]
    historical = AllocationAssumptions(
        expected_applicants_count=int(median(applicants))
        if applicants
        else values.expected_applicants_count,
        proportional_competition_conservative=_percentile(competitions, 0.75),
        proportional_competition_base=float(median(competitions)),
        proportional_competition_optimistic=_percentile(competitions, 0.25),
        equal_range_padding=values.equal_range_padding,
        source=f"historical:{underwriter.broker_name}:n={len(history)}",
        updated_at=history[0].actual_recorded_at or values.updated_at,
    )
    return _apply_offer_size(historical, underwriter)


def round_down_by_unit(
    shares: int, unit_rule: Sequence[Tuple[int, int]] = DEFAULT_UNIT_RULE
) -> int:
    if shares <= 0:
        return 0
    if not unit_rule or any(upper <= 0 or unit <= 0 for upper, unit in unit_rule):
        raise ValueError("Subscription unit rule must contain positive bounds and units")
    selected_unit = unit_rule[-1][1]
    for upper_bound, unit in unit_rule:
        if shares <= upper_bound:
            selected_unit = unit
            break
    return shares - (shares % selected_unit)


def calculate_apply_shares(
    user_cash: int,
    offer_price: int,
    deposit_rate: float,
    max_shares: Optional[int] = None,
    min_shares: int = 10,
    unit_rule: Sequence[Tuple[int, int]] = DEFAULT_UNIT_RULE,
    subscription_fee: int = 0,
) -> int:
    available_cash = user_cash - max(subscription_fee, 0)
    if available_cash <= 0 or offer_price <= 0 or not 0 < deposit_rate <= 1:
        return 0
    raw_shares = int(available_cash // (offer_price * deposit_rate))
    if max_shares is not None:
        raw_shares = min(raw_shares, max_shares)
    rounded = round_down_by_unit(raw_shares, unit_rule)
    if rounded < min_shares:
        return 0
    return rounded


def calculate_equal_expected(
    retail_allocated_shares: Optional[int],
    equal_allocation_ratio: float,
    expected_applicants_count: int,
) -> float:
    if (
        not retail_allocated_shares
        or expected_applicants_count <= 0
        or not 0 <= equal_allocation_ratio <= 1
    ):
        return 0.0
    equal_pool = retail_allocated_shares * equal_allocation_ratio
    return equal_pool / expected_applicants_count


def calculate_proportional_expected(
    apply_shares: int,
    expected_competition_ratio: float,
    proportional_allocation_ratio: float = 1.0,
) -> float:
    if apply_shares <= 0 or expected_competition_ratio <= 0:
        return 0.0
    if not 0 <= proportional_allocation_ratio <= 1:
        return 0.0
    return apply_shares * proportional_allocation_ratio / expected_competition_ratio


def build_allocation_scenario(
    ipo: Ipo,
    underwriter: Optional[UnderwriterAllocation],
    user_cash: int,
    assumptions: Optional[AllocationAssumptions] = None,
) -> AllocationResult:
    configured_assumptions, unit_rule = load_allocation_assumptions(
        underwriter.broker_name if underwriter else None
    )
    assumptions = assumptions or configured_assumptions
    offer_price = int(ipo.offer_price_final or ipo.offer_price_max or ipo.offer_price_min or 0)
    if offer_price <= 0:
        raise ValueError(f"Offer price is missing for {ipo.corp_name}")

    broker_name = underwriter.broker_name if underwriter else "미지정"
    deposit_rate = underwriter.deposit_rate if underwriter and underwriter.deposit_rate else 0.5
    min_shares = (
        underwriter.min_subscription_shares
        if underwriter and underwriter.min_subscription_shares
        else 10
    )
    max_shares = underwriter.max_subscription_shares if underwriter else None
    fee = (
        underwriter.subscription_fee
        if underwriter and underwriter.subscription_fee is not None
        else 0
    )
    proportional_ratio = (
        underwriter.proportional_allocation_ratio
        if underwriter and underwriter.proportional_allocation_ratio is not None
        else 0.5
    )

    apply_shares = calculate_apply_shares(
        user_cash=user_cash,
        offer_price=offer_price,
        deposit_rate=deposit_rate,
        max_shares=max_shares,
        min_shares=min_shares,
        unit_rule=unit_rule,
        subscription_fee=int(fee),
    )
    estimated_deposit = int(apply_shares * offer_price * deposit_rate)
    equal_mid = calculate_equal_expected(
        underwriter.retail_allocated_shares if underwriter else None,
        underwriter.equal_allocation_ratio
        if underwriter and underwriter.equal_allocation_ratio
        else 0.5,
        assumptions.expected_applicants_count,
    )
    padding = assumptions.equal_range_padding
    equal_low = max(equal_mid * (1 - padding), 0)
    equal_high = equal_mid * (1 + padding)

    proportional_conservative = calculate_proportional_expected(
        apply_shares, assumptions.proportional_competition_conservative, proportional_ratio
    )
    proportional_base = calculate_proportional_expected(
        apply_shares, assumptions.proportional_competition_base, proportional_ratio
    )
    proportional_optimistic = calculate_proportional_expected(
        apply_shares, assumptions.proportional_competition_optimistic, proportional_ratio
    )

    return AllocationResult(
        ipo_id=ipo.id,
        broker_name=broker_name,
        user_cash=user_cash,
        offer_price=offer_price,
        apply_shares=apply_shares,
        expected_equal_shares_low=equal_low,
        expected_equal_shares_mid=equal_mid,
        expected_equal_shares_high=equal_high,
        expected_proportional_shares_conservative=proportional_conservative,
        expected_proportional_shares_base=proportional_base,
        expected_proportional_shares_optimistic=proportional_optimistic,
        expected_total_shares_base=equal_mid + proportional_base,
        estimated_fee=int(fee),
        estimated_required_deposit=estimated_deposit,
        assumption_source=assumptions.source,
        assumption_updated_at=assumptions.updated_at,
    )


def equal_expectation_label(expected_equal_shares: float) -> str:
    if expected_equal_shares < 0.2:
        return "0주 가능성 높음"
    if expected_equal_shares < 0.8:
        return "0~1주 가능성"
    if expected_equal_shares < 1.2:
        return "1주 가능성"
    return "1주 이상 가능성"


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        raise ValueError("Cannot calculate a percentile from an empty list")
    position = (len(values) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    weight = position - lower
    return values[lower] * (1 - weight) + values[upper] * weight


def _apply_offer_size(
    assumptions: AllocationAssumptions,
    underwriter: UnderwriterAllocation,
) -> AllocationAssumptions:
    offer_amount = underwriter.ipo.offer_amount_million if underwriter.ipo else None
    if not offer_amount:
        return assumptions
    if offer_amount >= 100_000:
        factor, label = 1.2, "large"
    elif offer_amount < 20_000:
        factor, label = 0.8, "small"
    else:
        factor, label = 1.0, "medium"
    if factor == 1:
        return assumptions
    return AllocationAssumptions(
        expected_applicants_count=max(int(assumptions.expected_applicants_count * factor), 1),
        proportional_competition_conservative=(
            assumptions.proportional_competition_conservative * factor
        ),
        proportional_competition_base=assumptions.proportional_competition_base * factor,
        proportional_competition_optimistic=(
            assumptions.proportional_competition_optimistic * factor
        ),
        equal_range_padding=assumptions.equal_range_padding,
        source=f"{assumptions.source};offer-size:{label}",
        updated_at=assumptions.updated_at,
    )
