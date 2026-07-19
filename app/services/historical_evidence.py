from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from statistics import median
from typing import Mapping, Optional, Sequence, Union
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Ipo, OutcomeEvaluation, ScoreSnapshot
from app.services.evidence import DecisionStage
from app.services.scoring_v3 import resolve_scoring_identity
from app.services.validation import is_prelisting_score, is_validation_eligible_score


class IpoCohort(str, Enum):
    OPERATING_COMPANY = "operating_company"
    SPAC = "spac"
    REIT = "reit"


class SampleTier(str, Enum):
    INSUFFICIENT = "insufficient"
    EXPLORATORY = "exploratory"
    DECISION_GRADE = "decision_grade"


@dataclass(frozen=True)
class HistoricalIpoRecord:
    ipo_id: str
    corp_name: str
    ipo_type: Optional[str]
    market: Optional[str]
    decision_stage: Union[DecisionStage, str]
    decision_cutoff: datetime
    feature_published_at: Optional[datetime]
    feature_observed_at: Optional[datetime]
    outcome_at: Optional[datetime]
    return_pct: Optional[float]
    is_sample: bool = False
    data_origin: str = "official"
    is_complete: bool = True


@dataclass(frozen=True)
class HistoricalCohortReport:
    cohort: IpoCohort
    market: Optional[str]
    decision_stage: DecisionStage
    sample_count: int
    sample_tier: SampleTier
    median_return_pct: Optional[float]
    q1_return_pct: Optional[float]
    q3_return_pct: Optional[float]
    min_return_pct: Optional[float]
    max_return_pct: Optional[float]
    positive_rate: Optional[float]
    loss_rate: Optional[float]
    positive_rate_wilson_95: tuple[Optional[float], Optional[float]]
    recommendation_eligible: bool
    validation_status: str
    included_ipo_ids: tuple[str, ...]
    excluded_counts: Mapping[str, int]


@dataclass(frozen=True)
class HistoricalReturnDistribution:
    """A gross, descriptive return distribution for one explicit scope."""

    label: str
    sample_count: int
    median_return_pct: Optional[float]
    q1_return_pct: Optional[float]
    q3_return_pct: Optional[float]
    min_return_pct: Optional[float]
    max_return_pct: Optional[float]
    loss_rate: Optional[float]


@dataclass(frozen=True)
class HistoricalEvidenceAvailability:
    """DB-backed availability contract for the monthly facts-only email.

    Gross outcomes are deliberately separated from strict point-in-time pairs.
    The former can be shown as market history; only the latter can unlock a
    future recommendation after an explicit validation approval.
    """

    as_of: datetime
    gross_outcome_count: int
    distributions: tuple[HistoricalReturnDistribution, ...]
    strict_complete_pair_count: int
    minimum_complete_pairs: int
    recommendation_eligible: bool
    validation_status: str


