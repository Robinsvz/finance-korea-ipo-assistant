from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timezone
from math import ceil, sqrt
from statistics import mean, median
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Ipo, OutcomeEvaluation, ScoreSnapshot
from app.services.scoring_v3 import resolve_scoring_identity
from app.services.validation import (
    DataReadiness,
    assess_data_readiness,
    is_prelisting_score,
    is_validation_eligible_score,
)


@dataclass
class BandBacktestMetrics:
    sample_count: int
    prediction_accuracy: Optional[float]
    average_d1_return_pct: Optional[float]
    median_d1_return_pct: Optional[float]
    loss_probability: Optional[float]
    small_sample_warning: bool
    positive_probability: Optional[float] = None
    positive_probability_ci_low: Optional[float] = None
    positive_probability_ci_high: Optional[float] = None


@dataclass
class StrategyBacktestMetrics:
    sample_count: int
    average_return_pct: Optional[float]
    median_return_pct: Optional[float]
    positive_probability: Optional[float]
    positive_probability_ci_low: Optional[float]
    positive_probability_ci_high: Optional[float]
    small_sample_warning: bool


@dataclass
class FeatureBacktestMetrics:
    sample_count: int
    spearman_d0_open: Optional[float]
    spearman_d1_close: Optional[float]
    top_third_d1_positive_probability: Optional[float]
    bottom_third_d1_positive_probability: Optional[float]


@dataclass
class BacktestReport:
    evaluated_count: int
    prediction_accuracy: Optional[float]
    average_open_return_pct: Optional[float]
    average_d1_return_pct: Optional[float]
    strong_watch_accuracy: Optional[float]
    avoid_accuracy: Optional[float]
    neutral_accuracy: Optional[float] = None
    median_open_return_pct: Optional[float] = None
    median_d1_return_pct: Optional[float] = None
    loss_probability: Optional[float] = None
    band_metrics: dict[str, BandBacktestMetrics] = field(default_factory=dict)
    strategy_metrics: dict[str, StrategyBacktestMetrics] = field(default_factory=dict)
    feature_metrics: dict[str, FeatureBacktestMetrics] = field(default_factory=dict)
    model_versions: tuple[str, ...] = ()
    rules_hashes: tuple[str, ...] = ()
    selected_model_version: Optional[str] = None
    selected_rules_hash: Optional[str] = None
    minimum_samples: int = 30
    validation_status: str = "insufficient-sample"
    gross_returns_only: bool = True


@dataclass
class GrossOutcomeSummary:
    ipo_count: int
    d0_open: StrategyBacktestMetrics
    d1_close: StrategyBacktestMetrics


@dataclass
class OperationalBacktest:
    model_version: str
    rules_hash: str
    validation: BacktestReport
    readiness: DataReadiness
    gross_outcomes: GrossOutcomeSummary


