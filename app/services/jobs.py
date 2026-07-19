from __future__ import annotations

import hashlib
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Optional, Union
from zoneinfo import ZoneInfo

from sqlalchemy import and_, desc, func, or_, select, text
from sqlalchemy.orm import Session

from app.collectors.dart import (
    DartCollector,
    DartFinancialCollector,
    extract_demand_forecast_fields,
    summarize_financial_rows,
)
from app.collectors.kind import KindListingCollector, KindScheduleCollector
from app.collectors.naver import NaverDataLabCollector, NaverNewsCollector
from app.collectors.price import PriceCollector
from app.config import DEFAULT_EMAIL_RECIPIENT, get_settings
from app.db import engine, init_db, session_scope
from app.exceptions import MissingCredentialError
from app.models import (
    FinancialMetric,
    Ipo,
    IpoFactObservation,
    IpoStructureMetric,
    MarketConditionSnapshot,
    NewsMetric,
    OutcomeEvaluation,
    SearchTrend,
    SourceDocument,
    UnderwriterAllocation,
    ValuationMetric,
)
from app.repositories import (
    append_fact_observation,
    append_schedule_revision,
    build_ipo_identity,
    find_ipo,
    get_ipo_or_raise,
    list_active_ipos,
    list_evaluation_due_ipos,
    list_ipos_between,
    list_monthly_universe,
    list_price_due_ipos,
    refresh_ipo_statuses,
    save_source_document,
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
from app.schemas import HealthStatus, IpoScheduleRecord, MonthlyUniverseEntry
from app.services.backtest import build_operational_backtest
from app.services.clock import local_now, local_today
from app.services.decision_email import build_period_decision_email, build_schedule_email_items
from app.services.diligence import DiligenceCriteria, build_diligence_report
from app.services.email import EmailAuditContext, EmailContent, send_email_message
from app.services.formatter import format_operational_backtest, format_schedule
from app.services.monthly_email import (
    MonthlyEmailBundle,
    build_monthly_ipo_email,
    build_single_ipo_email,
)
from app.services.normalization import normalize_broker_name
from app.services.outcome import OutcomeEvaluationService
from app.services.scoring_v3 import SimpleIpoScoringService as IpoScoringService
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
        email_configured=settings.email_configured,
        dart_configured=settings.dart_configured,
        naver_configured=settings.naver_configured,
        database_url=_safe_database_url(settings.database_url),
    )


def refresh_statuses(today: Optional[date] = None) -> dict[str, int]:
    init_db()
    with session_scope() as session:
        return refresh_ipo_statuses(session, today)


def collect_public_dart_for_current_month(
    target: Optional[date] = None,
) -> dict[str, Any]:
    """Collect official public-DART evidence for the complete calendar month.

    The browser-facing DART fallback is deliberately opt-out because it does not
    require an OpenDART API key.  Keeping the feature gate in this job makes the
    scheduler safe to run in environments that explicitly prohibit the fallback.
    """
    settings = get_settings()
    if not getattr(settings, "dart_public_fallback_enabled", True):
        return {
            "skipped": True,
            "reason": "DART public fallback is disabled",
            "success": 0,
            "partial": 0,
            "failed": 0,
        }
    from app.services.official_evidence import collect_public_dart_evidence_for_month

    effective_target = target or local_today()
    month_start, month_end = _calendar_month_bounds(effective_target)
    return collect_public_dart_evidence_for_month(month_start, month_end)


def collect_public_dart_for_subscription_d_minus_one() -> dict[str, Any]:
    """Refresh public-DART facts for the month containing tomorrow's candidates."""
    return collect_public_dart_for_current_month(local_today() + timedelta(days=1))


def reconcile_current_month_evidence(
    target: Optional[date] = None,
) -> dict[str, Any]:
    """Promote a monthly universe only after persisted official evidence passes DQ."""
    from app.services.monthly_quality import reconcile_monthly_evidence

    init_db()
    effective_target = target or local_today()
    month_start, month_end = _calendar_month_bounds(effective_target)
    with session_scope() as session:
        return reconcile_monthly_evidence(session, month_start, month_end)


def reconcile_subscription_d_minus_one_evidence() -> dict[str, Any]:
    """Reconcile the month containing tomorrow's subscription candidates."""
    return reconcile_current_month_evidence(local_today() + timedelta(days=1))


def collect_kind_schedule(
    start: Optional[date] = None, end: Optional[date] = None
) -> dict[str, Any]:
    init_db()
    collector = KindScheduleCollector()
    settings = get_settings()
    today = local_today()
    effective_start = start or today - timedelta(days=settings.kind_lookback_days)
    effective_end = end or today + timedelta(days=settings.kind_lookahead_days)
    records, raw = collector.collect_with_raw(start=effective_start, end=effective_end)
    with session_scope() as session:
        snapshot = save_source_snapshot(
            session,
            "kind",
            f"{effective_start.isoformat()}:{effective_end.isoformat()}",
            raw,
            payload_json={"record_count": len(records)},
        )
        session.flush()
        ipos = []
        for record in records:
            ipo = upsert_schedule_record(session, record)
            _persist_kind_record_evidence(session, ipo, record, snapshot)
            ipos.append(ipo)
        return {
            "records": len(records),
            "ipos": len({ipo.id for ipo in ipos}),
            "snapshot_hash": snapshot.raw_text_hash,
        }