def build_db_historical_evidence_availability(
    session: Session,
    *,
    as_of: datetime,
    minimum_complete_pairs: int = 30,
    validation_approved: bool = False,
) -> HistoricalEvidenceAvailability:
    """Summarize honest historical availability from canonical production rows.

    ``OutcomeEvaluation`` rows are useful immediately as gross market history,
    but they are not treated as point-in-time decision evidence. Strict pairs
    additionally require one deployed-rules score whose demand/supply evidence
    existed before the subscription cutoff and a D0/D1 outcome known by
    ``as_of``.
    """
    if minimum_complete_pairs <= 0:
        raise ValueError("minimum_complete_pairs must be positive")
    cutoff = _as_utc(as_of)
    local_cutoff_date = cutoff.astimezone(ZoneInfo("Asia/Seoul")).date()
    outcome_rows = session.execute(
        select(OutcomeEvaluation, Ipo)
        .join(Ipo, OutcomeEvaluation.ipo_id == Ipo.id)
        .where(Ipo.is_sample.is_(False))
    ).all()
    canonical: dict[int, tuple[OutcomeEvaluation, Ipo]] = {}
    for outcome, ipo in outcome_rows:
        if _is_sample_origin(ipo.data_origin) or _as_utc(outcome.evaluated_at) > cutoff:
            continue
        listing_date = ipo.actual_listing_date or ipo.listing_date
        if listing_date is None or listing_date > local_cutoff_date:
            continue
        current = canonical.get(ipo.id)
        if current is None or _outcome_order(outcome) > _outcome_order(current[0]):
            canonical[ipo.id] = (outcome, ipo)

    rows = tuple(
        sorted(
            canonical.values(),
            key=lambda item: (
                item[1].actual_listing_date or item[1].listing_date,
                item[1].id,
            ),
        )
    )
    d1_rows = tuple(item for item in rows if item[0].d1_close_return_pct is not None)
    distributions = (
        _return_distribution("D0 시초 전체", rows, "d0_open_return_pct"),
        _return_distribution("D1 종가 전체", rows, "d1_close_return_pct"),
        _return_distribution("D3 종가 전체", rows, "d3_close_return_pct"),
        _return_distribution(
            "D1 종가 일반기업",
            tuple(
                item
                for item in rows
                if classify_ipo_cohort(item[1].corp_name, item[1].ipo_type)
                is IpoCohort.OPERATING_COMPANY
            ),
            "d1_close_return_pct",
        ),
        _return_distribution(
            "D1 종가 SPAC",
            tuple(
                item
                for item in rows
                if classify_ipo_cohort(item[1].corp_name, item[1].ipo_type) is IpoCohort.SPAC
            ),
            "d1_close_return_pct",
        ),
        _return_distribution(
            "D1 종가 REIT",
            tuple(
                item
                for item in rows
                if classify_ipo_cohort(item[1].corp_name, item[1].ipo_type) is IpoCohort.REIT
            ),
            "d1_close_return_pct",
        ),
        _return_distribution("D1 종가 최근 10개", d1_rows[-10:], "d1_close_return_pct"),
    )
    strict_count = _strict_complete_pair_count(session, cutoff)
    eligible = strict_count >= minimum_complete_pairs and validation_approved
    status = (
        "ready"
        if eligible
        else "validation_required"
        if strict_count >= minimum_complete_pairs
        else "insufficient_sample"
    )
    return HistoricalEvidenceAvailability(
        as_of=cutoff,
        gross_outcome_count=len(rows),
        distributions=distributions,
        strict_complete_pair_count=strict_count,
        minimum_complete_pairs=minimum_complete_pairs,
        recommendation_eligible=eligible,
        validation_status=status,
    )


def classify_ipo_cohort(corp_name: str, ipo_type: Optional[str] = None) -> IpoCohort:
    normalized = f"{ipo_type or ''} {corp_name or ''}".replace(" ", "").lower()
    if "spac" in normalized or "스팩" in normalized or "기업인수목적" in normalized:
        return IpoCohort.SPAC
    if "reit" in normalized or "리츠" in normalized or "부동산투자회사" in normalized:
        return IpoCohort.REIT
    return IpoCohort.OPERATING_COMPANY


