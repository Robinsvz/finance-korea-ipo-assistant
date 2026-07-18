from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Union

from sqlalchemy import and_, desc, or_, select
from sqlalchemy.orm import Session

from app.config import load_yaml_config
from app.models import (
    AllocationScenario,
    DemandForecast,
    DiligenceReview,
    FinancialMetric,
    FinancialStatement,
    Ipo,
    ListingPrice,
    NewsMetric,
    OutcomeEvaluation,
    ScoreSnapshot,
    SearchTrend,
    SourceSnapshot,
    UnderwriterAllocation,
)
from app.schemas import AllocationResult, IpoScheduleRecord
from app.services.clock import local_today
from app.services.normalization import normalize_broker_name, normalize_company_name

ACTIVE_IPO_STATUSES = {
    "scheduled",
    "demand_forecast",
    "subscription_open",
    "subscription_closed",
}
TERMINAL_IPO_STATUSES = {"withdrawn", "archived"}


def find_ipo(
    session: Session,
    name: str,
    subscription_start_date: Optional[date] = None,
    *,
    kind_biz_no: Optional[str] = None,
    dart_corp_code: Optional[str] = None,
) -> Optional[Ipo]:
    if kind_biz_no:
        matched = (
            session.execute(select(Ipo).where(Ipo.kind_biz_no == kind_biz_no)).scalars().first()
        )
        if matched:
            return matched
    if dart_corp_code:
        matched = (
            session.execute(select(Ipo).where(Ipo.dart_corp_code == dart_corp_code))
            .scalars()
            .first()
        )
        if matched:
            return matched

    normalized = normalize_company_name(name)
    stmt = select(Ipo).where(Ipo.normalized_name == normalized)
    if subscription_start_date is not None:
        exact = (
            session.execute(
                stmt.where(Ipo.subscription_start_date == subscription_start_date).order_by(
                    desc(Ipo.updated_at)
                )
            )
            .scalars()
            .first()
        )
        if exact:
            return exact
        placeholders = list(
            session.execute(stmt.where(Ipo.subscription_start_date.is_(None))).scalars()
        )
        return placeholders[0] if len(placeholders) == 1 else None
    return session.execute(stmt.order_by(desc(Ipo.updated_at), desc(Ipo.id))).scalars().first()


def get_ipo_or_raise(session: Session, identifier: Union[str, int]) -> Ipo:
    """Resolve a user-facing name strictly, or an internal immutable IPO id."""
    if isinstance(identifier, int):
        ipo = session.get(Ipo, identifier)
        if ipo is None:
            raise ValueError(f"IPO not found: id={identifier}")
        return ipo

    normalized = normalize_company_name(identifier)
    rows = list(
        session.execute(
            select(Ipo)
            .where(Ipo.normalized_name == normalized)
            .order_by(Ipo.subscription_start_date.is_(None), desc(Ipo.subscription_start_date))
        ).scalars()
    )
    if not rows:
        raise ValueError(f"IPO not found: {identifier}")
    if len(rows) > 1:
        candidates = ", ".join(
            f"id={row.id} {row.subscription_start_date or '일정미정'} {row.market or '시장미정'}"
            for row in rows[:5]
        )
        raise ValueError(f"IPO name is ambiguous: {identifier} ({candidates})")
    return rows[0]


def list_ipos_between(session: Session, start: date, end: date) -> list[Ipo]:
    stmt = (
        select(Ipo)
        .where(
            or_(
                and_(Ipo.subscription_start_date >= start, Ipo.subscription_start_date <= end),
                and_(Ipo.listing_date >= start, Ipo.listing_date <= end),
                and_(Ipo.refund_date >= start, Ipo.refund_date <= end),
                and_(Ipo.demand_start_date >= start, Ipo.demand_start_date <= end),
            )
        )
        .order_by(Ipo.subscription_start_date.is_(None), Ipo.subscription_start_date, Ipo.corp_name)
    )
    return list(session.execute(stmt).unique().scalars())


