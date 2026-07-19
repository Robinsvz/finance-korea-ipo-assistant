from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    DemandForecast,
    Ipo,
    IpoStructureMetric,
    ListingPrice,
    OutcomeEvaluation,
    ScoreSnapshot,
)
from app.services.clock import local_today

DEFAULT_MINIMUM_SAMPLES = 30
DECISION_POLICY = "subscription-open-09-kst"


@dataclass(frozen=True)
class DataReadiness:
    as_of_date: date
    ipo_count: int
    real_ipo_count: int
    sample_ipo_count: int
    listed_count: int
    ticker_count: int
    offer_price_count: int
    demand_count: int
    structure_count: int
    listing_price_count: int
    outcome_count: int
    eligible_score_count: int
    complete_pair_count: int
    minimum_samples: int
    selected_rules_hash: Optional[str]
    eligible_rules_hash_count: int
    validation_ready: bool
    blockers: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ValidationRow:
    ipo_id: int
    corp_name: str
    cohort: str
    listing_date: date
    decision_policy: str
    decision_date: date
    model_version: str
    rules_hash: Optional[str]
    total_score: float
    recommendation_band: str
    confidence_score: float
    demand_score: float
    supply_score: float
    valuation_score: float
    market_score: float
    d0_open_return_pct: Optional[float]
    d1_close_return_pct: Optional[float]
    gross_profit_per_share_open: Optional[float]
    gross_profit_per_share_d1: Optional[float]

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["listing_date"] = self.listing_date.isoformat()
        return payload