def collect_kind_schedule_for_subscription_d_minus_one() -> dict[str, Any]:
    """Refresh the official offering schedule for tomorrow's calendar month."""
    month_start, month_end = _calendar_month_bounds(local_today() + timedelta(days=1))
    return collect_kind_schedule(month_start, month_end)


def collect_kind_listings_for_current_month(
    target: Optional[date] = None,
) -> dict[str, Any]:
    """Persist the independent official KIND new-listing roster for reconciliation."""
    init_db()
    effective_target = target or local_today()
    month_start, month_end = _calendar_month_bounds(effective_target)
    records, raw = KindListingCollector().collect_with_raw(month_start, month_end)
    manifest = [
        {
            "corp_name": record.corp_name,
            "listing_date": record.listing_date.isoformat(),
            "listing_type": record.listing_type,
            "market": record.market,
            "isur_cd": record.isur_cd,
            "bz_procs_no": record.bz_procs_no,
        }
        for record in records
    ]
    with session_scope() as session:
        snapshot = save_source_snapshot(
            session,
            "kind_listing",
            f"{month_start.isoformat()}:{month_end.isoformat()}",
            raw,
            payload_json={"record_count": len(records), "records": manifest},
        )
        session.flush()
        return {
            "month_start": month_start.isoformat(),
            "month_end": month_end.isoformat(),
            "records": len(records),
            "names": [record.corp_name for record in records],
            "snapshot_hash": snapshot.raw_text_hash,
        }


def collect_kind_listings_for_subscription_d_minus_one() -> dict[str, Any]:
    """Refresh the official listing roster for tomorrow's calendar month."""
    return collect_kind_listings_for_current_month(local_today() + timedelta(days=1))


def _persist_kind_record_evidence(
    session: Session,
    ipo: Ipo,
    record: IpoScheduleRecord,
    snapshot,
) -> None:
    """Attach one KIND row to its offering and every exposed core schedule fact."""
    observed_at = snapshot.fetched_at
    published_at = _kind_report_datetime(record.report_filed_date, observed_at)
    lineage_identity = record.kind_biz_no or ipo.offering_id or f"ipo:{ipo.id}"
    document_key = (
        f"kind:{lineage_identity}:{record.report_filed_date or 'undated'}:{snapshot.raw_text_hash}"
    )
    document = save_source_document(
        session,
        ipo_id=ipo.id,
        source="kind",
        document_key=document_key,
        document_type="kind_public_offering_schedule",
        lineage_root_key=f"kind:{lineage_identity}",
        values={
            "source_snapshot_id": snapshot.id,
            "official_url": (
                "https://kind.krx.co.kr/listinvstg/pubofrprogcom.do?method=searchPubofrProgComMain"
            ),
            "published_at": published_at,
            "fetched_at": observed_at,
            "raw_text_hash": snapshot.raw_text_hash,
            "parser_name": "kind-public-offering-schedule",
            "parser_version": "kind-schedule-v1",
            "metadata_json": {
                "kind_biz_no": record.kind_biz_no,
                "report_filed_date": (
                    record.report_filed_date.isoformat() if record.report_filed_date else None
                ),
                "raw_row": record.raw,
            },
        },
    )
    session.flush()
    append_schedule_revision(
        session,
        ipo_id=ipo.id,
        source_document_id=document.id,
        event_status="kind_reported",
        values={
            "demand_start_date": record.demand_start_date,
            "demand_end_date": record.demand_end_date,
            "subscription_start_date": record.subscription_start_date,
            "subscription_end_date": record.subscription_end_date,
            "refund_date": record.refund_date,
            "payment_date": record.payment_date,
            "planned_listing_date": record.listing_date,
            "published_at": published_at,
            "observed_at": observed_at,
            "effective_from": published_at,
        },
    )
    facts = {
        "market": (record.market, None),
        "demand_period": (
            _kind_period(record.demand_start_date, record.demand_end_date),
            None,
        ),
        "subscription_period": (
            _kind_period(record.subscription_start_date, record.subscription_end_date),
            None,
        ),
        "refund_date": (_kind_date(record.refund_date), None),
        "payment_date": (_kind_date(record.payment_date), None),
        "listing_date": (_kind_date(record.listing_date), None),
        "offer_price_final": (record.offer_price_final, "원"),
        "offer_amount_million": (record.offer_amount_million, "백만원"),
        "underwriter_names": (record.underwriters or None, None),
    }
    facts.update(
        {
            f"underwriter:{normalize_broker_name(name)}:role": ("주관", None)
            for name in record.underwriters
            if normalize_broker_name(name)
        }
    )
    for field_name, (value, unit) in facts.items():
        if value is None:
            continue
        append_fact_observation(
            session,
            ipo_id=ipo.id,
            source_document_id=document.id,
            field_name=field_name,
            value=value,
            unit=unit,
            quality_state="reported",
            knowledge_phase="pre_subscription",
            values={
                "published_at": published_at,
                "observed_at": observed_at,
                "fetched_at": observed_at,
                "effective_from": published_at,
                "parser_name": "kind-public-offering-schedule",
                "parser_version": "kind-schedule-v1",
                "raw_text_hash": snapshot.raw_text_hash,
            },
        )


