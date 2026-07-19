from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models import Ipo, ListingPrice, OutcomeEvaluation, ScoreSnapshot
from app.repositories import upsert_outcome_evaluation
from app.services.scoring_v3 import resolve_scoring_identity

EVALUATION_VERSION = "outcome-v3-subscription-open"


def add_business_days(start: date, days: int) -> date:
    if days < 0:
        raise ValueError("days must be non-negative")
    if days == 0:
        return start
    calendar = xcals.get_calendar("XKRX")
    end = min(start + timedelta(days=max(30, days * 4)), calendar.last_session.date())
    sessions = calendar.sessions_in_range(start, end)
    future = [stamp.date() for stamp in sessions if stamp.date() > start]
    if len(future) < days:
        raise ValueError("KRX calendar does not cover the requested date range")
    return future[days - 1]


class OutcomeEvaluationService:
    def __init__(
        self,
        *,
        model_version: Optional[str] = None,
        rules_hash: Optional[str] = None,
    ) -> None:
        self.model_version, self.rules_hash = resolve_scoring_identity(
            model_version,
            rules_hash,
        )

    def evaluate_and_save(self, session: Session, ipo: Ipo) -> OutcomeEvaluation:
        listing_price = latest_listing_price(session, ipo.id)
        score = latest_actionable_score(
            session,
            ipo.id,
            ipo.subscription_start_date,
            ipo.actual_listing_date or ipo.listing_date,
            model_version=self.model_version,
            rules_hash=self.rules_hash,
        )
        if listing_price is None:
            raise ValueError(f"Listing price is missing for {ipo.corp_name}")
        offer_price = ipo.offer_price_final
        if not offer_price:
            raise ValueError(f"Final offer price is missing for {ipo.corp_name}")

        d0_open_return = pct_return(listing_price.d0_open_price, offer_price)
        d0_close_return = pct_return(listing_price.d0_close_price, offer_price)
        d1_close_return = pct_return(listing_price.d1_close_price, offer_price)
        d3_close_return = pct_return(listing_price.d3_close_price, offer_price)
        open_to_close = pct_return(listing_price.d0_close_price, listing_price.d0_open_price)
        max_drawdown = pct_return(listing_price.d0_low_price, listing_price.d0_open_price)
        prediction = prediction_correct(score, d1_close_return)
        values = {
            "offer_price": offer_price,
            "d0_open_return_pct": d0_open_return,
            "d0_close_return_pct": d0_close_return,
            "d1_close_return_pct": d1_close_return,
            "d3_close_return_pct": d3_close_return,
            "open_to_d0_close_return_pct": open_to_close,
            "max_drawdown_d0_pct": max_drawdown,
            "estimated_profit_per_share_open": profit_per_share(
                listing_price.d0_open_price, offer_price
            ),
            "estimated_profit_per_share_d1": profit_per_share(
                listing_price.d1_close_price, offer_price
            ),
            "estimated_profit_per_share_d3": profit_per_share(
                listing_price.d3_close_price, offer_price
            ),
            "label_open_success": success_label(d0_open_return),
            "label_d1_success": success_label(d1_close_return),
            "label_d3_success": success_label(d3_close_return),
            "prediction_was_correct": prediction,
            "error_reason": None
            if prediction is not None
            else "non-directional band or insufficient data",
            "evaluated_at": datetime.now(timezone.utc),
        }
        evaluation = upsert_outcome_evaluation(
            session,
            ipo_id=ipo.id,
            score_snapshot_id=score.id if score else None,
            evaluation_version=EVALUATION_VERSION,
            values=values,
        )
        # Repository upserts intentionally ignore None values. Clear a prior
        # directional label when a safety-only/non-directional band is reevaluated.
        evaluation.prediction_was_correct = prediction
        evaluation.error_reason = values["error_reason"]
        if d3_close_return is not None:
            ipo.status = "evaluated"
            ipo.status_changed_at = datetime.now(timezone.utc)
        return evaluation


def pct_return(price: Optional[float], base: Optional[float]) -> Optional[float]:
    if price is None or base is None or base == 0:
        return None
    return (float(price) / float(base) - 1) * 100


def profit_per_share(price: Optional[float], offer_price: Optional[float]) -> Optional[float]:
    if price is None or offer_price is None:
        return None
    return float(price) - float(offer_price)


def prediction_correct(
    score: Optional[ScoreSnapshot], d1_close_return_pct: Optional[float]
) -> Optional[bool]:
    if score is None or d1_close_return_pct is None:
        return None
    if score.recommendation_band in {"Strong", "Watch"}:
        return d1_close_return_pct > 0
    if score.recommendation_band == "Avoid":
        return d1_close_return_pct <= 0
    return None


def success_label(return_pct: Optional[float]) -> Optional[bool]:
    return return_pct > 0 if return_pct is not None else None


def latest_listing_price(session: Session, ipo_id: int) -> Optional[ListingPrice]:
    stmt = (
        select(ListingPrice)
        .where(ListingPrice.ipo_id == ipo_id)
        .order_by(desc(ListingPrice.updated_at), desc(ListingPrice.id))
    )
    return session.execute(stmt).scalars().first()


def latest_pre_listing_score(
    session: Session,
    ipo_id: int,
    listing_date: Optional[date],
    *,
    model_version: Optional[str] = None,
    rules_hash: Optional[str] = None,
) -> Optional[ScoreSnapshot]:
    return _latest_score_before(
        session,
        ipo_id,
        listing_date,
        model_version=model_version,
        rules_hash=rules_hash,
    )


def latest_actionable_score(
    session: Session,
    ipo_id: int,
    subscription_start_date: Optional[date],
    listing_date: Optional[date],
    *,
    model_version: Optional[str] = None,
    rules_hash: Optional[str] = None,
) -> Optional[ScoreSnapshot]:
    """Use a score available before subscription opens from one exact lineage.

    The safe default is the active v3 configuration. Historical callers can select a
    legacy lineage by explicitly providing both its model version and rules hash.
    """
    active_model, active_rules_hash = resolve_scoring_identity(model_version, rules_hash)
    return _latest_score_before(
        session,
        ipo_id,
        subscription_start_date or listing_date,
        model_version=active_model,
        rules_hash=active_rules_hash,
    )


def _latest_score_before(
    session: Session,
    ipo_id: int,
    cutoff_date: Optional[date],
    *,
    model_version: Optional[str] = None,
    rules_hash: Optional[str] = None,
) -> Optional[ScoreSnapshot]:
    if rules_hash is not None and model_version is None:
        raise ValueError("model_version is required when rules_hash is specified")
    stmt = select(ScoreSnapshot).where(ScoreSnapshot.ipo_id == ipo_id)
    if model_version is not None:
        stmt = stmt.where(ScoreSnapshot.model_version == model_version)
    if rules_hash is not None:
        stmt = stmt.where(ScoreSnapshot.rules_hash == rules_hash)
    if cutoff_date:
        market_open_kst = datetime.combine(
            cutoff_date,
            time(9, 0),
            tzinfo=ZoneInfo("Asia/Seoul"),
        )
        cutoff_utc = market_open_kst.astimezone(timezone.utc)
        stmt = stmt.where(
            ScoreSnapshot.score_date <= cutoff_date,
            ScoreSnapshot.calculated_at < cutoff_utc,
        )
    stmt = stmt.order_by(desc(ScoreSnapshot.calculated_at), desc(ScoreSnapshot.id))
    return session.execute(stmt).scalars().first()
