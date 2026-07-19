from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

from sqlalchemy.orm import Session

from app.models import Ipo, ScoreSnapshot
from app.schemas import AllocationResult
from app.services.allocation import (
    build_allocation_scenario,
    derive_allocation_assumptions,
)
from app.services.backtest import BacktestReport, build_backtest_report


@dataclass(frozen=True)
class DecisionPolicy:
    minimum_samples: int = 30
    minimum_confidence: float = 75
    minimum_positive_probability: float = 0.60
    minimum_expected_net_profit: float = 10_000
    annual_opportunity_cost_rate: float = 0.035


@dataclass(frozen=True)
class DecisionAssessment:
    action: str
    score: Optional[float]
    confidence: Optional[float]
    broker_name: Optional[str]
    expected_shares: Optional[float]
    break_even_return_pct: Optional[float]
    expected_net_profit: Optional[float]
    positive_probability: Optional[float]
    benchmark_median_return_pct: Optional[float]
    validation_sample_count: int
    validation_status: str
    reasons: tuple[str, ...]
    risks: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


def assess_ipo_decision(
    session: Session,
    ipo: Ipo,
    score: Optional[ScoreSnapshot],
    *,
    user_cash: int,
    policy: Optional[DecisionPolicy] = None,
    report: Optional[BacktestReport] = None,
) -> DecisionAssessment:
    active_policy = policy or DecisionPolicy()
    _validate_policy(active_policy, user_cash)
    if score is None:
        return _data_required("사전 점수 스냅샷이 없습니다.")
    gate = dict((score.explanation_json or {}).get("gates") or {})
    hard_block_reasons = tuple(
        str(value)
        for value in (gate.get("hard_block_reasons") or gate.get("blocked_reasons") or ())
    )
    review_reasons = tuple(
        str(value) for value in (gate.get("review_reasons") or gate.get("review_warnings") or ())
    )
    if score.recommendation_band == "Blocked" or hard_block_reasons:
        return DecisionAssessment(
            action="BLOCKED",
            score=float(score.total_score),
            confidence=float(score.confidence_score or 0),
            broker_name=None,
            expected_shares=None,
            break_even_return_pct=None,
            expected_net_profit=None,
            positive_probability=None,
            benchmark_median_return_pct=None,
            validation_sample_count=0,
            validation_status="blocked",
            reasons=("구조화된 중대 위험 gate가 우선 적용되었습니다.",),
            risks=hard_block_reasons or ("중대 위험 gate",),
        )
    if (
        score.recommendation_band == "Insufficient Data"
        or float(score.confidence_score or 0) < active_policy.minimum_confidence
    ):
        missing = tuple(str(value) for value in score.missing_features_json or ())
        return DecisionAssessment(
            action="DATA_REQUIRED",
            score=float(score.total_score),
            confidence=float(score.confidence_score or 0),
            broker_name=None,
            expected_shares=None,
            break_even_return_pct=None,
            expected_net_profit=None,
            positive_probability=None,
            benchmark_median_return_pct=None,
            validation_sample_count=0,
            validation_status="insufficient-data",
            reasons=("필수 데이터 또는 완성도 기준을 충족하지 못했습니다.",),
            risks=missing or ("데이터 완성도 부족",),
        )

    allocation = _best_allocation(session, ipo, user_cash, score)
    expected_shares = allocation.expected_total_shares_base
    cost = _estimated_cost(ipo, allocation, active_policy)
    break_even = (
        cost / (expected_shares * allocation.offer_price) * 100
        if expected_shares > 0 and allocation.offer_price > 0
        else None
    )
    if score.recommendation_band not in {"Strong", "Watch"} or expected_shares <= 0:
        reason = (
            "예상 배정주수가 0주입니다."
            if expected_shares <= 0
            else f"점수 등급이 {score.recommendation_band}입니다."
        )
        return DecisionAssessment(
            action="VALUE_SKIP",
            score=float(score.total_score),
            confidence=float(score.confidence_score or 0),
            broker_name=allocation.broker_name,
            expected_shares=expected_shares,
            break_even_return_pct=break_even,
            expected_net_profit=None,
            positive_probability=None,
            benchmark_median_return_pct=None,
            validation_sample_count=0,
            validation_status="not-eligible",
            reasons=(reason,),
            risks=review_reasons,
        )

    active_report = report or build_backtest_report(
        session,
        model_version=score.model_version,
        rules_hash=score.rules_hash,
        minimum_samples=active_policy.minimum_samples,
    )
    band = active_report.band_metrics.get(score.recommendation_band)
    sample_count = band.sample_count if band else 0
    if active_report.validation_status != "ready":
        return DecisionAssessment(
            action="RESEARCH_ONLY",
            score=float(score.total_score),
            confidence=float(score.confidence_score or 0),
            broker_name=allocation.broker_name,
            expected_shares=expected_shares,
            break_even_return_pct=break_even,
            expected_net_profit=None,
            positive_probability=None,
            benchmark_median_return_pct=None,
            validation_sample_count=sample_count,
            validation_status=active_report.validation_status,
            reasons=(f"백테스트 준비 상태가 {active_report.validation_status}입니다.",),
            risks=review_reasons,
        )
    if band is None or sample_count < active_policy.minimum_samples:
        return DecisionAssessment(
            action="RESEARCH_ONLY",
            score=float(score.total_score),
            confidence=float(score.confidence_score or 0),
            broker_name=allocation.broker_name,
            expected_shares=expected_shares,
            break_even_return_pct=break_even,
            expected_net_profit=None,
            positive_probability=None,
            benchmark_median_return_pct=None,
            validation_sample_count=sample_count,
            validation_status="insufficient-sample",
            reasons=(
                f"동일 등급의 누수 없는 검증 표본이 {sample_count}건으로 "
                f"최소 {active_policy.minimum_samples}건보다 적습니다.",
            ),
            risks=review_reasons,
        )

    median_return = band.median_d1_return_pct
    positive_probability = band.positive_probability
    expected_net_profit = (
        expected_shares * allocation.offer_price * median_return / 100 - cost
        if median_return is not None
        else None
    )
    if positive_probability is None or expected_net_profit is None:
        missing_metrics = []
        if positive_probability is None:
            missing_metrics.append("D1 양수 확률을 계산할 유효 수익률이 없습니다.")
        if expected_net_profit is None:
            missing_metrics.append("D1 중앙값 수익률이 없어 예상 순이익을 계산할 수 없습니다.")
        return DecisionAssessment(
            action="RESEARCH_ONLY",
            score=float(score.total_score),
            confidence=float(score.confidence_score or 0),
            broker_name=allocation.broker_name,
            expected_shares=expected_shares,
            break_even_return_pct=break_even,
            expected_net_profit=None,
            positive_probability=None,
            benchmark_median_return_pct=median_return,
            validation_sample_count=sample_count,
            validation_status="incomplete-outcomes",
            reasons=("검증 표본의 성과값이 불완전합니다.",),
            risks=tuple((*review_reasons, *missing_metrics)),
        )
    meets_probability = positive_probability >= active_policy.minimum_positive_probability
    meets_profit = expected_net_profit >= active_policy.minimum_expected_net_profit
    action = "CANDIDATE" if meets_probability and meets_profit else "VALUE_SKIP"
    reasons = (
        f"D1 양수 확률 {positive_probability:.1%}",
        f"D1 중앙값 기준 예상 순이익 {expected_net_profit:,.0f}원",
    )
    risks = list(review_reasons)
    if not meets_probability:
        risks.append("검증된 양수 확률이 정책 기준보다 낮습니다.")
    if not meets_profit:
        risks.append("예상 순이익이 정책 기준보다 낮습니다.")
    return DecisionAssessment(
        action=action,
        score=float(score.total_score),
        confidence=float(score.confidence_score or 0),
        broker_name=allocation.broker_name,
        expected_shares=expected_shares,
        break_even_return_pct=break_even,
        expected_net_profit=expected_net_profit,
        positive_probability=positive_probability,
        benchmark_median_return_pct=median_return,
        validation_sample_count=sample_count,
        validation_status="validated",
        reasons=reasons,
        risks=tuple(risks),
    )