def list_active_ipos(session: Session) -> list[Ipo]:
    stmt = (
        select(Ipo)
        .where(Ipo.status.in_(ACTIVE_IPO_STATUSES))
        .order_by(Ipo.subscription_start_date.is_(None), Ipo.subscription_start_date, Ipo.corp_name)
    )
    return list(session.execute(stmt).scalars())


def list_price_due_ipos(session: Session, today: Optional[date] = None) -> list[Ipo]:
    today = today or local_today()
    stmt = (
        select(Ipo)
        .where(
            Ipo.ticker.is_not(None),
            Ipo.listing_date.is_not(None),
            Ipo.listing_date <= today,
            Ipo.listing_date >= today - timedelta(days=21),
            Ipo.status.notin_(TERMINAL_IPO_STATUSES),
        )
        .order_by(Ipo.listing_date)
    )
    return list(session.execute(stmt).scalars())


def list_evaluation_due_ipos(session: Session) -> list[Ipo]:
    stmt = (
        select(Ipo)
        .join(ListingPrice, ListingPrice.ipo_id == Ipo.id)
        .where(
            ListingPrice.d3_close_price.is_not(None),
            Ipo.status.in_({"listed", "subscription_closed"}),
        )
        .order_by(Ipo.listing_date)
    )
    return list(session.execute(stmt).unique().scalars())


def upsert_schedule_record(session: Session, record: IpoScheduleRecord) -> Ipo:
    candidate_identity = _new_schedule_identity(record)
    normalized_name = normalize_company_name(record.corp_name)
    if not record.kind_biz_no and record.subscription_start_date is None:
        ipo = session.execute(
            select(Ipo).where(Ipo.identity_key == candidate_identity)
        ).scalar_one_or_none()
    else:
        ipo = find_ipo(
            session,
            record.corp_name,
            record.subscription_start_date,
            kind_biz_no=record.kind_biz_no,
        )
    if ipo is None:
        same_name_exists = session.execute(
            select(Ipo.id).where(Ipo.normalized_name == normalized_name).limit(1)
        ).first()
        ipo = Ipo(
            identity_key=candidate_identity,
            corp_name=record.corp_name,
            normalized_name=normalized_name,
            subscription_start_date=record.subscription_start_date,
            needs_review=bool(same_name_exists),
        )
        session.add(ipo)
        session.flush()
    elif (
        record.subscription_start_date
        and ipo.subscription_start_date
        and record.subscription_start_date != ipo.subscription_start_date
    ):
        ipo.schedule_version = (ipo.schedule_version or 1) + 1

    previous_status = ipo.status
    ipo.corp_name = record.corp_name
    ipo.normalized_name = normalize_company_name(record.corp_name)
    _assign_if_present(ipo, "market", record.market)
    _assign_if_present(ipo, "kind_biz_no", record.kind_biz_no)
    _assign_if_present(ipo, "report_filed_date", record.report_filed_date)
    _assign_if_present(ipo, "demand_start_date", record.demand_start_date)
    _assign_if_present(ipo, "demand_end_date", record.demand_end_date)
    _assign_if_present(ipo, "offer_price_final", record.offer_price_final)
    _assign_if_present(ipo, "offer_amount_million", record.offer_amount_million)
    _assign_if_present(ipo, "subscription_start_date", record.subscription_start_date)
    _assign_if_present(ipo, "subscription_end_date", record.subscription_end_date)
    if record.refund_date is not None:
        ipo.refund_date = record.refund_date
        ipo.refund_date_source = record.source
        ipo.refund_date_verified_at = datetime.now(timezone.utc)
    if record.payment_date is not None:
        ipo.payment_date = record.payment_date
        ipo.payment_date_source = record.source
        ipo.payment_date_verified_at = datetime.now(timezone.utc)
    _assign_if_present(ipo, "listing_date", record.listing_date)
    ipo.source_confidence = max(ipo.source_confidence or 0, record.source_confidence)
    ipo.identity_key = build_ipo_identity(ipo)
    ipo.status = derive_status(ipo)
    if ipo.status != previous_status:
        ipo.status_changed_at = datetime.now(timezone.utc)

    broker_config = load_yaml_config("brokers.yaml")
    defaults = broker_config.get("defaults", {})
    configured_brokers = broker_config.get("brokers", {})
    existing = {item.broker_name: item for item in ipo.underwriters}
    for raw_name in record.underwriters:
        broker_name = normalize_broker_name(raw_name)
        if not broker_name:
            continue
        values = {**defaults, **configured_brokers.get(broker_name, {})}
        underwriter = existing.get(broker_name)
        if underwriter is None:
            underwriter = UnderwriterAllocation(ipo_id=ipo.id, broker_name=broker_name)
            ipo.underwriters.append(underwriter)
            existing[broker_name] = underwriter
        underwriter.role = underwriter.role or "주관"
        underwriter.deposit_rate = float(values.get("default_deposit_rate", 0.5))
        underwriter.equal_allocation_ratio = float(values.get("equal_allocation_ratio", 0.5))
        underwriter.proportional_allocation_ratio = float(
            values.get("proportional_allocation_ratio", 0.5)
        )
        underwriter.subscription_fee = int(values.get("default_fee", 0))
        competition = values.get("proportional_competition") or {}
        underwriter.expected_applicants_count = int(
            values.get("expected_applicants_count", 300_000)
        )
        underwriter.proportional_competition_conservative = float(
            competition.get("conservative", 2_000)
        )
        underwriter.proportional_competition_base = float(competition.get("base", 1_000))
        underwriter.proportional_competition_optimistic = float(competition.get("optimistic", 500))
        underwriter.assumption_source = "brokers.yaml"
        underwriter.assumption_updated_at = datetime.now(timezone.utc)
        underwriter.source = record.source

    return ipo


