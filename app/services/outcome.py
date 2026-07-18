from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models import Ipo, ListingPrice, OutcomeEvaluation, ScoreSnapshot
from app.repositories import upsert_outcome_evaluation

EVALUATION_VERSION = "outcome-v2"


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
    def evaluate_and_save(self, session: Session, ipo: Ipo) -> OutcomeEvaluation:
        listing_price = latest_listing_price(session, ipo.id)
        score = latest_pre_listing_score(
            session, ipo.id, ipo.actual_listing_date or ipo.listing_date
        )
        if listing_price is None:
            raise ValueError(f"Listing price is missing for {ipo.corp_name}")
        offer_price = ipo.offer_price_final or ipo.offer_price_max or ipo.offer_price_min
        if not offer_price:
            raise ValueError(f"Offer price is missing for {ipo.corp_name}")

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
            "label_open_success": d0_open_return is not None and d0_open_return > 0,
            "label_d1_success": d1_close_return is not None and d1_close_return > 0,
            "label_d3_success": d3_close_return is not None and d3_close_return > 0,
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
) -> Optional[ScoreSnapshot]:
    stmt = select(ScoreSnapshot).where(ScoreSnapshot.ipo_id == ipo_id)
    if listing_date:
        market_open_kst = datetime.combine(
            listing_date,
            time(9, 0),
            tzinfo=ZoneInfo("Asia/Seoul"),
        )
        cutoff_utc = market_open_kst.astimezone(timezone.utc)
        stmt = stmt.where(
            ScoreSnapshot.score_date <= listing_date,
            ScoreSnapshot.calculated_at < cutoff_utc,
        )
    stmt = stmt.order_by(desc(ScoreSnapshot.calculated_at), desc(ScoreSnapshot.id))
    return session.execute(stmt).scalars().first()