def build_backtest_report(
    session: Session,
    *,
    model_version: Optional[str] = None,
    rules_hash: Optional[str] = None,
    minimum_samples: int = 30,
    validation_eligible_only: bool = False,
) -> BacktestReport:
    if minimum_samples <= 0:
        raise ValueError("minimum_samples must be positive")
    all_pairs = list(
        session.execute(
            select(OutcomeEvaluation, ScoreSnapshot, Ipo)
            .outerjoin(
                ScoreSnapshot,
                OutcomeEvaluation.score_snapshot_id == ScoreSnapshot.id,
            )
            .join(Ipo, OutcomeEvaluation.ipo_id == Ipo.id)
            .where(Ipo.is_sample.is_(False))
        ).all()
    )
    candidates = []
    for evaluation, score, ipo in all_pairs:
        if model_version is not None and (score is None or score.model_version != model_version):
            continue
        if rules_hash is not None and (score is None or score.rules_hash != rules_hash):
            continue
        if score is not None:
            cutoff_date = ipo.subscription_start_date or ipo.actual_listing_date or ipo.listing_date
            if not is_prelisting_score(score, cutoff_date):
                continue
            if validation_eligible_only and not is_validation_eligible_score(score):
                continue
        elif validation_eligible_only:
            continue
        if validation_eligible_only and (
            evaluation.d0_open_return_pct is None or evaluation.d1_close_return_pct is None
        ):
            continue
        candidates.append((evaluation, score))
    pairs = _canonical_pairs(candidates)
    rows = [evaluation for evaluation, _score in pairs]
    directional = [
        evaluation.prediction_was_correct
        for evaluation, score in pairs
        if score is not None
        and score.recommendation_band in {"Strong", "Watch", "Avoid"}
        and evaluation.prediction_was_correct is not None
    ]
    open_returns = [
        float(row.d0_open_return_pct) for row in rows if row.d0_open_return_pct is not None
    ]
    d1_returns = [
        float(row.d1_close_return_pct) for row in rows if row.d1_close_return_pct is not None
    ]
    model_versions = tuple(sorted({score.model_version for _, score in pairs if score}))
    rules_keys = {score.rules_hash for _, score in pairs if score}
    rules_hashes = tuple(sorted(rule_hash for rule_hash in rules_keys if rule_hash))

    band_metrics: dict[str, BandBacktestMetrics] = {}
    for band in ("Strong", "Watch", "Neutral", "Avoid", "Blocked", "Insufficient Data"):
        band_rows = [
            evaluation for evaluation, score in pairs if score and score.recommendation_band == band
        ]
        band_metrics[band] = _band_metrics(band, band_rows)

    positive_values = [
        evaluation.prediction_was_correct
        for evaluation, score in pairs
        if score
        and score.recommendation_band in {"Strong", "Watch"}
        and evaluation.prediction_was_correct is not None
    ]
    avoid_values = [
        evaluation.prediction_was_correct
        for evaluation, score in pairs
        if score
        and score.recommendation_band == "Avoid"
        and evaluation.prediction_was_correct is not None
    ]
    neutral_values = [
        -5 <= float(evaluation.d1_close_return_pct) <= 5
        for evaluation, score in pairs
        if score
        and score.recommendation_band == "Neutral"
        and evaluation.d1_close_return_pct is not None
    ]
    strategy_metrics = {
        "D0_OPEN": _strategy_metrics(open_returns),
        "D1_CLOSE": _strategy_metrics(d1_returns),
    }
    feature_metrics = _feature_metrics(pairs)
    complete_count = sum(
        evaluation.d0_open_return_pct is not None and evaluation.d1_close_return_pct is not None
        for evaluation, _score in pairs
    )
    if model_version is None and len(model_versions) > 1:
        validation_status = "mixed-models"
    elif rules_hash is None and len(rules_keys) > 1:
        validation_status = "mixed-rules"
    elif complete_count >= minimum_samples:
        validation_status = "ready"
    else:
        validation_status = "insufficient-sample"
    return BacktestReport(
        evaluated_count=len(rows),
        prediction_accuracy=_ratio(directional),
        average_open_return_pct=mean(open_returns) if open_returns else None,
        average_d1_return_pct=mean(d1_returns) if d1_returns else None,
        strong_watch_accuracy=_ratio(positive_values),
        avoid_accuracy=_ratio(avoid_values),
        neutral_accuracy=_ratio(neutral_values),
        median_open_return_pct=median(open_returns) if open_returns else None,
        median_d1_return_pct=median(d1_returns) if d1_returns else None,
        loss_probability=_ratio([value < 0 for value in d1_returns]),
        band_metrics=band_metrics,
        strategy_metrics=strategy_metrics,
        feature_metrics=feature_metrics,
        model_versions=model_versions,
        rules_hashes=rules_hashes,
        selected_model_version=model_version,
        selected_rules_hash=rules_hash,
        minimum_samples=minimum_samples,
        validation_status=validation_status,
    )