def assess_data_readiness(
    session: Session,
    *,
    model_version: str,
    rules_hash: Optional[str] = None,
    minimum_samples: int = DEFAULT_MINIMUM_SAMPLES,
    as_of_date: Optional[date] = None,
) -> DataReadiness:
    if minimum_samples <= 0:
        raise ValueError("minimum_samples must be positive")
    ipos = list(session.execute(select(Ipo).order_by(Ipo.id)).scalars())
    real_ipos = [ipo for ipo in ipos if not ipo.is_sample]
    real_ids = {ipo.id for ipo in real_ipos}
    cutoff_date = as_of_date or local_today()
    listed = [
        ipo
        for ipo in real_ipos
        if (ipo.actual_listing_date or ipo.listing_date)
        and (ipo.actual_listing_date or ipo.listing_date) <= cutoff_date
    ]
    listed_ids = {ipo.id for ipo in listed}
    demand_ids = _ipo_ids(session, DemandForecast) & real_ids
    structure_ids = _ipo_ids(session, IpoStructureMetric) & real_ids
    price_ids = _ipo_ids(session, ListingPrice) & real_ids
    outcome_ids = _ipo_ids(session, OutcomeEvaluation) & real_ids
    score_stmt = select(ScoreSnapshot).where(ScoreSnapshot.model_version == model_version)
    if rules_hash is not None:
        score_stmt = score_stmt.where(ScoreSnapshot.rules_hash == rules_hash)
    scores = list(session.execute(score_stmt).scalars())
    ipo_by_id = {ipo.id: ipo for ipo in listed}
    eligible_scores = [
        score
        for score in scores
        if score.ipo_id in ipo_by_id
        and is_prelisting_score(
            score,
            ipo_by_id[score.ipo_id].subscription_start_date
            or ipo_by_id[score.ipo_id].actual_listing_date
            or ipo_by_id[score.ipo_id].listing_date,
        )
        and is_validation_eligible_score(score)
    ]
    eligible_by_id = {score.id: score for score in eligible_scores}
    eligible_rules_hashes = {score.rules_hash for score in eligible_scores}
    evaluations = list(session.execute(select(OutcomeEvaluation)).scalars())
    complete_by_rules: dict[Optional[str], set[int]] = {}
    for row in evaluations:
        score = eligible_by_id.get(row.score_snapshot_id)
        if (
            row.ipo_id not in real_ids
            or score is None
            or row.d0_open_return_pct is None
            or row.d1_close_return_pct is None
        ):
            continue
        complete_by_rules.setdefault(score.rules_hash, set()).add(row.ipo_id)
    if rules_hash is not None:
        complete_ipo_ids = complete_by_rules.get(rules_hash, set())
    else:
        complete_ipo_ids = max(complete_by_rules.values(), key=len, default=set())
    blockers: list[str] = []
    if len(price_ids & listed_ids) < len(listed_ids):
        blockers.append("상장 종목의 티커·D0/D1 가격 백필이 완료되지 않았습니다.")
    if len(demand_ids & listed_ids) < len(listed_ids):
        blockers.append("과거 수요예측 피처 커버리지가 부족합니다.")
    if len(structure_ids & listed_ids) < len(listed_ids):
        blockers.append("과거 상장일 유통물량 피처 커버리지가 부족합니다.")
    if rules_hash is None and len(eligible_rules_hashes) > 1:
        blockers.append(
            "서로 다른 rules_hash의 점수가 혼합되어 있습니다. 검증할 규칙 해시를 지정해야 합니다."
        )
    if len(complete_ipo_ids) < minimum_samples:
        blockers.append(
            f"누수 없는 사전 점수-D0/D1 완전 표본이 {len(complete_ipo_ids)}건으로 "
            f"최소 {minimum_samples}건보다 적습니다."
        )
    return DataReadiness(
        as_of_date=cutoff_date,
        ipo_count=len(ipos),
        real_ipo_count=len(real_ipos),
        sample_ipo_count=len(ipos) - len(real_ipos),
        listed_count=len(listed),
        ticker_count=sum(bool(ipo.ticker) for ipo in listed),
        offer_price_count=sum(
            bool(ipo.offer_price_final or ipo.offer_price_max or ipo.offer_price_min)
            for ipo in listed
        ),
        demand_count=len(demand_ids & listed_ids),
        structure_count=len(structure_ids & listed_ids),
        listing_price_count=len(price_ids & listed_ids),
        outcome_count=len(outcome_ids & listed_ids),
        eligible_score_count=len(eligible_scores),
        complete_pair_count=len(complete_ipo_ids),
        minimum_samples=minimum_samples,
        selected_rules_hash=rules_hash,
        eligible_rules_hash_count=len(eligible_rules_hashes),
        validation_ready=not blockers,
        blockers=tuple(blockers),
    )