def _kind_report_datetime(value: Optional[date], fallback: datetime) -> datetime:
    if value is None:
        return fallback
    return datetime.combine(value, datetime.max.time(), tzinfo=ZoneInfo("Asia/Seoul")).astimezone(
        timezone.utc
    )


def _kind_date(value: Optional[date]) -> Optional[str]:
    return value.isoformat() if value else None


def _kind_period(start: Optional[date], end: Optional[date]) -> Optional[str]:
    if start is None and end is None:
        return None
    if start == end or end is None:
        return _kind_date(start)
    if start is None:
        return _kind_date(end)
    return f"{start.isoformat()}~{end.isoformat()}"


def score_all_active() -> dict[str, int]:
    init_db()
    service = IpoScoringService()
    with session_scope() as session:
        refresh_ipo_statuses(session)
        ipos = list_active_ipos(session)
        actionable = [ipo for ipo in ipos if _before_subscription_cutoff(ipo)]
        for ipo in actionable:
            service.score_and_save(session, ipo)
        return {
            "processed": len(ipos),
            "scored": len(actionable),
            "skipped": len(ipos) - len(actionable),
        }


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
        if not ipo.dart_corp_code or not ipo.ticker:
            corp = collector.find_corp(ipo.corp_name, stock_code=ipo.ticker)
            if corp:
                ipo.dart_corp_code = ipo.dart_corp_code or corp.get("corp_code")
                ipo.ticker = ipo.ticker or corp.get("stock_code") or None
        corp_code = ipo.dart_corp_code
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
        if not ipo.dart_corp_code or not ipo.ticker:
            corp = collector.find_corp(ipo.corp_name, stock_code=ipo.ticker)
            if corp:
                ipo.dart_corp_code = ipo.dart_corp_code or corp.get("corp_code")
                ipo.ticker = ipo.ticker or corp.get("stock_code") or None
        corp_code = ipo.dart_corp_code
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
        if not ipo.listing_date:
            return {"ipo": ipo.corp_name, "updated": False, "reason": "listing_date missing"}
        if not ipo.ticker:
            ipo.ticker = collector.resolve_ticker(ipo.corp_name, ipo.listing_date)
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


def resolve_ticker_for_ipo(name: Union[str, int]) -> dict[str, Any]:
    init_db()
    collector = PriceCollector()
    with session_scope() as session:
        ipo = _ipo_or_raise(session, name)
        if ipo.ticker:
            return {
                "ipo": ipo.corp_name,
                "ticker": ipo.ticker,
                "updated": False,
                "reason": "ticker already present",
            }
        ipo.ticker = collector.resolve_ticker(
            ipo.corp_name,
            ipo.actual_listing_date or ipo.listing_date,
        )
        return {"ipo": ipo.corp_name, "ticker": ipo.ticker, "updated": True}