def build_gross_outcome_summary(session: Session) -> GrossOutcomeSummary:
    """Build one descriptive gross outcome per real IPO, independently of model scores."""
    rows = session.execute(
        select(OutcomeEvaluation, Ipo)
        .join(Ipo, OutcomeEvaluation.ipo_id == Ipo.id)
        .where(Ipo.is_sample.is_(False))
    ).all()
    canonical: dict[int, OutcomeEvaluation] = {}
    for evaluation, _ipo in rows:
        current = canonical.get(evaluation.ipo_id)
        if current is None or _pair_order(evaluation, None) > _pair_order(current, None):
            canonical[evaluation.ipo_id] = evaluation
    outcomes = list(canonical.values())
    d0_returns = [
        float(row.d0_open_return_pct) for row in outcomes if row.d0_open_return_pct is not None
    ]
    d1_returns = [
        float(row.d1_close_return_pct) for row in outcomes if row.d1_close_return_pct is not None
    ]
    return GrossOutcomeSummary(
        ipo_count=len(outcomes),
        d0_open=_strategy_metrics(d0_returns),
        d1_close=_strategy_metrics(d1_returns),
    )


def build_operational_backtest(
    session: Session,
    *,
    minimum_samples: int = 30,
) -> OperationalBacktest:
    """Use the deployed v3 rules and the strict readiness gate for user-facing reports."""
    model_version, rules_hash = resolve_scoring_identity()
    readiness = assess_data_readiness(
        session,
        model_version=model_version,
        rules_hash=rules_hash,
        minimum_samples=minimum_samples,
    )
    validation = build_backtest_report(
        session,
        model_version=model_version,
        rules_hash=rules_hash,
        minimum_samples=minimum_samples,
        validation_eligible_only=True,
    )
    return OperationalBacktest(
        model_version=model_version,
        rules_hash=rules_hash,
        validation=validation,
        readiness=readiness,
        gross_outcomes=build_gross_outcome_summary(session),
    )


def _band_metrics(band: str, rows: list[OutcomeEvaluation]) -> BandBacktestMetrics:
    returns = [
        float(row.d1_close_return_pct) for row in rows if row.d1_close_return_pct is not None
    ]
    if band == "Neutral":
        correct = [-5 <= value <= 5 for value in returns]
    elif band == "Blocked":
        correct = []
    else:
        correct = [
            row.prediction_was_correct for row in rows if row.prediction_was_correct is not None
        ]
    positive = [value > 0 for value in returns]
    probability = _ratio(positive)
    low, high = _wilson_interval(sum(positive), len(positive))
    return BandBacktestMetrics(
        sample_count=len(returns),
        prediction_accuracy=_ratio(correct),
        average_d1_return_pct=mean(returns) if returns else None,
        median_d1_return_pct=median(returns) if returns else None,
        loss_probability=_ratio([value < 0 for value in returns]),
        small_sample_warning=len(returns) < 10,
        positive_probability=probability,
        positive_probability_ci_low=low,
        positive_probability_ci_high=high,
    )


def _ratio(values: list[bool]) -> Optional[float]:
    if not values:
        return None
    return sum(bool(value) for value in values) / len(values)


def _strategy_metrics(values: list[float]) -> StrategyBacktestMetrics:
    positive = [value > 0 for value in values]
    probability = _ratio(positive)
    low, high = _wilson_interval(sum(positive), len(positive))
    return StrategyBacktestMetrics(
        sample_count=len(values),
        average_return_pct=mean(values) if values else None,
        median_return_pct=median(values) if values else None,
        positive_probability=probability,
        positive_probability_ci_low=low,
        positive_probability_ci_high=high,
        small_sample_warning=len(values) < 30,
    )


