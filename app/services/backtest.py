from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean, median
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OutcomeEvaluation, ScoreSnapshot


@dataclass
class BandBacktestMetrics:
    sample_count: int
    prediction_accuracy: Optional[float]
    average_d1_return_pct: Optional[float]
    median_d1_return_pct: Optional[float]
    loss_probability: Optional[float]
    small_sample_warning: bool


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


def build_backtest_report(session: Session) -> BacktestReport:
    pairs = list(
        session.execute(
            select(OutcomeEvaluation, ScoreSnapshot).outerjoin(
                ScoreSnapshot,
                OutcomeEvaluation.score_snapshot_id == ScoreSnapshot.id,
            )
        ).all()
    )
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

    band_metrics: dict[str, BandBacktestMetrics] = {}
    for band in ("Strong", "Watch", "Neutral", "Avoid", "Insufficient Data"):
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
    )


def _band_metrics(band: str, rows: list[OutcomeEvaluation]) -> BandBacktestMetrics:
    returns = [
        float(row.d1_close_return_pct) for row in rows if row.d1_close_return_pct is not None
    ]
    if band == "Neutral":
        correct = [-5 <= value <= 5 for value in returns]
    else:
        correct = [
            row.prediction_was_correct for row in rows if row.prediction_was_correct is not None
        ]
    return BandBacktestMetrics(
        sample_count=len(rows),
        prediction_accuracy=_ratio(correct),
        average_d1_return_pct=mean(returns) if returns else None,
        median_d1_return_pct=median(returns) if returns else None,
        loss_probability=_ratio([value < 0 for value in returns]),
        small_sample_warning=len(rows) < 10,
    )


def _ratio(values: list[bool]) -> Optional[float]:
    if not values:
        return None
    return sum(bool(value) for value in values) / len(values)