def backfill_historical_outcomes(start: date, end: date) -> dict[str, Any]:
    """Backfill exact-name tickers, D0/D1/D3 prices, and gross outcome labels."""
    today = local_today()
    if start > end:
        raise ValueError("history start date must not be after end date")
    if end > today:
        raise ValueError("history end date must not be in the future")
    if (end - start).days > 3660:
        raise ValueError("history range must be 10 years or shorter")
    init_db()
    with session_scope() as session:
        candidates = list(
            session.execute(
                select(Ipo)
                .where(
                    Ipo.listing_date.between(start, end),
                    Ipo.is_sample.is_(False),
                )
                .order_by(Ipo.listing_date, Ipo.id)
            ).scalars()
        )
        candidate_ids = [ipo.id for ipo in candidates]

    collector = PriceCollector()
    results: list[dict[str, Any]] = []
    counts = {"resolved": 0, "prices": 0, "evaluated": 0, "skipped": 0, "failed": 0}
    for ipo_id in candidate_ids:
        try:
            with session_scope() as session:
                ipo = _ipo_or_raise(session, ipo_id)
                ticker = ipo.ticker
                corp_name = ipo.corp_name
                listing_date = ipo.listing_date
                offer_price = ipo.offer_price_final
            if listing_date is None:
                counts["skipped"] += 1
                results.append(
                    {"ipo": corp_name, "status": "skipped", "reason": "listing_date missing"}
                )
                continue
            if not ticker:
                ticker = collector.resolve_ticker(corp_name, listing_date)
                with session_scope() as session:
                    _ipo_or_raise(session, ipo_id).ticker = ticker
                counts["resolved"] += 1
            payload = collector.collect_listing_prices(ticker, listing_date)
            with session_scope() as session:
                ipo = _ipo_or_raise(session, ipo_id)
                upsert_listing_price(session, ipo_id=ipo.id, values=payload.__dict__)
                if payload.d0_trade_date:
                    ipo.actual_listing_date = payload.d0_trade_date
            counts["prices"] += 1
            if not offer_price:
                counts["skipped"] += 1
                results.append(
                    {
                        "ipo": corp_name,
                        "ticker": ticker,
                        "status": "skipped",
                        "reason": (
                            "final offer price missing; price saved but outcome not evaluated"
                        ),
                    }
                )
                continue
            with session_scope() as session:
                ipo = _ipo_or_raise(session, ipo_id)
                evaluation = OutcomeEvaluationService().evaluate_and_save(session, ipo)
                d0_return = evaluation.d0_open_return_pct
                d1_return = evaluation.d1_close_return_pct
            counts["evaluated"] += 1
            results.append(
                {
                    "ipo": corp_name,
                    "ticker": ticker,
                    "status": "success",
                    "d0_open_return_pct": d0_return,
                    "d1_close_return_pct": d1_return,
                }
            )
        except Exception as exc:
            counts["failed"] += 1
            results.append(
                {
                    "ipo_id": ipo_id,
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return {
        "start": start,
        "end": end,
        "processed": len(candidate_ids),
        **counts,
        "results": results,
        "note": (
            "Returns are gross; pre-listing feature validation requires eligible score snapshots."
        ),
    }


def collect_market_condition(as_of_date: Optional[date] = None) -> dict[str, Any]:
    init_db()
    as_of_date = as_of_date or local_today()
    settings = get_settings()
    outcome_cutoff = datetime.combine(
        as_of_date + timedelta(days=1),
        datetime.min.time(),
        tzinfo=ZoneInfo(settings.timezone),
    ).astimezone(timezone.utc)
    payload = None
    price_error = None
    try:
        payload = PriceCollector().collect_market_condition(as_of_date)
    except Exception as exc:
        price_error = f"{type(exc).__name__}: {exc}"
    with session_scope() as session:
        ranked_outcomes = (
            select(
                OutcomeEvaluation.id.label("outcome_id"),
                OutcomeEvaluation.ipo_id.label("ipo_id"),
                OutcomeEvaluation.evaluated_at.label("evaluated_at"),
                func.row_number()
                .over(
                    partition_by=OutcomeEvaluation.ipo_id,
                    order_by=(
                        OutcomeEvaluation.evaluated_at.desc(),
                        OutcomeEvaluation.id.desc(),
                    ),
                )
                .label("revision_rank"),
            )
            .join(Ipo, OutcomeEvaluation.ipo_id == Ipo.id)
            .where(
                OutcomeEvaluation.d1_close_return_pct.is_not(None),
                OutcomeEvaluation.evaluated_at < outcome_cutoff,
                Ipo.is_sample.is_(False),
                or_(
                    Ipo.actual_listing_date <= as_of_date,
                    and_(
                        Ipo.actual_listing_date.is_(None),
                        Ipo.listing_date <= as_of_date,
                    ),
                ),
            )
            .subquery()
        )
        outcomes = list(
            session.execute(
                select(OutcomeEvaluation)
                .join(
                    ranked_outcomes,
                    ranked_outcomes.c.outcome_id == OutcomeEvaluation.id,
                )
                .where(ranked_outcomes.c.revision_rank == 1)
                .order_by(
                    desc(ranked_outcomes.c.evaluated_at),
                    desc(ranked_outcomes.c.outcome_id),
                )
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
        created = row is None
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
        if payload is None and (created or not row.source):
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


EMAIL_EVENT_VERSION = "v2"
MONTHLY_EMAIL_EVENT_VERSION = "v3-facts"
SUBSCRIPTION_EMAIL_EVENT_VERSION = "v3-facts-d1"
SUBSCRIPTION_RECONCILIATION_STATE = "KIND 청약일 일치"
NO_EMAIL_ITEMS_REASON = "해당 기간에 발송할 IPO 판단 또는 일정이 없습니다."
MONTHLY_RECONCILIATION_REQUIRED_REASON = "월 전체 공식 대조가 완료되지 않아 발송하지 않습니다."
SUBSCRIPTION_EVIDENCE_REQUIRED_REASON = (
    "최신 공식 KIND 일반청약 일정이 현재 종목 일정과 일치하지 않아 발송하지 않습니다."
)
SUBSCRIPTION_REPORT_REQUIRED_REASON = (
    "종목별 리포트의 식별·KIND 청약일 대조 상태가 안전하지 않아 발송하지 않습니다."
)
NO_D_MINUS_ONE_ITEMS_REASON = "내일 일반청약을 시작하는 IPO가 없습니다."
D_MINUS_ONE_ALL_BLOCKED_REASON = "D-1 종목별 발송 안전 게이트를 통과한 IPO가 없습니다."
WEEKLY_V3_SKIP_REASON = "V3 메일은 일반청약 시작 D-1에 종목별로 발송됩니다."
MONDAY_DAILY_SKIP_REASON = "월요일 일간 브리핑은 주간 브리핑으로 통합됩니다."
KST = ZoneInfo("Asia/Seoul")


@dataclass(frozen=True)
class _SubscriptionEmailCandidate:
    ipo_id: int
    offering_id: str
    corp_name: str
    subscription_start_date: date
    bundle: MonthlyEmailBundle
    evidence_warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _SubscriptionEmailBatch:
    run_date: date
    subscription_date: date
    total_candidates: int
    ready: tuple[_SubscriptionEmailCandidate, ...]
    blocked: tuple[dict[str, Any], ...]


def build_daily_email_content(target: Optional[date] = None) -> tuple[EmailContent, int, int]:
    target = target or local_today()
    return _build_email_content(
        target,
        target,
        title=f"[Korea IPO] {_email_date_label(target)} 일간 브리핑",
    )


def build_weekly_email_content(start: Optional[date] = None) -> tuple[EmailContent, int, int]:
    start = start or local_today()
    end = start + timedelta(days=6)
    return _build_email_content(
        start,
        end,
        title=(f"[Korea IPO] {_email_date_label(start)}–{_email_date_label(end)} 주간 브리핑"),
    )


def _email_date_label(value: date) -> str:
    return f"{value.month}월 {value.day}일"


def send_daily_email_summary() -> dict[str, Any]:
    if getattr(get_settings(), "email_product_mode", "v2") == "v3-facts":
        return send_subscription_d_minus_one_emails()
    target = local_today()
    if target.weekday() == 0:
        return _skipped_email_result(MONDAY_DAILY_SKIP_REASON)
    content, decision_count, schedule_count = build_daily_email_content(target)
    return _send_period_email(
        content,
        decision_count=decision_count,
        schedule_count=schedule_count,
        event_key=f"daily-decision-email:{EMAIL_EVENT_VERSION}:{target.isoformat()}",
    )


def send_weekly_email_summary() -> dict[str, Any]:
    if getattr(get_settings(), "email_product_mode", "v2") == "v3-facts":
        return _skipped_email_result(WEEKLY_V3_SKIP_REASON)
    start = local_today()
    content, decision_count, schedule_count = build_weekly_email_content(start)
    return _send_period_email(
        content,
        decision_count=decision_count,
        schedule_count=schedule_count,
        event_key=f"weekly-decision-email:{EMAIL_EVENT_VERSION}:{start.isoformat()}",
    )


def _send_period_email(
    content: EmailContent,
    *,
    decision_count: int,
    schedule_count: int,
    event_key: str,
) -> dict[str, Any]:
    item_count = decision_count + schedule_count
    if item_count == 0:
        return _skipped_email_result(NO_EMAIL_ITEMS_REASON)
    result = send_email_message(content, event_key=event_key)
    return {
        **result,
        "items": item_count,
        "decision_items": decision_count,
        "schedule_items": schedule_count,
    }


def _skipped_email_result(reason: str) -> dict[str, Any]:
    return {
        "processed": 0,
        "sent": 0,
        "duplicate": 0,
        "failed": 0,
        "delivery_state_unknown": 0,
        "skipped": True,
        "reason": reason,
        "failures": [],
        "items": 0,
        "decision_items": 0,
        "schedule_items": 0,
    }


def build_monthly_email_content(target: Optional[date] = None) -> MonthlyEmailBundle:
    """Build the complete calendar-month roster without filtering ended IPOs."""
    init_db()
    target = target or local_today()
    month_start, month_end = _calendar_month_bounds(target)
    settings = get_settings()
    generated_at = local_now()
    with session_scope() as session:
        universe = list_monthly_universe(session, month_start, month_end)
        return build_monthly_ipo_email(
            session,
            universe,
            month_start=month_start,
            month_end=month_end,
            generated_at=generated_at,
            title=f"[Korea IPO] {month_start.year}년 {month_start.month}월",
            max_html_bytes=getattr(settings, "email_max_html_bytes", 90_000),
        )


def build_subscription_d_minus_one_email_batch(
    target: Optional[date] = None,
) -> _SubscriptionEmailBatch:
    """Build one independently auditable email for each IPO opening tomorrow.

    ``target`` is the scheduler run date in KST. Candidates are selected directly
    by exact retail-subscription start date. Each one is fail-closed unless its
    latest official KIND subscription period matches the current schedule.
    """
    init_db()
    run_date = target or local_today()
    subscription_date = run_date + timedelta(days=1)
    month_start, month_end = _calendar_month_bounds(subscription_date)
    generated_at = (
        local_now() if target is None else datetime.combine(run_date, time(18, 30), tzinfo=KST)
    )
    settings = get_settings()
    ready: list[_SubscriptionEmailCandidate] = []
    blocked: list[dict[str, Any]] = []
    with session_scope() as session:
        ipos = tuple(
            session.execute(
                select(Ipo)
                .where(
                    Ipo.subscription_start_date == subscription_date,
                    Ipo.is_sample.is_(False),
                    Ipo.data_origin.notin_({"sample", "synthetic", "test"}),
                    Ipo.status.notin_({"withdrawn", "archived"}),
                )
                .order_by(Ipo.corp_name, Ipo.id)
            ).scalars()
        )
        for ipo in ipos:
            expected_period = _kind_period(
                ipo.subscription_start_date,
                ipo.subscription_end_date,
            )
            observed_period = _latest_official_kind_subscription_period(
                session,
                ipo.id,
                generated_at,
            )
            if observed_period != expected_period:
                blocked.append(
                    {
                        "ipo_id": ipo.id,
                        "corp_name": ipo.corp_name,
                        "reason": SUBSCRIPTION_EVIDENCE_REQUIRED_REASON,
                        "expected_subscription_period": expected_period,
                        "observed_subscription_period": observed_period,
                    }
                )
                continue
            offering_id = ipo.offering_id or ipo.identity_key or f"ipo:{ipo.id}"
            entry = MonthlyUniverseEntry(
                ipo=ipo,
                ipo_id=ipo.id,
                offering_id=offering_id,
                event_badges=("subscription",),
                inclusion_dates={
                    "subscription": tuple(
                        value
                        for value in (
                            ipo.subscription_start_date,
                            ipo.subscription_end_date,
                        )
                        if value is not None
                    )
                },
                reconciliation_state=SUBSCRIPTION_RECONCILIATION_STATE,
                planned_listing_date=ipo.listing_date,
                actual_listing_date=ipo.actual_listing_date,
                display_listing_date=ipo.actual_listing_date or ipo.listing_date,
            )
            try:
                bundle = build_single_ipo_email(
                    session,
                    entry,
                    month_start=month_start,
                    month_end=month_end,
                    generated_at=generated_at,
                    max_html_bytes=getattr(settings, "email_max_html_bytes", 90_000),
                )
            except Exception as exc:
                blocked.append(
                    {
                        "ipo_id": ipo.id,
                        "corp_name": ipo.corp_name,
                        "reason": "종목별 리포트 생성 실패",
                        "error": f"{type(exc).__name__}: report build failed",
                    }
                )
                continue
            if not _single_ipo_bundle_is_sendable(bundle, ipo.id):
                blocked.append(
                    {
                        "ipo_id": ipo.id,
                        "corp_name": ipo.corp_name,
                        "reason": SUBSCRIPTION_REPORT_REQUIRED_REASON,
                        "reconciliation_state": bundle.read_model.reconciliation_state,
                    }
                )
                continue
            ready.append(
                _SubscriptionEmailCandidate(
                    ipo_id=ipo.id,
                    offering_id=offering_id,
                    corp_name=ipo.corp_name,
                    subscription_start_date=subscription_date,
                    bundle=bundle,
                    evidence_warnings=tuple(bundle.read_model.core[0].missing_labels),
                )
            )
    return _SubscriptionEmailBatch(
        run_date=run_date,
        subscription_date=subscription_date,
        total_candidates=len(ready) + len(blocked),
        ready=tuple(ready),
        blocked=tuple(blocked),
    )


def _latest_official_kind_subscription_period(
    session: Session,
    ipo_id: int,
    cutoff: datetime,
) -> Optional[str]:
    cutoff_utc = cutoff.astimezone(timezone.utc)
    observation = (
        session.execute(
            select(IpoFactObservation)
            .join(
                SourceDocument,
                IpoFactObservation.source_document_id == SourceDocument.id,
            )
            .where(
                IpoFactObservation.ipo_id == ipo_id,
                IpoFactObservation.field_name == "subscription_period",
                IpoFactObservation.quality_state.in_({"reported", "cross_checked", "verified"}),
                IpoFactObservation.observed_at <= cutoff_utc,
                or_(
                    IpoFactObservation.fetched_at.is_(None),
                    IpoFactObservation.fetched_at <= cutoff_utc,
                ),
                SourceDocument.source == "kind",
                SourceDocument.document_type == "kind_public_offering_schedule",
            )
            .order_by(
                desc(IpoFactObservation.observed_at),
                desc(IpoFactObservation.id),
            )
        )
        .scalars()
        .first()
    )
    if observation is None or observation.value_json is None:
        return None
    return str(observation.value_json).strip() or None


def _single_ipo_bundle_is_sendable(bundle: MonthlyEmailBundle, ipo_id: int) -> bool:
    return (
        bundle.read_model.reconciliation_state == SUBSCRIPTION_RECONCILIATION_STATE
        and len(bundle.read_model.core) == 1
        and not bundle.read_model.filing_appendix
        and bundle.read_model.core[0].ipo_id == ipo_id
        and len(bundle.parts) == 1
    )


def subscription_email_event_key(candidate: _SubscriptionEmailCandidate) -> str:
    """Return the stable occurrence key shared by preview and SMTP delivery."""
    occurrence_hash = hashlib.sha256(candidate.offering_id.encode("utf-8")).hexdigest()[:20]
    return (
        f"subscription-d1-email:{SUBSCRIPTION_EMAIL_EVENT_VERSION}:"
        f"{candidate.subscription_start_date.isoformat()}:{occurrence_hash}"
    )


def send_subscription_d_minus_one_emails(
    target: Optional[date] = None,
) -> dict[str, Any]:
    """Send one message per IPO exactly one KST day before retail subscription."""
    batch = build_subscription_d_minus_one_email_batch(target)
    aggregate: dict[str, Any] = {
        "processed": 0,
        "sent": 0,
        "duplicate": 0,
        "failed": 0,
        "delivery_state_unknown": 0,
        "skipped": batch.total_candidates == 0 or not batch.ready,
        "failures": [],
        "items": len(batch.ready),
        "candidates": batch.total_candidates,
        "blocked": len(batch.blocked),
        "blocked_items": list(batch.blocked),
        "evidence_warnings": [
            {
                "ipo_id": candidate.ipo_id,
                "corp_name": candidate.corp_name,
                "missing_labels": list(candidate.evidence_warnings),
            }
            for candidate in batch.ready
            if candidate.evidence_warnings
        ],
        "run_date": batch.run_date.isoformat(),
        "subscription_date": batch.subscription_date.isoformat(),
        "recipient": DEFAULT_EMAIL_RECIPIENT,
    }
    if batch.total_candidates == 0:
        aggregate["reason"] = NO_D_MINUS_ONE_ITEMS_REASON
        return aggregate
    if not batch.ready:
        aggregate["reason"] = D_MINUS_ONE_ALL_BLOCKED_REASON
        return aggregate

    aggregate["skipped"] = False
    for candidate in batch.ready:
        bundle = candidate.bundle
        content = bundle.parts[0]
        event_key = subscription_email_event_key(candidate)
        try:
            result = send_email_message(
                content,
                event_key=event_key,
                recipients=(DEFAULT_EMAIL_RECIPIENT,),
                audit_context=EmailAuditContext(
                    universe_hash=bundle.universe_hash,
                    evidence_hash=bundle.evidence_hash,
                    metadata={
                        "product": SUBSCRIPTION_EMAIL_EVENT_VERSION,
                        "trigger": "subscription_d_minus_one",
                        "ipo_id": candidate.ipo_id,
                        "offering_id": candidate.offering_id,
                        "corp_name": candidate.corp_name,
                        "run_date": batch.run_date.isoformat(),
                        "send_date": batch.run_date.isoformat(),
                        "subscription_start_date": (candidate.subscription_start_date.isoformat()),
                        "universe_hash": bundle.universe_hash,
                        "evidence_hash": bundle.evidence_hash,
                        "missing_labels": list(candidate.evidence_warnings),
                    },
                ),
            )
        except Exception as exc:
            aggregate["processed"] += 1
            aggregate["failed"] += 1
            aggregate["failures"].append(
                {
                    "ipo_id": candidate.ipo_id,
                    "corp_name": candidate.corp_name,
                    "error": f"{type(exc).__name__}: email send failed",
                }
            )
            continue
        for key in (
            "processed",
            "sent",
            "duplicate",
            "failed",
            "delivery_state_unknown",
        ):
            aggregate[key] += int(result.get(key, 0) or 0)
        aggregate["failures"].extend(result.get("failures", []))
        if result.get("skipped"):
            aggregate["skipped"] = True
            aggregate["reason"] = result.get("reason")
    return aggregate


def send_monthly_email_snapshot(
    target: Optional[date] = None,
    *,
    trigger: str = "manual",
) -> dict[str, Any]:
    """Send every deterministic v3 part to the one fixed operational recipient."""
    if trigger not in {"daily", "weekly", "manual", "canary"}:
        raise ValueError("trigger must be daily, weekly, manual, or canary")
    bundle = build_monthly_email_content(target)
    read_model = bundle.read_model
    item_count = len(read_model.core) + len(read_model.filing_appendix)
    if item_count == 0:
        return _skipped_email_result(NO_EMAIL_ITEMS_REASON)
    if read_model.reconciliation_state != "cross_checked":
        return _skipped_email_result(MONTHLY_RECONCILIATION_REQUIRED_REASON)
    period_token = read_model.month_start.strftime("%Y-%m")
    trigger_token = trigger
    aggregate: dict[str, Any] = {
        "processed": 0,
        "sent": 0,
        "duplicate": 0,
        "failed": 0,
        "delivery_state_unknown": 0,
        "skipped": False,
        "failures": [],
        "items": item_count,
        "core_items": len(read_model.core),
        "filing_appendix_items": len(read_model.filing_appendix),
        "parts": len(bundle.parts),
        "universe_hash": bundle.universe_hash,
        "evidence_hash": bundle.evidence_hash,
        "recipient": DEFAULT_EMAIL_RECIPIENT,
    }
    for part_number, content in enumerate(bundle.parts, 1):
        event_key = (
            f"monthly-ipo-email:{MONTHLY_EMAIL_EVENT_VERSION}:{trigger_token}:{period_token}:"
            f"{bundle.universe_hash[:12]}:{bundle.evidence_hash[:12]}:{part_number}"
        )
        result = send_email_message(
            content,
            event_key=event_key,
            recipients=(DEFAULT_EMAIL_RECIPIENT,),
            audit_context=EmailAuditContext(
                universe_hash=bundle.universe_hash,
                evidence_hash=bundle.evidence_hash,
                metadata={
                    "product": MONTHLY_EMAIL_EVENT_VERSION,
                    "period": period_token,
                    "trigger": trigger,
                    "part_number": part_number,
                    "part_count": len(bundle.parts),
                },
            ),
        )
        for key in (
            "processed",
            "sent",
            "duplicate",
            "failed",
            "delivery_state_unknown",
        ):
            aggregate[key] += int(result.get(key, 0) or 0)
        aggregate["failures"].extend(result.get("failures", []))
        if result.get("skipped"):
            aggregate["skipped"] = True
            aggregate["reason"] = result.get("reason")
    return aggregate


def _calendar_month_bounds(target: date) -> tuple[date, date]:
    start = target.replace(day=1)
    return start, target.replace(day=monthrange(target.year, target.month)[1])


def send_monthly_backtest() -> dict[str, Any]:
    init_db()
    if not get_settings().telegram_configured:
        return {"sent": False, "skipped": True, "reason": "Telegram is not configured"}
    with session_scope() as session:
        bundle = build_operational_backtest(session)
        text_body = format_operational_backtest(bundle)
    month = local_today().strftime("%Y-%m")
    response = send_telegram_message(text_body, event_key=f"monthly-backtest:{month}")
    return {
        "sent": bool(response.get("ok", True)),
        "validation_ready": bundle.readiness.validation_ready,
        "eligible_pairs": bundle.readiness.complete_pair_count,
        "gross_outcomes": bundle.gross_outcomes.ipo_count,
    }


def _build_email_content(
    start: date,
    end: date,
    *,
    title: str,
) -> tuple[EmailContent, int, int]:
    init_db()
    settings = get_settings()
    generated_at = local_now()
    with session_scope() as session:
        ipos = list_ipos_between(session, start, end)
        schedule_ipos = [
            ipo for ipo in ipos if not ipo.is_sample and ipo.status not in {"withdrawn", "archived"}
        ]
        schedule_items = build_schedule_email_items(schedule_ipos, start, end)
        decision_ipos = [ipo for ipo in ipos if _eligible_for_decision_email(ipo, generated_at)]
        content = build_period_decision_email(
            session,
            decision_ipos,
            schedule_items=schedule_items,
            user_cash=settings.default_user_cash,
            title=title,
            period_label=(
                start.isoformat() if start == end else f"{start.isoformat()} ~ {end.isoformat()}"
            ),
            generated_at=generated_at,
        )
        return content, len(decision_ipos), len(schedule_items)


def _eligible_for_decision_email(ipo: Ipo, as_of: Union[date, datetime]) -> bool:
    if ipo.is_sample:
        return False
    if ipo.status in {"subscription_closed", "listed", "withdrawn", "archived"}:
        return False
    as_of_at = (
        as_of
        if isinstance(as_of, datetime)
        else datetime.combine(as_of, datetime.min.time(), tzinfo=KST)
    )
    as_of_date = as_of_at.astimezone(KST).date()
    if ipo.subscription_end_date is not None and ipo.subscription_end_date < as_of_date:
        return False
    return _before_subscription_cutoff(ipo, as_of_at)


def _before_subscription_cutoff(ipo: Ipo, as_of: Optional[datetime] = None) -> bool:
    subscription_start_date = getattr(ipo, "subscription_start_date", None)
    if subscription_start_date is None:
        return True
    cutoff = datetime.combine(
        subscription_start_date,
        datetime.min.time().replace(hour=9),
        tzinfo=KST,
    )
    current = as_of if as_of is not None else local_now()
    return current.astimezone(KST) < cutoff


def seed_sample_data() -> dict[str, Any]:
    init_db()
    with session_scope() as session:
        before = session.query(Ipo).count()
        ipo = find_ipo(session, "ABC테크")
        if ipo is None:
            ipo = Ipo(
                identity_key="sample:ABC테크",
                corp_name="ABC테크",
                normalized_name="abc테크",
                is_sample=True,
                data_origin="synthetic",
            )
            session.add(ipo)
            session.flush()
        ipo.is_sample = True
        ipo.data_origin = "synthetic"
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
                status = operation_status(result)
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


def operation_status(result: dict[str, Any]) -> str:
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