def _best_allocation(
    session: Session,
    ipo: Ipo,
    user_cash: int,
    score: ScoreSnapshot,
) -> AllocationResult:
    candidates = ipo.underwriters or [None]
    scenarios = []
    for underwriter in candidates:
        assumptions = derive_allocation_assumptions(
            session,
            underwriter,
            as_of=score.calculated_at,
        )
        scenarios.append(
            build_allocation_scenario(
                ipo,
                underwriter,
                user_cash,
                assumptions=assumptions,
            )
        )
    return max(
        scenarios,
        key=lambda row: (row.expected_total_shares_base, -row.estimated_fee),
    )


def _estimated_cost(
    ipo: Ipo,
    allocation: AllocationResult,
    policy: DecisionPolicy,
) -> float:
    lock_days = 0
    release_date = ipo.refund_date or ipo.payment_date
    if release_date and ipo.subscription_end_date:
        lock_days = max((release_date - ipo.subscription_end_date).days, 0)
    opportunity_cost = (
        allocation.estimated_required_deposit
        * policy.annual_opportunity_cost_rate
        * lock_days
        / 365
    )
    return float(allocation.estimated_fee) + opportunity_cost


def _validate_policy(policy: DecisionPolicy, user_cash: int) -> None:
    if user_cash <= 0:
        raise ValueError("user_cash must be positive")
    if policy.minimum_samples <= 0:
        raise ValueError("minimum_samples must be positive")
    if not 0 <= policy.minimum_confidence <= 100:
        raise ValueError("minimum_confidence must be between 0 and 100")
    if not 0 <= policy.minimum_positive_probability <= 1:
        raise ValueError("minimum_positive_probability must be between 0 and 1")
    if policy.annual_opportunity_cost_rate < 0:
        raise ValueError("annual_opportunity_cost_rate must be non-negative")


def _data_required(reason: str) -> DecisionAssessment:
    return DecisionAssessment(
        action="DATA_REQUIRED",
        score=None,
        confidence=None,
        broker_name=None,
        expected_shares=None,
        break_even_return_pct=None,
        expected_net_profit=None,
        positive_probability=None,
        benchmark_median_return_pct=None,
        validation_sample_count=0,
        validation_status="missing-score",
        reasons=(reason,),
        risks=("데이터 부족",),
    )
