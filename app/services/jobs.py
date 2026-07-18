from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional, Union
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select, text
from sqlalchemy.orm import Session

from app.collectors.dart import (
    DartCollector,
    DartFinancialCollector,
    extract_demand_forecast_fields,
    summarize_financial_rows,
)
from app.collectors.kind import KindScheduleCollector
from app.collectors.naver import NaverDataLabCollector, NaverNewsCollector
from app.collectors.price import PriceCollector
from app.config import get_settings
from app.db import engine, init_db, session_scope
from app.exceptions import MissingCredentialError
from app.models import (
    FinancialMetric,
    Ipo,
    IpoStructureMetric,
    MarketConditionSnapshot,
    NewsMetric,
    OutcomeEvaluation,
    SearchTrend,
    UnderwriterAllocation,
    ValuationMetric,
)
from app.repositories import (
    build_ipo_identity,
    find_ipo,
    get_ipo_or_raise,
    list_active_ipos,
    list_evaluation_due_ipos,
    list_ipos_between,
    list_price_due_ipos,
    refresh_ipo_statuses,
    save_source_snapshot,
    upsert_demand_forecast,
    upsert_diligence_review,
    upsert_financial_metric,
    upsert_financial_statement,
    upsert_listing_price,
    upsert_news_metric,
    upsert_schedule_record,
    upsert_search_trend,
)
from app.schemas import HealthStatus
from app.services.backtest import build_backtest_report
from app.services.clock import local_today
from app.services.diligence import DiligenceCriteria, build_diligence_report
from app.services.formatter import format_backtest, format_schedule
from app.services.normalization import normalize_broker_name
from app.services.outcome import OutcomeEvaluationService
from app.services.scoring import IpoScoringService
from app.services.telegram import send_telegram_message


def healthcheck() -> HealthStatus:
    settings = get_settings()
    database_ok = False
    schema_revision = None
    expected_revision = None
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        config = Config(str(settings.project_root / "alembic.ini"))
        expected_revision = ScriptDirectory.from_config(config).get_current_head()
    except Exception:
        expected_revision = None
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            database_ok = True
            try:
                schema_revision = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one_or_none()
            except Exception:
                schema_revision = None
    except Exception:
        database_ok = False
    return HealthStatus(
        database_ok=database_ok,
        schema_current=bool(schema_revision and schema_revision == expected_revision),
        schema_revision=schema_revision,
        expected_schema_revision=expected_revision,
        telegram_configured=settings.telegram_configured,
        dart_configured=settings.dart_configured,
        naver_configured=settings.naver_configured,
        database_url=_safe_database_url(settings.database_url),
    )


def refresh_statuses(today: Optional[date] = None) -> dict[str, int]:
    init_db()
    with session_scope() as session:
        return refresh_ipo_statuses(session, today)


def collect_kind_schedule(
    start: Optional[date] = None, end: Optional[date] = None
) -> dict[str, Any]:
    init_db()
    collector = KindScheduleCollector()
    records, raw = collector.collect_with_raw(start=start, end=end)
    with session_scope() as session:
        snapshot = save_source_snapshot(
            session,
            "kind",
            f"{start or 'default'}:{end or 'default'}",
            raw,
            payload_json={"record_count": len(records)},
        )
        ipos = [upsert_schedule_record(session, record) for record in records]
        return {
            "records": len(records),
            "ipos": len({ipo.id for ipo in ipos}),
            "snapshot_hash": snapshot.raw_text_hash,
        }


def score_all_active() -> dict[str, int]:
    init_db()
    service = IpoScoringService()
    with session_scope() as session:
        refresh_ipo_statuses(session)
        ipos = list_active_ipos(session)
        for ipo in ipos:
            service.score_and_save(session, ipo)
        return {"scored": len(ipos)}


def score_one(name: Union[str, int]) -> dict[str, Any]:
    init_db()
    service = IpoScoringService()
    with session_scope() as session:
        ipo = _ipo_or_raise(session, name)
        snapshot = service.score_and_save(session, ipo)
        return {
            "ipo": ipo.corp_name,
            "score": snapshot.total_score,
            "band": snapshot.recommendation_band,
            "confidence": snapshot.confidence_score,
            "model_version": snapshot.model_version,
        }


