from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models import (
    DemandForecast,
    DiligenceReview,
    FinancialMetric,
    Ipo,
    NewsMetric,
    SourceSnapshot,
)


@dataclass(frozen=True)
class DiligenceCriteria:
    min_news_articles: int = 1
    max_age_days: int = 14


def build_diligence_report(
    session: Session,
    ipos: list[Ipo],
    criteria: Optional[DiligenceCriteria] = None,
) -> dict[str, Any]:
    criteria = criteria or DiligenceCriteria()
    rows = [_review_ipo(session, ipo, criteria) for ipo in ipos]
    return {
        "criteria": {
            "min_news_articles": criteria.min_news_articles,
            "max_age_days": criteria.max_age_days,
        },
        "summary": {
            "total": len(rows),
            "completed": len([row for row in rows if row["completed"]]),
            "pending": len([row for row in rows if not row["completed"]]),
        },
        "items": rows,
    }


def _review_ipo(session: Session, ipo: Ipo, criteria: DiligenceCriteria) -> dict[str, Any]:
    disclosure = _review_disclosure(session, ipo, criteria)
    articles = _review_articles(session, ipo, criteria)
    completed = disclosure["passed"] and articles["passed"]
    return {
        "ipo": ipo.corp_name,
        "status": ipo.status,
        "subscription_start_date": ipo.subscription_start_date,
        "subscription_end_date": ipo.subscription_end_date,
        "completed": completed,
        "criteria": {
            "disclosure_review": disclosure,
            "article_review": articles,
        },
        "next_actions": _next_actions(disclosure, articles),
    }


def _review_disclosure(session: Session, ipo: Ipo, criteria: DiligenceCriteria) -> dict[str, Any]:
    demand = _latest(session, DemandForecast, ipo.id)
    financial = _latest_financial(session, ipo.id)
    snapshot = _latest_source_snapshot(session, ipo.id, "opendart_document")
    review = _review_record(session, ipo.id, "disclosure")
    receipt_no = ipo.dart_receipt_no or (snapshot.source_key if snapshot else None)
    source_age_days = _age_days(
        snapshot.fetched_at
        if snapshot
        else demand.updated_at
        if demand
        else financial.updated_at
        if financial
        else None
    )

    has_parsed_disclosure = bool(
        demand
        and demand.source == "opendart"
        and (
            demand.institutional_competition_ratio is not None
            or demand.lockup_commitment_ratio is not None
            or demand.final_price_position is not None
            or demand.participants_count is not None
        )
    )
    has_financial_review = bool(financial and financial.fiscal_year)
    has_raw_snapshot = bool(snapshot and snapshot.raw_text_hash)
    is_fresh = source_age_days is None or source_age_days <= criteria.max_age_days
    manually_reviewed = bool(review and review.review_status == "reviewed" and review.reviewed_at)
    passed = bool(
        receipt_no
        and is_fresh
        and manually_reviewed
        and (has_parsed_disclosure or has_financial_review or has_raw_snapshot)
    )

    missing = []
    if not ipo.dart_corp_code:
        missing.append("dart_corp_code")
    if not receipt_no:
        missing.append("dart_receipt_no_or_snapshot")
    if not (has_parsed_disclosure or has_financial_review or has_raw_snapshot):
        missing.append("parsed_or_raw_opendart_review")
    if not is_fresh:
        missing.append("fresh_opendart_review")
    if not manually_reviewed:
        missing.append("manual_disclosure_review")

    return {
        "passed": passed,
        "score": (
            100
            if passed
            else _disclosure_partial_score(
                ipo,
                receipt_no,
                has_parsed_disclosure,
                has_financial_review,
                has_raw_snapshot,
                is_fresh,
            )
        ),
        "receipt_no": receipt_no,
        "source_age_days": source_age_days,
        "has_parsed_demand_forecast": has_parsed_disclosure,
        "has_financial_review": has_financial_review,
        "has_raw_snapshot": has_raw_snapshot,
        "review_status": review.review_status if review else "not_started",
        "reviewed_by": review.reviewed_by if review else None,
        "reviewed_at": review.reviewed_at if review else None,
        "missing": missing,
    }