def build_validation_dataset(
    session: Session,
    *,
    model_version: str,
    rules_hash: Optional[str] = None,
) -> list[ValidationRow]:
    stmt = (
        select(OutcomeEvaluation, ScoreSnapshot, Ipo)
        .join(ScoreSnapshot, OutcomeEvaluation.score_snapshot_id == ScoreSnapshot.id)
        .join(Ipo, OutcomeEvaluation.ipo_id == Ipo.id)
        .where(ScoreSnapshot.model_version == model_version)
        .order_by(Ipo.listing_date, Ipo.id)
    )
    if rules_hash is not None:
        stmt = stmt.where(ScoreSnapshot.rules_hash == rules_hash)
    canonical: dict[
        tuple[int, str, Optional[str]], tuple[OutcomeEvaluation, ScoreSnapshot, Ipo]
    ] = {}
    for evaluation, score, ipo in session.execute(stmt).all():
        listing_date = ipo.actual_listing_date or ipo.listing_date
        decision_date = ipo.subscription_start_date or listing_date
        if (
            listing_date is None
            or decision_date is None
            or ipo.is_sample
            or not is_prelisting_score(score, decision_date)
            or not is_validation_eligible_score(score)
            or evaluation.d0_open_return_pct is None
            or evaluation.d1_close_return_pct is None
        ):
            continue
        key = (ipo.id, score.model_version, score.rules_hash)
        current = canonical.get(key)
        if current is None or _candidate_order(evaluation, score) > _candidate_order(
            current[0], current[1]
        ):
            canonical[key] = (evaluation, score, ipo)

    rows: list[ValidationRow] = []
    for evaluation, score, ipo in canonical.values():
        listing_date = ipo.actual_listing_date or ipo.listing_date
        decision_date = ipo.subscription_start_date or listing_date
        rows.append(
            ValidationRow(
                ipo_id=ipo.id,
                corp_name=ipo.corp_name,
                cohort="spac" if "스팩" in ipo.corp_name else "operating_company",
                listing_date=listing_date,
                decision_policy=DECISION_POLICY,
                decision_date=decision_date,
                model_version=score.model_version,
                rules_hash=score.rules_hash,
                total_score=float(score.total_score),
                recommendation_band=score.recommendation_band,
                confidence_score=float(score.confidence_score or 0),
                demand_score=float(score.demand_score or 0),
                supply_score=float(score.structure_score or 0),
                valuation_score=float(score.valuation_score or 0),
                market_score=float(score.market_condition_score or 0),
                d0_open_return_pct=_float(evaluation.d0_open_return_pct),
                d1_close_return_pct=_float(evaluation.d1_close_return_pct),
                gross_profit_per_share_open=_float(evaluation.estimated_profit_per_share_open),
                gross_profit_per_share_d1=_float(evaluation.estimated_profit_per_share_d1),
            )
        )
    return sorted(rows, key=lambda row: (row.listing_date, row.ipo_id, row.rules_hash or ""))


def write_validation_csv(rows: list[ValidationRow], output_path: Path) -> Path:
    if output_path.suffix.lower() != ".csv":
        raise ValueError("validation export path must end in .csv")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(ValidationRow.__dataclass_fields__)
    with output_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(row.to_dict() for row in rows)
    return output_path


def is_prelisting_score(score: ScoreSnapshot, cutoff_date: Optional[date]) -> bool:
    """Return whether a score existed before the supplied decision-date cutoff."""
    if cutoff_date is None or score.score_date > cutoff_date:
        return False
    calculated_at = score.calculated_at
    if calculated_at.tzinfo is None:
        calculated_at = calculated_at.replace(tzinfo=timezone.utc)
    cutoff = datetime.combine(
        cutoff_date,
        time(9, 0),
        tzinfo=ZoneInfo("Asia/Seoul"),
    ).astimezone(timezone.utc)
    return calculated_at < cutoff


def is_validation_eligible_score(score: ScoreSnapshot) -> bool:
    if not score.rules_hash:
        return False
    explanation = score.explanation_json or {}
    gates = explanation.get("gates") or {}
    if gates.get("status") != "pass":
        return False
    sources = explanation.get("sources") or {}
    return all(
        _source_was_available(sources.get(name), score.calculated_at)
        for name in ("demand", "supply")
    )


def _source_was_available(payload, calculated_at: datetime) -> bool:
    if not isinstance(payload, dict) or not str(payload.get("source") or "").strip():
        return False
    raw = payload.get("as_of")
    if not raw:
        return False
    try:
        if "T" not in str(raw):
            source_date = date.fromisoformat(str(raw))
            score_date = _aware_utc(calculated_at).astimezone(ZoneInfo("Asia/Seoul")).date()
            return source_date <= score_date
        source_at = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return _aware_utc(source_at) <= _aware_utc(calculated_at)


def _candidate_order(
    evaluation: OutcomeEvaluation,
    score: ScoreSnapshot,
) -> tuple[datetime, datetime, int, int]:
    return (
        _aware_utc(score.calculated_at),
        _aware_utc(evaluation.evaluated_at),
        score.id or 0,
        evaluation.id or 0,
    )


def _aware_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _ipo_ids(session: Session, model) -> set[int]:
    return set(session.execute(select(model.ipo_id).distinct()).scalars())


def _float(value) -> Optional[float]:
    return float(value) if value is not None else None