def build_historical_cohort_report(
    target: HistoricalIpoRecord,
    candidates: Sequence[HistoricalIpoRecord],
    *,
    validation_approved: bool = False,
) -> HistoricalCohortReport:
    """Build a leak-free, like-for-like historical evidence report."""
    if target.is_sample or _is_sample_origin(target.data_origin):
        raise ValueError("sample or synthetic target cannot receive a decision cohort")
    target_stage = _coerce_stage(target.decision_stage)
    target_cohort = classify_ipo_cohort(target.corp_name, target.ipo_type)
    target_market = _normalized_market(target.market)
    identifier_counts = Counter(item.ipo_id for item in candidates)
    excluded: Counter[str] = Counter()
    accepted: list[HistoricalIpoRecord] = []

    for candidate in candidates:
        reason = _exclusion_reason(
            target,
            candidate,
            target_stage=target_stage,
            target_cohort=target_cohort,
            target_market=target_market,
            duplicate=identifier_counts[candidate.ipo_id] > 1,
        )
        if reason is not None:
            excluded[reason] += 1
            continue
        accepted.append(candidate)

    accepted.sort(key=lambda item: (_as_utc(item.decision_cutoff), item.ipo_id))
    values = [float(item.return_pct) for item in accepted if item.return_pct is not None]
    sample_count = len(values)
    tier = _sample_tier(sample_count)
    descriptive = _descriptive_statistics(values) if sample_count >= 5 else None
    positive_rate: Optional[float] = None
    loss_rate: Optional[float] = None
    wilson: tuple[Optional[float], Optional[float]] = (None, None)
    if sample_count >= 30:
        positives = sum(value > 0 for value in values)
        losses = sum(value < 0 for value in values)
        positive_rate = positives / sample_count
        loss_rate = losses / sample_count
        wilson = wilson_interval(positives, sample_count)
    eligible = sample_count >= 30 and validation_approved
    validation_status = (
        "ready"
        if eligible
        else "validation_required"
        if sample_count >= 30
        else "insufficient_sample"
    )
    return HistoricalCohortReport(
        cohort=target_cohort,
        market=target_market,
        decision_stage=target_stage,
        sample_count=sample_count,
        sample_tier=tier,
        median_return_pct=descriptive[0] if descriptive else None,
        q1_return_pct=descriptive[1] if descriptive else None,
        q3_return_pct=descriptive[2] if descriptive else None,
        min_return_pct=descriptive[3] if descriptive else None,
        max_return_pct=descriptive[4] if descriptive else None,
        positive_rate=positive_rate,
        loss_rate=loss_rate,
        positive_rate_wilson_95=wilson,
        recommendation_eligible=eligible,
        validation_status=validation_status,
        included_ipo_ids=tuple(item.ipo_id for item in accepted),
        excluded_counts=dict(sorted(excluded.items())),
    )


def wilson_interval(
    successes: int,
    total: int,
    *,
    z: float = 1.96,
) -> tuple[Optional[float], Optional[float]]:
    if total < 0:
        raise ValueError("total must be non-negative")
    if successes < 0 or successes > total:
        raise ValueError("successes must be between zero and total")
    if not math.isfinite(z) or z <= 0:
        raise ValueError("z must be a positive finite number")
    if total == 0:
        return None, None
    proportion = successes / total
    denominator = 1 + z**2 / total
    centre = (proportion + z**2 / (2 * total)) / denominator
    margin = (
        z * math.sqrt(proportion * (1 - proportion) / total + z**2 / (4 * total**2)) / denominator
    )
    return max(centre - margin, 0), min(centre + margin, 1)


def _exclusion_reason(
    target: HistoricalIpoRecord,
    candidate: HistoricalIpoRecord,
    *,
    target_stage: DecisionStage,
    target_cohort: IpoCohort,
    target_market: Optional[str],
    duplicate: bool,
) -> Optional[str]:
    if candidate.ipo_id == target.ipo_id:
        return "target_self"
    if duplicate:
        return "duplicate_ipo_id"
    if candidate.is_sample or _is_sample_origin(candidate.data_origin):
        return "sample_or_synthetic"
    if not candidate.is_complete:
        return "incomplete_record"
    if classify_ipo_cohort(candidate.corp_name, candidate.ipo_type) is not target_cohort:
        return "cohort_mismatch"
    if target_market and _normalized_market(candidate.market) != target_market:
        return "market_mismatch"
    try:
        candidate_stage = _coerce_stage(candidate.decision_stage)
    except ValueError:
        return "stage_mismatch"
    if candidate_stage is not target_stage:
        return "stage_mismatch"
    if candidate.return_pct is None or not math.isfinite(float(candidate.return_pct)):
        return "invalid_return"
    if candidate.feature_published_at is None or candidate.feature_observed_at is None:
        return "missing_feature_timestamp"
    if candidate.outcome_at is None:
        return "missing_outcome_timestamp"

    target_cutoff = _as_utc(target.decision_cutoff)
    candidate_cutoff = _as_utc(candidate.decision_cutoff)
    if candidate_cutoff >= target_cutoff:
        return "future_candidate"
    if _as_utc(candidate.feature_published_at) > candidate_cutoff:
        return "feature_published_after_candidate_cutoff"
    if _as_utc(candidate.feature_observed_at) > candidate_cutoff:
        return "feature_observed_after_candidate_cutoff"
    outcome_at = _as_utc(candidate.outcome_at)
    if outcome_at <= candidate_cutoff:
        return "invalid_temporal_order"
    if outcome_at > target_cutoff:
        return "outcome_unknown_at_target_cutoff"
    return None