def _review_articles(session: Session, ipo: Ipo, criteria: DiligenceCriteria) -> dict[str, Any]:
    news = _latest(session, NewsMetric, ipo.id)
    review = _review_record(session, ipo.id, "articles")
    if not news:
        return {
            "passed": False,
            "score": 0,
            "article_count": 0,
            "source_age_days": None,
            "top_articles": [],
            "missing": ["news_metrics"],
        }

    source_age_days = _age_days(news.updated_at)
    article_count = news.deduped_article_count or 0
    top_articles = news.top_articles_json or []
    is_fresh = source_age_days is None or source_age_days <= criteria.max_age_days
    manually_reviewed = bool(review and review.review_status == "reviewed" and review.reviewed_at)
    passed = bool(
        article_count >= criteria.min_news_articles
        and top_articles
        and is_fresh
        and manually_reviewed
    )

    missing = []
    if article_count < criteria.min_news_articles:
        missing.append("minimum_article_count")
    if not top_articles:
        missing.append("top_article_links")
    if not is_fresh:
        missing.append("fresh_article_review")
    if not manually_reviewed:
        missing.append("manual_article_review")

    return {
        "passed": passed,
        "score": 100
        if passed
        else _article_partial_score(article_count, top_articles, is_fresh, criteria),
        "article_count": article_count,
        "major_media_count": news.major_media_count or 0,
        "source_age_days": source_age_days,
        "top_articles": top_articles[:5],
        "summary": news.summary,
        "review_status": review.review_status if review else "not_started",
        "reviewed_by": review.reviewed_by if review else None,
        "reviewed_at": review.reviewed_at if review else None,
        "missing": missing,
    }


def _next_actions(disclosure: dict[str, Any], articles: dict[str, Any]) -> list[str]:
    actions = []
    if not disclosure["passed"]:
        actions.append(
            "run collect-dart and collect-financials, then review extracted OpenDART evidence"
        )
    if not articles["passed"]:
        actions.append("run collect-news-trend and verify top article links")
    return actions


def _latest(session: Session, model, ipo_id: int):
    stmt = (
        select(model).where(model.ipo_id == ipo_id).order_by(desc(model.updated_at), desc(model.id))
    )
    return session.execute(stmt).scalars().first()


def _latest_financial(session: Session, ipo_id: int):
    stmt = (
        select(FinancialMetric)
        .where(FinancialMetric.ipo_id == ipo_id)
        .order_by(
            desc(FinancialMetric.fiscal_year),
            desc(FinancialMetric.updated_at),
            desc(FinancialMetric.id),
        )
    )
    return session.execute(stmt).scalars().first()


def _latest_source_snapshot(session: Session, ipo_id: int, source: str):
    stmt = (
        select(SourceSnapshot)
        .where(SourceSnapshot.ipo_id == ipo_id, SourceSnapshot.source == source)
        .order_by(desc(SourceSnapshot.fetched_at), desc(SourceSnapshot.id))
    )
    return session.execute(stmt).scalars().first()


def _review_record(session: Session, ipo_id: int, review_type: str):
    return (
        session.execute(
            select(DiligenceReview).where(
                DiligenceReview.ipo_id == ipo_id,
                DiligenceReview.review_type == review_type,
            )
        )
        .scalars()
        .first()
    )


def _age_days(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return max((datetime.now(timezone.utc) - value).days, 0)


def _disclosure_partial_score(
    ipo: Ipo,
    receipt_no: Optional[str],
    has_parsed_disclosure: bool,
    has_financial_review: bool,
    has_raw_snapshot: bool,
    is_fresh: bool,
) -> int:
    score = 0
    if ipo.dart_corp_code:
        score += 20
    if receipt_no:
        score += 25
    if has_raw_snapshot:
        score += 20
    if has_parsed_disclosure:
        score += 25
    if has_financial_review:
        score += 10
    if score and not is_fresh:
        score = min(score, 80)
    return min(score, 98)


def _article_partial_score(
    article_count: int,
    top_articles: list[dict[str, Any]],
    is_fresh: bool,
    criteria: DiligenceCriteria,
) -> int:
    score = 0
    if article_count > 0:
        score += 45
    if article_count >= criteria.min_news_articles:
        score += 25
    if top_articles:
        score += 20
    if is_fresh:
        score += 10
    return min(score, 98)