def review_diligence(
    name: Optional[str] = None,
    min_news_articles: int = 1,
    max_age_days: int = 14,
) -> dict[str, Any]:
    init_db()
    criteria = DiligenceCriteria(min_news_articles=min_news_articles, max_age_days=max_age_days)
    with session_scope() as session:
        ipos = [_ipo_or_raise(session, name)] if name else list_active_ipos(session)
        return build_diligence_report(session, ipos, criteria)


def mark_diligence_review(
    name: str,
    review_type: str,
    reviewed_by: str,
    note: str = "",
    evidence_refs: Optional[list[str]] = None,
) -> dict[str, Any]:
    if review_type not in {"disclosure", "articles"}:
        raise ValueError("review_type must be disclosure or articles")
    if not reviewed_by.strip():
        raise ValueError("reviewed_by is required")
    with session_scope() as session:
        ipo = _ipo_or_raise(session, name)
        row = upsert_diligence_review(
            session,
            ipo_id=ipo.id,
            review_type=review_type,
            values={
                "review_status": "reviewed",
                "reviewed_by": reviewed_by.strip(),
                "reviewed_at": datetime.now(timezone.utc),
                "evidence_refs_json": evidence_refs or [],
                "review_note": note,
            },
        )
        return {"ipo": ipo.corp_name, "review_type": row.review_type, "status": row.review_status}


def collect_dart_for_ipo(name: Union[str, int]) -> dict[str, Any]:
    init_db()
    try:
        collector = DartCollector()
    except MissingCredentialError as exc:
        return {"skipped": True, "reason": str(exc)}
    with session_scope() as session:
        ipo = _ipo_or_raise(session, name)
        corp_code = ipo.dart_corp_code or collector.find_corp_code(
            ipo.corp_name,
            stock_code=ipo.ticker,
        )
        if not corp_code:
            ipo.needs_review = True
            return {
                "ipo": ipo.corp_name,
                "updated": False,
                "reason": "DART corp_code is ambiguous or missing",
            }
        ipo.dart_corp_code = corp_code
        filings = collector.search_ipo_related_filings(corp_code)
        if not filings:
            return {
                "ipo": ipo.corp_name,
                "updated": False,
                "reason": "IPO related filing not found",
            }
        latest = filings[0]
        receipt_no = str(latest.get("rcept_no") or "")
        if not receipt_no:
            return {"ipo": ipo.corp_name, "updated": False, "reason": "receipt_no missing"}
        text_body = collector.fetch_document_text(receipt_no)
        fields = extract_demand_forecast_fields(text_body)
        digest = hashlib.sha256(text_body.encode("utf-8", errors="ignore")).hexdigest()
        ipo.dart_receipt_no = receipt_no
        ipo.identity_key = build_ipo_identity(ipo)
        if fields.get("offer_price_final") is not None:
            ipo.offer_price_final = fields["offer_price_final"]
        published_at = _dart_receipt_datetime(latest.get("rcept_dt"))
        values = {
            "institutional_competition_ratio": fields.get("institutional_competition_ratio"),
            "lockup_commitment_ratio": fields.get("lockup_commitment_ratio"),
            "above_band_participation_ratio": fields.get("above_band_participation_ratio"),
            "final_price_position": _final_price_position(
                fields.get("offer_price_final"),
                ipo.offer_price_min,
                ipo.offer_price_max,
            ),
            "participants_count": fields.get("participants_count"),
            "result_published_at": published_at,
            "source": "opendart",
            "raw_text_hash": digest,
            "evidence_json": fields.get("evidence") or {},
            "parser_version": fields.get("parser_version") or "dart-tables-v1",
        }
        demand = upsert_demand_forecast(
            session,
            ipo_id=ipo.id,
            receipt_no=receipt_no,
            values=values,
        )
        save_source_snapshot(
            session,
            "opendart_document",
            receipt_no,
            text_body[:1_000_000],
            ipo_id=ipo.id,
            payload_json={"filing": latest, "extracted": fields},
        )
        upsert_diligence_review(
            session,
            ipo_id=ipo.id,
            review_type="disclosure",
            values={
                "review_status": "collected",
                "evidence_refs_json": [f"opendart:{receipt_no}"],
            },
        )
        return {
            "ipo": ipo.corp_name,
            "updated": True,
            "receipt_no": demand.receipt_no,
            "fields": fields,
        }