def _descriptive_statistics(values: Sequence[float]) -> tuple[float, float, float, float, float]:
    ordered = sorted(values)
    return (
        float(median(ordered)),
        _quantile(ordered, 0.25),
        _quantile(ordered, 0.75),
        ordered[0],
        ordered[-1],
    )


def _quantile(ordered: Sequence[float], probability: float) -> float:
    if not ordered:
        raise ValueError("ordered values are required")
    if not 0 <= probability <= 1:
        raise ValueError("probability must be between zero and one")
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return float(ordered[lower] * (1 - weight) + ordered[upper] * weight)


def _sample_tier(sample_count: int) -> SampleTier:
    if sample_count < 5:
        return SampleTier.INSUFFICIENT
    if sample_count < 30:
        return SampleTier.EXPLORATORY
    return SampleTier.DECISION_GRADE


def _normalized_market(value: Optional[str]) -> Optional[str]:
    normalized = str(value or "").strip().upper().replace(" ", "")
    aliases = {
        "KOSDAQ": "KOSDAQ",
        "코스닥": "KOSDAQ",
        "KOSPI": "KOSPI",
        "유가증권": "KOSPI",
        "유가증권시장": "KOSPI",
    }
    return aliases.get(normalized, normalized or None)


def _coerce_stage(value: Union[DecisionStage, str]) -> DecisionStage:
    if isinstance(value, DecisionStage):
        return value
    try:
        return DecisionStage(str(value).strip().upper())
    except ValueError as exc:
        raise ValueError(f"unknown decision stage: {value}") from exc


def _is_sample_origin(value: str) -> bool:
    return str(value or "").strip().lower() in {
        "sample",
        "synthetic",
        "test",
        "dummy",
        "demo",
    }


def _return_distribution(
    label: str,
    rows: Sequence[tuple[OutcomeEvaluation, Ipo]],
    field_name: str,
) -> HistoricalReturnDistribution:
    values = [
        float(value)
        for outcome, _ipo in rows
        if (value := getattr(outcome, field_name)) is not None and math.isfinite(float(value))
    ]
    descriptive = _descriptive_statistics(values) if values else None
    return HistoricalReturnDistribution(
        label=label,
        sample_count=len(values),
        median_return_pct=descriptive[0] if descriptive else None,
        q1_return_pct=descriptive[1] if descriptive else None,
        q3_return_pct=descriptive[2] if descriptive else None,
        min_return_pct=descriptive[3] if descriptive else None,
        max_return_pct=descriptive[4] if descriptive else None,
        loss_rate=(sum(value < 0 for value in values) / len(values)) if values else None,
    )


def _strict_complete_pair_count(session: Session, as_of: datetime) -> int:
    model_version, rules_hash = resolve_scoring_identity()
    pairs = session.execute(
        select(OutcomeEvaluation, ScoreSnapshot, Ipo)
        .join(ScoreSnapshot, OutcomeEvaluation.score_snapshot_id == ScoreSnapshot.id)
        .join(Ipo, OutcomeEvaluation.ipo_id == Ipo.id)
        .where(
            Ipo.is_sample.is_(False),
            ScoreSnapshot.model_version == model_version,
            ScoreSnapshot.rules_hash == rules_hash,
        )
    ).all()
    complete_ids: set[int] = set()
    for outcome, score, ipo in pairs:
        decision_date = ipo.subscription_start_date or ipo.actual_listing_date or ipo.listing_date
        if (
            not _is_sample_origin(ipo.data_origin)
            and _as_utc(score.calculated_at) <= as_of
            and _as_utc(outcome.evaluated_at) <= as_of
            and outcome.d0_open_return_pct is not None
            and outcome.d1_close_return_pct is not None
            and is_prelisting_score(score, decision_date)
            and is_validation_eligible_score(score)
        ):
            complete_ids.add(ipo.id)
    return len(complete_ids)


def _outcome_order(outcome: OutcomeEvaluation) -> tuple[datetime, int]:
    return _as_utc(outcome.evaluated_at), outcome.id or 0


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