def derive_status(ipo: Ipo, today: Optional[date] = None) -> str:
    today = today or local_today()
    if ipo.status in TERMINAL_IPO_STATUSES:
        return ipo.status
    if ipo.status == "evaluated":
        return "evaluated"
    listing_date = ipo.actual_listing_date or ipo.listing_date
    if listing_date and listing_date <= today:
        return "listed"
    if ipo.subscription_start_date and ipo.subscription_end_date:
        if ipo.subscription_start_date <= today <= ipo.subscription_end_date:
            return "subscription_open"
        if today > ipo.subscription_end_date:
            return "subscription_closed"
    if (
        ipo.demand_start_date
        and ipo.demand_end_date
        and ipo.demand_start_date <= today <= ipo.demand_end_date
    ):
        return "demand_forecast"
    return "scheduled"


def build_ipo_identity(ipo: Ipo) -> str:
    if ipo.kind_biz_no:
        return f"kind:{ipo.kind_biz_no}"
    if ipo.dart_corp_code and ipo.dart_receipt_no:
        return f"dart:{ipo.dart_corp_code}:{ipo.dart_receipt_no}"
    if not ipo.subscription_start_date and ipo.identity_key.startswith(
        f"name:{ipo.normalized_name}:unknown:"
    ):
        return ipo.identity_key
    schedule = ipo.subscription_start_date.isoformat() if ipo.subscription_start_date else "unknown"
    return f"name:{ipo.normalized_name}:{schedule}:v{ipo.schedule_version or 1}"


def _new_schedule_identity(record: IpoScheduleRecord) -> str:
    if record.kind_biz_no:
        return f"kind:{record.kind_biz_no}"
    schedule = (
        record.subscription_start_date.isoformat() if record.subscription_start_date else "unknown"
    )
    raw_key = (record.raw or {}).get("source_key")
    suffix = str(raw_key or record.corp_name)
    return f"name:{normalize_company_name(record.corp_name)}:{schedule}:{suffix}"