def collect_financials_for_ipo(name: Union[str, int], years: int = 3) -> dict[str, Any]:
    init_db()
    if years <= 0 or years > 10:
        raise ValueError("years must be between 1 and 10")
    try:
        collector = DartFinancialCollector()
    except MissingCredentialError as exc:
        return {"skipped": True, "reason": str(exc)}
    with session_scope() as session:
        ipo = _ipo_or_raise(session, name)
        corp_code = ipo.dart_corp_code or collector.find_corp_code(
            ipo.corp_name, stock_code=ipo.ticker
        )
        if not corp_code:
            return {"ipo": ipo.corp_name, "updated": False, "reason": "DART corp_code not found"}
        ipo.dart_corp_code = corp_code
        current_year = local_today().year - 1
        rows = collector.fetch_single_company_financials(
            corp_code,
            range(current_year - years + 1, current_year + 1),
        )
        for row in rows:
            upsert_financial_statement(
                session,
                ipo_id=ipo.id,
                values={
                    "fiscal_year": row.get("fiscal_year"),
                    "statement_type": row.get("fs_div") or row.get("sj_div"),
                    "account_name": row.get("account_nm"),
                    "account_code": row.get("account_id") or row.get("account_nm"),
                    "amount": _amount(row.get("thstrm_amount")),
                    "currency": row.get("currency", "KRW"),
                    "source": "opendart",
                },
            )
        metrics = summarize_financial_rows(rows)
        for metric in metrics:
            upsert_financial_metric(
                session,
                ipo_id=ipo.id,
                values={**_financial_metric_fields(metric), "source": "opendart"},
            )
        return {"ipo": ipo.corp_name, "statements": len(rows), "metrics": len(metrics)}


def collect_news_trend_for_ipo(name: Union[str, int]) -> dict[str, Any]:
    init_db()
    try:
        news_collector = NaverNewsCollector()
        trend_collector = NaverDataLabCollector()
    except MissingCredentialError as exc:
        return {"skipped": True, "reason": str(exc)}
    with session_scope() as session:
        ipo = _ipo_or_raise(session, name)
        ipo_id = ipo.id
        corp_name = ipo.corp_name
    result: dict[str, Any] = {"ipo": corp_name, "news": None, "trend": None, "errors": []}
    try:
        news_payload = news_collector.collect(corp_name)
        with session_scope() as session:
            upsert_news_metric(session, ipo_id=ipo_id, values=news_payload.__dict__)
            upsert_diligence_review(
                session,
                ipo_id=ipo_id,
                review_type="articles",
                values={"review_status": "collected", "evidence_refs_json": ["naver_news"]},
            )
        result["news"] = news_payload.deduped_article_count
    except Exception as exc:
        result["errors"].append(f"news: {type(exc).__name__}: {exc}")
    try:
        trend_payload = trend_collector.collect(corp_name, [corp_name, f"{corp_name} 공모주"])
        with session_scope() as session:
            upsert_search_trend(session, ipo_id=ipo_id, values=trend_payload.__dict__)
        result["trend"] = trend_payload.trend_growth_rate
    except Exception as exc:
        result["errors"].append(f"trend: {type(exc).__name__}: {exc}")
    result["updated"] = result["news"] is not None or result["trend"] is not None
    return result


def collect_prices_for_ipo(name: Union[str, int]) -> dict[str, Any]:
    init_db()
    collector = PriceCollector()
    with session_scope() as session:
        ipo = _ipo_or_raise(session, name)
        if not ipo.ticker:
            return {"ipo": ipo.corp_name, "updated": False, "reason": "ticker missing"}
        if not ipo.listing_date:
            return {"ipo": ipo.corp_name, "updated": False, "reason": "listing_date missing"}
        payload = collector.collect_listing_prices(ipo.ticker, ipo.listing_date)
        listing = upsert_listing_price(session, ipo_id=ipo.id, values=payload.__dict__)
        if payload.d0_trade_date:
            ipo.actual_listing_date = payload.d0_trade_date
        return {
            "ipo": ipo.corp_name,
            "updated": True,
            "source": listing.source,
            "d0_trade_date": listing.d0_trade_date,
        }