def _feature_metrics(
    pairs: list[tuple[OutcomeEvaluation, Optional[ScoreSnapshot]]],
) -> dict[str, FeatureBacktestMetrics]:
    fields = {
        "demand": ("demand_score", "demand_forecast"),
        "supply": ("structure_score", "ipo_structure_metrics"),
        "valuation": ("valuation_score", "valuation_metrics"),
        "market": ("market_condition_score", "market_condition"),
    }
    result: dict[str, FeatureBacktestMetrics] = {}
    for name, (field_name, missing_name) in fields.items():
        rows = [
            (
                float(getattr(score, field_name) or 0),
                _optional_float(evaluation.d0_open_return_pct),
                _optional_float(evaluation.d1_close_return_pct),
            )
            for evaluation, score in pairs
            if score is not None and missing_name not in (score.missing_features_json or ())
        ]
        d0_pairs = [(feature, d0) for feature, d0, _d1 in rows if d0 is not None]
        d1_pairs = [(feature, d1) for feature, _d0, d1 in rows if d1 is not None]
        ordered = sorted(d1_pairs, key=lambda item: item[0])
        third_size = ceil(len(ordered) / 3) if ordered else 0
        bottom = ordered[:third_size]
        top = ordered[-third_size:] if third_size else []
        result[name] = FeatureBacktestMetrics(
            sample_count=len(d1_pairs),
            spearman_d0_open=_spearman(d0_pairs),
            spearman_d1_close=_spearman(d1_pairs),
            top_third_d1_positive_probability=_ratio([value > 0 for _, value in top]),
            bottom_third_d1_positive_probability=_ratio([value > 0 for _, value in bottom]),
        )
    return result


def _spearman(pairs: list[tuple[float, float]]) -> Optional[float]:
    if len(pairs) < 3:
        return None
    x_values = [pair[0] for pair in pairs]
    y_values = [pair[1] for pair in pairs]
    x_ranks = _ranks(x_values)
    y_ranks = _ranks(y_values)
    x_mean = mean(x_ranks)
    y_mean = mean(y_ranks)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_ranks, y_ranks))
    x_sum = sum((value - x_mean) ** 2 for value in x_ranks)
    y_sum = sum((value - y_mean) ** 2 for value in y_ranks)
    denominator = sqrt(x_sum * y_sum)
    return numerator / denominator if denominator else None


def _ranks(values: list[float]) -> list[float]:
    positions: dict[float, list[int]] = {}
    for index, value in enumerate(sorted(values), start=1):
        positions.setdefault(value, []).append(index)
    average_rank = {value: mean(indices) for value, indices in positions.items()}
    return [average_rank[value] for value in values]


def _wilson_interval(successes: int, total: int) -> tuple[Optional[float], Optional[float]]:
    if total == 0:
        return None, None
    z = 1.96
    proportion = successes / total
    denominator = 1 + z**2 / total
    centre = (proportion + z**2 / (2 * total)) / denominator
    margin = z * sqrt(proportion * (1 - proportion) / total + z**2 / (4 * total**2)) / denominator
    return max(centre - margin, 0), min(centre + margin, 1)


def _optional_float(value) -> Optional[float]:
    return float(value) if value is not None else None


def _canonical_pairs(
    pairs: list[tuple[OutcomeEvaluation, Optional[ScoreSnapshot]]],
) -> list[tuple[OutcomeEvaluation, Optional[ScoreSnapshot]]]:
    canonical: dict[
        tuple[int, Optional[str], Optional[str]],
        tuple[OutcomeEvaluation, Optional[ScoreSnapshot]],
    ] = {}
    for evaluation, score in pairs:
        key = (
            evaluation.ipo_id,
            score.model_version if score else None,
            score.rules_hash if score else None,
        )
        current = canonical.get(key)
        if current is None or _pair_order(evaluation, score) > _pair_order(*current):
            canonical[key] = (evaluation, score)
    return sorted(
        canonical.values(),
        key=lambda pair: (
            pair[0].ipo_id,
            pair[1].model_version if pair[1] else "",
            (pair[1].rules_hash or "") if pair[1] else "",
        ),
    )


def _pair_order(
    evaluation: OutcomeEvaluation,
    score: Optional[ScoreSnapshot],
) -> tuple:
    calculated_at = score.calculated_at if score else evaluation.evaluated_at
    if calculated_at.tzinfo is None:
        calculated_at = calculated_at.replace(tzinfo=timezone.utc)
    evaluated_at = evaluation.evaluated_at
    if evaluated_at.tzinfo is None:
        evaluated_at = evaluated_at.replace(tzinfo=timezone.utc)
    return calculated_at, evaluated_at, score.id if score and score.id else 0, evaluation.id or 0