def refresh_ipo_statuses(session: Session, today: Optional[date] = None) -> dict[str, int]:
    today = today or local_today()
    changed = 0
    rows = list(session.execute(select(Ipo)).scalars())
    for ipo in rows:
        next_status = derive_status(ipo, today)
        if next_status != ipo.status:
            ipo.status = next_status
            ipo.status_changed_at = datetime.now(timezone.utc)
            changed += 1
    return {"checked": len(rows), "changed": changed}


def latest_score(session: Session, ipo_id: int) -> Optional[ScoreSnapshot]:
    stmt = (
        select(ScoreSnapshot)
        .where(ScoreSnapshot.ipo_id == ipo_id)
        .order_by(desc(ScoreSnapshot.calculated_at), desc(ScoreSnapshot.id))
    )
    return session.execute(stmt).scalars().first()


def save_source_snapshot(
    session: Session,
    source: str,
    source_key: str,
    raw_text: str,
    ipo_id: Optional[int] = None,
    payload_json: Optional[dict] = None,
) -> SourceSnapshot:
    digest = hashlib.sha256(raw_text.encode("utf-8", errors="ignore")).hexdigest()
    existing = (
        session.execute(
            select(SourceSnapshot).where(
                SourceSnapshot.source == source,
                SourceSnapshot.source_key == source_key,
                SourceSnapshot.raw_text_hash == digest,
            )
        )
        .scalars()
        .first()
    )
    if existing:
        if payload_json is not None:
            existing.payload_json = payload_json
        if ipo_id is not None:
            existing.ipo_id = ipo_id
        return existing
    snapshot = SourceSnapshot(
        source=source,
        source_key=source_key,
        ipo_id=ipo_id,
        raw_text_hash=digest,
        payload_json=payload_json,
        raw_text=raw_text,
    )
    session.add(snapshot)
    return snapshot