def collect_market_condition(as_of_date: Optional[date] = None) -> dict[str, Any]:
    init_db()
    as_of_date = as_of_date or local_today()
    payload = None
    price_error = None
    try:
        payload = PriceCollector().collect_market_condition(as_of_date)
    except Exception as exc:
        price_error = f"{type(exc).__name__}: {exc}"
    with session_scope() as session:
        outcomes = list(
            session.execute(
                select(OutcomeEvaluation)
                .where(OutcomeEvaluation.d1_close_return_pct.is_not(None))
                .order_by(desc(OutcomeEvaluation.evaluated_at))
                .limit(20)
            ).scalars()
        )
        success_rate = (
            sum(float(row.d1_close_return_pct) > 0 for row in outcomes) / len(outcomes)
            if outcomes
            else None
        )
        if payload is None and success_rate is None:
            return {"updated": False, "reason": price_error or "market data unavailable"}
        row = (
            session.execute(
                select(MarketConditionSnapshot).where(
                    MarketConditionSnapshot.as_of_date == as_of_date
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            row = MarketConditionSnapshot(as_of_date=as_of_date)
            session.add(row)
        if payload is not None:
            if payload.kosdaq_momentum_20d is not None:
                row.kosdaq_momentum_20d = payload.kosdaq_momentum_20d
            if payload.volatility_20d is not None:
                row.volatility_20d = payload.volatility_20d
            row.source = payload.source
        if success_rate is not None:
            row.recent_ipo_success_rate = success_rate
        elif payload is None:
            row.source = row.source or "outcomes-only"
        return {
            "updated": True,
            "as_of_date": as_of_date,
            "kosdaq_momentum_20d": row.kosdaq_momentum_20d,
            "recent_ipo_success_rate": success_rate,
            "warning": price_error,
        }


def evaluate_outcome_for_ipo(name: Union[str, int]) -> dict[str, Any]:
    init_db()
    with session_scope() as session:
        ipo = _ipo_or_raise(session, name)
        evaluation = OutcomeEvaluationService().evaluate_and_save(session, ipo)
        return {
            "ipo": ipo.corp_name,
            "prediction_was_correct": evaluation.prediction_was_correct,
            "evaluation_version": evaluation.evaluation_version,
        }


def record_allocation_actual(
    name: str,
    broker_name: str,
    applicants_count: int,
    proportional_competition: float,
    source: str = "manual",
) -> dict[str, Any]:
    if applicants_count <= 0 or proportional_competition <= 0:
        raise ValueError("allocation actuals must be positive")
    normalized = normalize_broker_name(broker_name)
    with session_scope() as session:
        ipo = _ipo_or_raise(session, name)
        row = (
            session.execute(
                select(UnderwriterAllocation).where(
                    UnderwriterAllocation.ipo_id == ipo.id,
                    UnderwriterAllocation.broker_name == normalized,
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            raise ValueError(f"Broker not found for IPO: {broker_name}")
        row.actual_applicants_count = applicants_count
        row.actual_proportional_competition = proportional_competition
        row.actual_source = source
        row.actual_recorded_at = datetime.now(timezone.utc)
        return {
            "ipo": ipo.corp_name,
            "broker": row.broker_name,
            "applicants_count": applicants_count,
            "proportional_competition": proportional_competition,
            "source": source,
        }


def collect_dart_for_active() -> dict[str, Any]:
    return _run_for_selected(list_active_ipos, collect_dart_for_ipo)


def collect_financials_for_active() -> dict[str, Any]:
    return _run_for_selected(list_active_ipos, collect_financials_for_ipo)


def collect_news_for_active() -> dict[str, Any]:
    return _run_for_selected(list_active_ipos, collect_news_trend_for_ipo)


def collect_prices_due() -> dict[str, Any]:
    return _run_for_selected(list_price_due_ipos, collect_prices_for_ipo)


def evaluate_outcomes_due() -> dict[str, Any]:
    return _run_for_selected(list_evaluation_due_ipos, evaluate_outcome_for_ipo)


def send_daily_summary() -> dict[str, Any]:
    init_db()
    if not get_settings().telegram_configured:
        return {"sent": False, "skipped": True, "reason": "Telegram is not configured"}
    today = local_today()
    with session_scope() as session:
        ipos = list_ipos_between(session, today, today)
        for ipo in ipos:
            _ = ipo.underwriters
        text_body = format_schedule("오늘 공모주 일정", ipos, today, today)
    response = send_telegram_message(text_body, event_key=f"daily-summary:{today.isoformat()}")
    return {"sent": bool(response.get("ok", True)), "items": len(ipos)}


def send_weekly_summary() -> dict[str, Any]:
    init_db()
    if not get_settings().telegram_configured:
        return {"sent": False, "skipped": True, "reason": "Telegram is not configured"}
    today = local_today()
    with session_scope() as session:
        ipos = list_ipos_between(session, today, today + timedelta(days=6))
        for ipo in ipos:
            _ = ipo.underwriters
        text_body = format_schedule("이번 주 공모주", ipos, today, today + timedelta(days=6))
    response = send_telegram_message(text_body, event_key=f"weekly-summary:{today.isoformat()}")
    return {"sent": bool(response.get("ok", True)), "items": len(ipos)}


def send_monthly_backtest() -> dict[str, Any]:
    init_db()
    if not get_settings().telegram_configured:
        return {"sent": False, "skipped": True, "reason": "Telegram is not configured"}
    with session_scope() as session:
        text_body = format_backtest(build_backtest_report(session))
    month = local_today().strftime("%Y-%m")
    send_telegram_message(text_body, event_key=f"monthly-backtest:{month}")
    return {"sent": True}


def seed_sample_data() -> dict[str, Any]:
    init_db()
    with session_scope() as session:
        before = session.query(Ipo).count()
        ipo = find_ipo(session, "ABC테크")
        if ipo is None:
            ipo = Ipo(identity_key="sample:ABC테크", corp_name="ABC테크", normalized_name="abc테크")
            session.add(ipo)
            session.flush()
        ipo.market = "코스닥"
        ipo.industry = "AI 소프트웨어"
        ipo.status = "scheduled"
        ipo.offer_price_min = 18_000
        ipo.offer_price_max = 20_000
        ipo.offer_price_final = 20_000
        today = local_today()
        ipo.subscription_start_date = today + timedelta(days=5)
        ipo.subscription_end_date = today + timedelta(days=6)
        ipo.refund_date = today + timedelta(days=8)
        ipo.refund_date_source = "sample"
        ipo.refund_date_verified_at = datetime.now(timezone.utc)
        ipo.payment_date = today + timedelta(days=8)
        ipo.payment_date_source = "sample"
        ipo.payment_date_verified_at = datetime.now(timezone.utc)
        ipo.listing_date = today + timedelta(days=15)
        ipo.ticker = "123456"
        ipo.source_confidence = 1.0
        _seed_underwriter(session, ipo, "한국투자증권", 500_000)
        _seed_underwriter(session, ipo, "NH투자증권", 300_000)
        upsert_demand_forecast(
            session,
            ipo_id=ipo.id,
            receipt_no="sample-receipt",
            values={
                "institutional_competition_ratio": 1280,
                "lockup_commitment_ratio": 0.18,
                "above_band_participation_ratio": 0.55,
                "final_price_position": "top_band",
                "participants_count": 1750,
                "source": "sample",
            },
        )
        upsert_financial_metric(
            session,
            ipo_id=ipo.id,
            values={
                "fiscal_year": today.year - 1,
                "source": "sample",
                "revenue": 52_000_000_000,
                "revenue_yoy_growth": 0.42,
                "revenue_yoy_status": "calculated",
                "operating_profit": 6_300_000_000,
                "operating_margin": 0.121,
                "net_income": 4_900_000_000,
                "net_margin": 0.094,
                "total_assets": 45_000_000_000,
                "total_liabilities": 16_000_000_000,
                "total_equity": 29_000_000_000,
                "debt_ratio": 0.55,
                "operating_cash_flow": 5_000_000_000,
                "roe": 0.169,
                "roa": 0.109,
                "is_profitable": True,
                "has_negative_ocf": False,
            },
        )
        _seed_valuation_and_structure(session, ipo)
        upsert_news_metric(
            session,
            ipo_id=ipo.id,
            values={
                "source": "sample",
                "window_start": today - timedelta(days=29),
                "window_end": today,
                "api_total_count": 48,
                "total_article_count": 29,
                "deduped_article_count": 29,
                "major_media_count": 5,
                "recent_7d_article_count": 14,
                "previous_7d_article_count": 7,
                "article_growth_rate_7d": 1.0,
                "positive_keyword_count": 6,
                "negative_keyword_count": 1,
                "risk_keyword_count": 1,
                "top_keywords_json": [{"keyword": "AI", "count": 8}],
                "top_articles_json": [{"title": "ABC테크 IPO", "link": "https://example.com/abc"}],
                "summary": "최근 청약 전 관심도 상승.",
            },
        )
        upsert_search_trend(
            session,
            ipo_id=ipo.id,
            values={
                "source": "sample",
                "keyword_group": "ABC테크",
                "window_start": today - timedelta(days=29),
                "window_end": today,
                "baseline_avg_index": 18,
                "recent_avg_index": 52,
                "peak_index": 81,
                "trend_growth_rate": 1.88,
                "trend_acceleration": 10,
                "zero_data_ratio": 0.0,
                "raw_series_json": [],
            },
        )
        market = (
            session.execute(
                select(MarketConditionSnapshot).where(MarketConditionSnapshot.as_of_date == today)
            )
            .scalars()
            .first()
        )
        if market is None:
            market = MarketConditionSnapshot(as_of_date=today)
            session.add(market)
        market.kosdaq_momentum_20d = 0.08
        market.recent_ipo_success_rate = 0.65
        market.volatility_20d = 0.18
        market.source = "sample"
        session.flush()
        score = IpoScoringService().score_and_save(session, ipo)
        return {"before": before, "ipo": ipo.corp_name, "score": score.total_score}


def latest_financial_metric(session: Session, ipo_id: int):
    return (
        session.execute(
            select(FinancialMetric)
            .where(FinancialMetric.ipo_id == ipo_id)
            .order_by(desc(FinancialMetric.fiscal_year), desc(FinancialMetric.updated_at))
        )
        .scalars()
        .first()
    )


def latest_news_metric(session: Session, ipo_id: int):
    return (
        session.execute(
            select(NewsMetric)
            .where(NewsMetric.ipo_id == ipo_id)
            .order_by(desc(NewsMetric.updated_at))
        )
        .scalars()
        .first()
    )


def latest_search_trend(session: Session, ipo_id: int):
    return (
        session.execute(
            select(SearchTrend)
            .where(SearchTrend.ipo_id == ipo_id)
            .order_by(desc(SearchTrend.updated_at))
        )
        .scalars()
        .first()
    )


def latest_outcome(session: Session, ipo_id: int):
    return (
        session.execute(
            select(OutcomeEvaluation)
            .where(OutcomeEvaluation.ipo_id == ipo_id)
            .order_by(desc(OutcomeEvaluation.evaluated_at))
        )
        .scalars()
        .first()
    )


def _run_for_selected(
    selector: Callable[[Session], list[Ipo]],
    operation: Callable[[Union[str, int]], dict[str, Any]],
) -> dict[str, Any]:
    init_db()
    with session_scope() as session:
        refresh_ipo_statuses(session)
        ipo_ids = [ipo.id for ipo in selector(session)]
    return _run_operations(ipo_ids, operation)


def _run_operations(
    names: list[Union[str, int]],
    operation: Callable[[Union[str, int]], dict[str, Any]],
    max_attempts: int = 2,
) -> dict[str, Any]:
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    results = []
    failures = []
    counts = {"success": 0, "partial": 0, "failed": 0, "skipped": 0}
    retried = 0
    for name in names:
        for attempt in range(1, max_attempts + 1):
            try:
                result = operation(name)
                status = _operation_status(result)
                item = {**result, "status": status, "attempts": attempt}
                results.append(item)
                counts[status] += 1
                if status == "failed":
                    failures.append(item)
                retried += int(attempt > 1)
                break
            except Exception as exc:
                if attempt == max_attempts:
                    failure = {
                        "ipo": name,
                        "status": "failed",
                        "attempts": attempt,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    failures.append(failure)
                    results.append(failure)
                    counts["failed"] += 1
    return {
        "processed": len(names),
        **counts,
        "retried": retried,
        "results": results,
        "failures": failures,
    }


def _operation_status(result: dict[str, Any]) -> str:
    if result.get("skipped") is True:
        return "skipped"
    errors = result.get("errors") or result.get("error")
    warning = result.get("warning")
    if errors:
        return "partial" if result.get("updated") is True else "failed"
    if warning:
        return "partial" if result.get("updated") is not False else "failed"
    if result.get("updated") is False:
        return "skipped" if result.get("reason") else "failed"
    return "success"


def _ipo_or_raise(session: Session, name: Union[str, int]) -> Ipo:
    return get_ipo_or_raise(session, name)


def _dart_receipt_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.strptime(str(value), "%Y%m%d")
    except ValueError:
        return None
    return parsed.replace(tzinfo=ZoneInfo("Asia/Seoul")).astimezone(timezone.utc)


def _final_price_position(
    final_price: Optional[int],
    offer_min: Optional[int],
    offer_max: Optional[int],
) -> Optional[str]:
    if final_price is None or offer_min is None or offer_max is None:
        return None
    if final_price > offer_max:
        return "above_band"
    if final_price == offer_max:
        return "top_band"
    if final_price < offer_min:
        return "below_band"
    return "in_band"


def _amount(value: Any) -> Optional[float]:
    if value is None:
        return None
    text_value = str(value).replace(",", "").strip()
    if text_value in {"", "-"}:
        return None
    negative = text_value.startswith("(") and text_value.endswith(")")
    number = float(text_value.strip("()"))
    return -abs(number) if negative else number


def _financial_metric_fields(metric: dict[str, Any]) -> dict[str, Any]:
    keys = {
        "fiscal_year",
        "revenue",
        "revenue_yoy_growth",
        "revenue_yoy_status",
        "gross_profit",
        "gross_margin",
        "operating_profit",
        "operating_margin",
        "net_income",
        "net_margin",
        "total_assets",
        "total_liabilities",
        "total_equity",
        "debt_ratio",
        "operating_cash_flow",
        "free_cash_flow",
        "roe",
        "roa",
        "is_profitable",
        "has_negative_ocf",
    }
    return {key: metric.get(key) for key in keys if key in metric}


def _seed_underwriter(session: Session, ipo: Ipo, broker_name: str, shares: int) -> None:
    row = (
        session.execute(
            select(UnderwriterAllocation).where(
                UnderwriterAllocation.ipo_id == ipo.id,
                UnderwriterAllocation.broker_name == broker_name,
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        row = UnderwriterAllocation(ipo_id=ipo.id, broker_name=broker_name)
        session.add(row)
    row.role = "대표주관"
    row.retail_allocated_shares = shares
    row.equal_allocation_ratio = 0.5
    row.proportional_allocation_ratio = 0.5
    row.min_subscription_shares = 10
    row.max_subscription_shares = 20_000
    row.deposit_rate = 0.5
    row.subscription_fee = 2000
    row.source = "sample"


def _seed_valuation_and_structure(session: Session, ipo: Ipo) -> None:
    today = local_today()
    valuation = (
        session.execute(
            select(ValuationMetric).where(
                ValuationMetric.ipo_id == ipo.id,
                ValuationMetric.as_of_date == today,
                ValuationMetric.source == "sample",
            )
        )
        .scalars()
        .first()
    )
    if valuation is None:
        valuation = ValuationMetric(ipo_id=ipo.id, as_of_date=today, source="sample")
        session.add(valuation)
    valuation.market_cap_at_offer = 120_000_000_000
    valuation.per = 24
    valuation.psr = 2.3
    valuation.peer_median_per = 28
    valuation.peer_median_psr = 2.8
    valuation.valuation_discount_premium_ratio = -0.12
    valuation.valuation_comment = "동종 SaaS 기업 대비 소폭 할인"
    structure = (
        session.execute(
            select(IpoStructureMetric).where(
                IpoStructureMetric.ipo_id == ipo.id,
                IpoStructureMetric.as_of_date == today,
                IpoStructureMetric.source == "sample",
            )
        )
        .scalars()
        .first()
    )
    if structure is None:
        structure = IpoStructureMetric(ipo_id=ipo.id, as_of_date=today, source="sample")
        session.add(structure)
    structure.total_offer_shares = 6_000_000
    structure.new_share_ratio = 0.75
    structure.secondary_share_ratio = 0.25
    structure.free_float_ratio_on_listing = 0.28
    structure.free_float_market_cap = 33_600_000_000
    structure.vc_overhang_ratio = 0.12
    structure.use_of_proceeds_summary = "연구개발 및 글로벌 영업 투자"
    structure.risk_flags_json = []


def _safe_database_url(value: str) -> str:
    if "@" not in value or "://" not in value:
        return value
    scheme, rest = value.split("://", 1)
    authority, suffix = rest.rsplit("@", 1)
    username = authority.split(":", 1)[0]
    return f"{scheme}://{username}:***@{suffix}"