def upsert_demand_forecast(
    session: Session, *, ipo_id: int, receipt_no: str, values: dict[str, Any]
) -> DemandForecast:
    row = (
        session.execute(
            select(DemandForecast).where(
                DemandForecast.ipo_id == ipo_id,
                DemandForecast.receipt_no == receipt_no,
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        row = DemandForecast(ipo_id=ipo_id, receipt_no=receipt_no)
        session.add(row)
    _set_fields(row, values)
    return row


def upsert_financial_statement(
    session: Session, *, ipo_id: int, values: dict[str, Any]
) -> FinancialStatement:
    row = (
        session.execute(
            select(FinancialStatement).where(
                FinancialStatement.ipo_id == ipo_id,
                FinancialStatement.fiscal_year == values.get("fiscal_year"),
                FinancialStatement.statement_type == values.get("statement_type"),
                FinancialStatement.account_code == values.get("account_code"),
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        row = FinancialStatement(ipo_id=ipo_id)
        session.add(row)
    _set_fields(row, values)
    return row


def upsert_financial_metric(
    session: Session, *, ipo_id: int, values: dict[str, Any]
) -> FinancialMetric:
    source = values.get("source", "opendart")
    row = (
        session.execute(
            select(FinancialMetric).where(
                FinancialMetric.ipo_id == ipo_id,
                FinancialMetric.fiscal_year == values.get("fiscal_year"),
                FinancialMetric.source == source,
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        row = FinancialMetric(ipo_id=ipo_id, source=source)
        session.add(row)
    _set_fields(row, values)
    return row


def upsert_news_metric(session: Session, *, ipo_id: int, values: dict[str, Any]) -> NewsMetric:
    source = values.get("source", "naver_news")
    row = (
        session.execute(
            select(NewsMetric).where(
                NewsMetric.ipo_id == ipo_id,
                NewsMetric.window_start == values.get("window_start"),
                NewsMetric.window_end == values.get("window_end"),
                NewsMetric.source == source,
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        row = NewsMetric(ipo_id=ipo_id, source=source)
        session.add(row)
    _set_fields(row, values)
    return row


def upsert_search_trend(session: Session, *, ipo_id: int, values: dict[str, Any]) -> SearchTrend:
    row = (
        session.execute(
            select(SearchTrend).where(
                SearchTrend.ipo_id == ipo_id,
                SearchTrend.keyword_group == values.get("keyword_group"),
                SearchTrend.window_start == values.get("window_start"),
                SearchTrend.window_end == values.get("window_end"),
                SearchTrend.source == values.get("source"),
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        row = SearchTrend(ipo_id=ipo_id)
        session.add(row)
    _set_fields(row, values)
    return row


def upsert_listing_price(session: Session, *, ipo_id: int, values: dict[str, Any]) -> ListingPrice:
    row = (
        session.execute(
            select(ListingPrice).where(
                ListingPrice.ipo_id == ipo_id,
                ListingPrice.listing_date == values.get("listing_date"),
                ListingPrice.source == values.get("source"),
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        row = ListingPrice(ipo_id=ipo_id)
        session.add(row)
    _set_fields(row, values)
    return row


def upsert_outcome_evaluation(
    session: Session,
    *,
    ipo_id: int,
    score_snapshot_id: Optional[int],
    evaluation_version: str,
    values: dict[str, Any],
) -> OutcomeEvaluation:
    predicate = [
        OutcomeEvaluation.ipo_id == ipo_id,
        OutcomeEvaluation.evaluation_version == evaluation_version,
    ]
    if score_snapshot_id is None:
        predicate.append(OutcomeEvaluation.score_snapshot_id.is_(None))
    else:
        predicate.append(OutcomeEvaluation.score_snapshot_id == score_snapshot_id)
    row = session.execute(select(OutcomeEvaluation).where(*predicate)).scalars().first()
    if row is None:
        row = OutcomeEvaluation(
            ipo_id=ipo_id,
            score_snapshot_id=score_snapshot_id,
            evaluation_version=evaluation_version,
        )
        session.add(row)
    _set_fields(row, values)
    return row


def upsert_diligence_review(
    session: Session,
    *,
    ipo_id: int,
    review_type: str,
    values: dict[str, Any],
) -> DiligenceReview:
    row = (
        session.execute(
            select(DiligenceReview).where(
                DiligenceReview.ipo_id == ipo_id,
                DiligenceReview.review_type == review_type,
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        row = DiligenceReview(ipo_id=ipo_id, review_type=review_type)
        session.add(row)
    elif row.review_status == "reviewed" and values.get("review_status") == "collected":
        return row
    _set_fields(row, values)
    return row


def save_allocation_result(session: Session, result: AllocationResult) -> AllocationScenario:
    scenario = AllocationScenario(
        ipo_id=result.ipo_id,
        broker_name=result.broker_name,
        user_cash=result.user_cash,
        offer_price=result.offer_price,
        apply_shares=result.apply_shares,
        expected_equal_shares_low=result.expected_equal_shares_low,
        expected_equal_shares_mid=result.expected_equal_shares_mid,
        expected_equal_shares_high=result.expected_equal_shares_high,
        expected_proportional_shares_conservative=result.expected_proportional_shares_conservative,
        expected_proportional_shares_base=result.expected_proportional_shares_base,
        expected_proportional_shares_optimistic=result.expected_proportional_shares_optimistic,
        expected_total_shares_base=result.expected_total_shares_base,
        estimated_fee=result.estimated_fee,
        estimated_required_deposit=result.estimated_required_deposit,
        assumption_source=result.assumption_source,
        assumption_updated_at=result.assumption_updated_at,
    )
    session.add(scenario)
    return scenario


def count_rows(session: Session, models: Iterable[type]) -> dict[str, int]:
    return {model.__tablename__: session.query(model).count() for model in models}


def _assign_if_present(target: Any, name: str, value: Any) -> None:
    if value is not None:
        setattr(target, name, value)


def _set_fields(target: Any, values: dict[str, Any]) -> None:
    column_names = {column.name for column in target.__table__.columns}
    for key, value in values.items():
        if key in column_names and value is not None:
            setattr(target, key, value)
