from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Union

from sqlalchemy import and_, desc, or_, select
from sqlalchemy.orm import Session

from app.config import load_yaml_config
from app.models import (
    AllocationScenario,
    BrokerTermVersion,
    DataQualityIssue,
    DemandForecast,
    DiligenceReview,
    FinancialMetric,
    FinancialStatement,
    Ipo,
    IpoFactObservation,
    IpoScheduleRevision,
    ListingPrice,
    MonthlyUniverseSnapshot,
    NewsMetric,
    NotificationContentArtifact,
    NotificationEvent,
    OutcomeEvaluation,
    ScoreSnapshot,
    SearchTrend,
    SourceDocument,
    SourceSnapshot,
    UnderwriterAllocation,
)
from app.schemas import (
    AllocationResult,
    IpoScheduleRecord,
    MonthlyUniverseEntry,
    MonthlyUniverseResult,
)
from app.services.clock import local_today
from app.services.normalization import normalize_broker_name, normalize_company_name

ACTIVE_IPO_STATUSES = {
    "scheduled",
    "demand_forecast",
    "subscription_open",
    "subscription_closed",
}
TERMINAL_IPO_STATUSES = {"withdrawn", "archived"}
NON_PRODUCTION_ORIGINS = {"sample", "synthetic", "test"}
MONTHLY_EVENT_ORDER = (
    "demand_forecast",
    "subscription",
    "refund",
    "payment",
    "actual_listing",
    "planned_listing",
)


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
    return [entry.ipo for entry in list_monthly_universe(session, start, end).core]


def list_monthly_universe(
    session: Session, month_start: date, month_end: date
) -> MonthlyUniverseResult:
    """Return production IPO activity; filing-only rows never inflate the core count."""
    if month_start > month_end:
        raise ValueError("month_start must be on or before month_end")

    demand_overlap = _period_overlap_predicate(
        Ipo.demand_start_date, Ipo.demand_end_date, month_start, month_end
    )
    subscription_overlap = _period_overlap_predicate(
        Ipo.subscription_start_date,
        Ipo.subscription_end_date,
        month_start,
        month_end,
    )
    single_date_predicates = tuple(
        field.between(month_start, month_end)
        for field in (
            Ipo.refund_date,
            Ipo.payment_date,
            Ipo.listing_date,
            Ipo.actual_listing_date,
        )
    )
    core_predicate = or_(demand_overlap, subscription_overlap, *single_date_predicates)
    production_predicate = _production_ipo_predicate()
    core_rows = list(
        session.execute(select(Ipo).where(production_predicate, core_predicate)).scalars()
    )
    core_ids = {row.id for row in core_rows}
    appendix_rows = list(
        session.execute(
            select(Ipo).where(
                production_predicate,
                Ipo.report_filed_date.between(month_start, month_end),
                Ipo.id.notin_(core_ids),
            )
        ).scalars()
    )

    reconciliation = _latest_monthly_reconciliation(
        session, month_start, [*core_rows, *appendix_rows]
    )
    core = tuple(
        sorted(
            (
                _monthly_entry(
                    ipo,
                    month_start,
                    month_end,
                    reconciliation.get(ipo.id, "unreconciled"),
                )
                for ipo in core_rows
            ),
            key=lambda entry: _monthly_entry_sort_key(entry, month_start),
        )
    )
    appendix = tuple(
        sorted(
            (
                _filing_appendix_entry(ipo, reconciliation.get(ipo.id, "unreconciled"))
                for ipo in appendix_rows
            ),
            key=lambda entry: (entry.inclusion_dates["filing_update"][0], entry.ipo.corp_name),
        )
    )
    return MonthlyUniverseResult(
        month_start=month_start,
        month_end=month_end,
        core=core,
        filing_update_appendix=appendix,
    )


def _period_overlap_predicate(start_field, end_field, start: date, end: date):
    return and_(
        or_(start_field.is_not(None), end_field.is_not(None)),
        or_(start_field.is_(None), start_field <= end),
        or_(end_field.is_(None), end_field >= start),
    )


def _production_ipo_predicate():
    return and_(
        Ipo.is_sample.is_(False),
        Ipo.data_origin.notin_(NON_PRODUCTION_ORIGINS),
    )


def _monthly_entry(
    ipo: Ipo,
    month_start: date,
    month_end: date,
    reconciliation_state: str,
) -> MonthlyUniverseEntry:
    inclusion_dates: dict[str, tuple[date, ...]] = {}
    if _dates_overlap(ipo.demand_start_date, ipo.demand_end_date, month_start, month_end):
        inclusion_dates["demand_forecast"] = _known_dates(
            ipo.demand_start_date, ipo.demand_end_date
        )
    if _dates_overlap(
        ipo.subscription_start_date,
        ipo.subscription_end_date,
        month_start,
        month_end,
    ):
        inclusion_dates["subscription"] = _known_dates(
            ipo.subscription_start_date, ipo.subscription_end_date
        )
    for badge, event_date in (
        ("refund", ipo.refund_date),
        ("payment", ipo.payment_date),
        ("actual_listing", ipo.actual_listing_date),
        ("planned_listing", ipo.listing_date),
    ):
        if event_date is not None and month_start <= event_date <= month_end:
            inclusion_dates[badge] = (event_date,)
    event_badges = tuple(badge for badge in MONTHLY_EVENT_ORDER if badge in inclusion_dates)
    offering_id = ipo.offering_id or ipo.identity_key
    return MonthlyUniverseEntry(
        ipo=ipo,
        ipo_id=ipo.id,
        offering_id=offering_id,
        event_badges=event_badges,
        inclusion_dates=inclusion_dates,
        reconciliation_state=reconciliation_state,
        planned_listing_date=ipo.listing_date,
        actual_listing_date=ipo.actual_listing_date,
        display_listing_date=ipo.actual_listing_date or ipo.listing_date,
    )


def _filing_appendix_entry(ipo: Ipo, reconciliation_state: str) -> MonthlyUniverseEntry:
    filing_date = ipo.report_filed_date
    if filing_date is None:
        raise ValueError("filing appendix rows require report_filed_date")
    return MonthlyUniverseEntry(
        ipo=ipo,
        ipo_id=ipo.id,
        offering_id=ipo.offering_id or ipo.identity_key,
        event_badges=("filing_update",),
        inclusion_dates={"filing_update": (filing_date,)},
        reconciliation_state=reconciliation_state,
        planned_listing_date=ipo.listing_date,
        actual_listing_date=ipo.actual_listing_date,
        display_listing_date=ipo.actual_listing_date or ipo.listing_date,
    )


def _dates_overlap(first: Optional[date], second: Optional[date], start: date, end: date) -> bool:
    if first is None and second is None:
        return False
    effective_start = first or second
    effective_end = second or first
    return bool(effective_start <= end and effective_end >= start)


def _known_dates(*values: Optional[date]) -> tuple[date, ...]:
    return tuple(value for value in values if value is not None)


def _monthly_entry_sort_key(entry: MonthlyUniverseEntry, month_start: date) -> tuple[date, str]:
    dates = [value for values in entry.inclusion_dates.values() for value in values]
    first_relevant_date = min(max(value, month_start) for value in dates)
    return first_relevant_date, entry.ipo.corp_name


def _latest_monthly_reconciliation(
    session: Session, month_start: date, ipos: list[Ipo]
) -> dict[int, str]:
    ipo_ids = [ipo.id for ipo in ipos]
    if not ipo_ids:
        return {}
    rows = session.execute(
        select(MonthlyUniverseSnapshot)
        .where(
            MonthlyUniverseSnapshot.month_start == month_start,
            MonthlyUniverseSnapshot.ipo_id.in_(ipo_ids),
        )
        .order_by(
            desc(MonthlyUniverseSnapshot.captured_at),
            desc(MonthlyUniverseSnapshot.id),
        )
    ).scalars()
    result: dict[int, str] = {}
    for row in rows:
        result.setdefault(row.ipo_id, row.reconciliation_state)
    return result


def list_active_ipos(session: Session) -> list[Ipo]:
    stmt = (
        select(Ipo)
        .where(_production_ipo_predicate(), Ipo.status.in_(ACTIVE_IPO_STATUSES))
        .order_by(Ipo.subscription_start_date.is_(None), Ipo.subscription_start_date, Ipo.corp_name)
    )
    return list(session.execute(stmt).scalars())


def list_price_due_ipos(session: Session, today: Optional[date] = None) -> list[Ipo]:
    today = today or local_today()
    stmt = (
        select(Ipo)
        .where(
            _production_ipo_predicate(),
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
            _production_ipo_predicate(),
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
            offering_id=_new_offering_id(record),
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
    ipo.is_sample = False
    ipo.data_origin = record.source
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
    ipo.offering_id = build_offering_id(ipo)
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


def build_offering_id(ipo: Ipo) -> str:
    """Build a stable offering identity without a revision-specific receipt number."""
    if ipo.kind_biz_no:
        return f"kind:{ipo.kind_biz_no}"
    if ipo.dart_corp_code:
        cycle = _offering_cycle_date(ipo)
        return f"dart:{ipo.dart_corp_code}:equity:{cycle}"
    if ipo.ticker:
        cycle = ipo.actual_listing_date or ipo.listing_date
        suffix = cycle.isoformat() if cycle else "unknown"
        return f"krx:{ipo.ticker}:{suffix}"
    current = ipo.offering_id or ""
    if current and not current.startswith("offering:temp:"):
        return current
    cycle = _offering_cycle_date(ipo)
    if cycle != "unknown":
        return f"name:{ipo.normalized_name}:{cycle}"
    return current or f"offering:temp:{hashlib.sha256(ipo.identity_key.encode()).hexdigest()}"


def _new_schedule_identity(record: IpoScheduleRecord) -> str:
    if record.kind_biz_no:
        return f"kind:{record.kind_biz_no}"
    schedule = (
        record.subscription_start_date.isoformat() if record.subscription_start_date else "unknown"
    )
    raw_key = (record.raw or {}).get("source_key")
    suffix = str(raw_key or record.corp_name)
    return f"name:{normalize_company_name(record.corp_name)}:{schedule}:{suffix}"


def _new_offering_id(record: IpoScheduleRecord) -> str:
    if record.kind_biz_no:
        return f"kind:{record.kind_biz_no}"
    if record.subscription_start_date:
        return (
            f"name:{normalize_company_name(record.corp_name)}:"
            f"{record.subscription_start_date.isoformat()}"
        )
    return f"offering:temp:{uuid_from_source_record(record)}"


def uuid_from_source_record(record: IpoScheduleRecord) -> str:
    raw_key = (record.raw or {}).get("source_key")
    value = f"{record.source}:{raw_key or record.corp_name}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _offering_cycle_date(ipo: Ipo) -> str:
    cycle = (
        ipo.subscription_start_date
        or ipo.report_filed_date
        or ipo.actual_listing_date
        or ipo.listing_date
    )
    return cycle.isoformat() if cycle else "unknown"


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


def save_source_document(
    session: Session,
    *,
    ipo_id: int,
    source: str,
    document_key: str,
    document_type: str,
    lineage_root_key: Optional[str] = None,
    values: Optional[dict[str, Any]] = None,
) -> SourceDocument:
    """Persist a source revision once; a revision cannot move between offerings."""
    existing = session.execute(
        select(SourceDocument).where(
            SourceDocument.source == source,
            SourceDocument.document_key == document_key,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.ipo_id != ipo_id:
            raise ValueError("source document is already linked to another offering")
        return existing
    document = SourceDocument(
        ipo_id=ipo_id,
        source=source,
        document_key=document_key,
        document_type=document_type,
        lineage_root_key=lineage_root_key or document_key,
    )
    _set_fields(document, values or {})
    session.add(document)
    return document


def append_fact_observation(
    session: Session,
    *,
    ipo_id: int,
    field_name: str,
    value: Any,
    quality_state: str,
    knowledge_phase: str,
    source_document_id: Optional[int] = None,
    unit: Optional[str] = None,
    values: Optional[dict[str, Any]] = None,
) -> IpoFactObservation:
    """Append a fact observation, while making identical collector retries idempotent."""
    extras = values or {}
    observation_key = _semantic_hash(
        {
            "ipo_id": ipo_id,
            "source_document_id": source_document_id,
            "field_name": field_name,
            "value": value,
            "unit": unit,
            "quality_state": quality_state,
            "knowledge_phase": knowledge_phase,
            "published_at": extras.get("published_at"),
            "fetched_at": extras.get("fetched_at"),
            "effective_from": extras.get("effective_from"),
            "raw_text_hash": extras.get("raw_text_hash"),
        }
    )
    existing = session.execute(
        select(IpoFactObservation).where(IpoFactObservation.observation_key == observation_key)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    observation = IpoFactObservation(
        observation_key=observation_key,
        ipo_id=ipo_id,
        source_document_id=source_document_id,
        field_name=field_name,
        value_json=value,
        unit=unit,
        quality_state=quality_state,
        knowledge_phase=knowledge_phase,
    )
    _set_fields(observation, extras)
    session.add(observation)
    return observation


def append_schedule_revision(
    session: Session,
    *,
    ipo_id: int,
    event_status: str,
    source_document_id: Optional[int] = None,
    values: Optional[dict[str, Any]] = None,
) -> IpoScheduleRevision:
    """Append one normalized schedule state without overwriting earlier revisions."""
    extras = values or {}
    revision_key = _semantic_hash(
        {
            "ipo_id": ipo_id,
            "source_document_id": source_document_id,
            "event_status": event_status,
            **extras,
        }
    )
    existing = session.execute(
        select(IpoScheduleRevision).where(IpoScheduleRevision.revision_key == revision_key)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    revision = IpoScheduleRevision(
        revision_key=revision_key,
        ipo_id=ipo_id,
        source_document_id=source_document_id,
        event_status=event_status,
    )
    _set_fields(revision, extras)
    session.add(revision)
    return revision


def save_monthly_universe_snapshot(
    session: Session,
    universe: MonthlyUniverseResult,
    *,
    reconciliation_state: str,
    reconciliation_sources: Iterable[str],
) -> list[MonthlyUniverseSnapshot]:
    """Persist one idempotent reconciliation snapshot for every visible offering."""
    sources = tuple(sorted(set(reconciliation_sources)))
    rows: list[MonthlyUniverseSnapshot] = []
    for cohort, entries in (
        ("core", universe.core),
        ("filing_update_appendix", universe.filing_update_appendix),
    ):
        for entry in entries:
            payload = {
                "month_start": universe.month_start,
                "ipo_id": entry.ipo_id,
                "offering_id": entry.offering_id,
                "cohort": cohort,
                "event_badges": entry.event_badges,
                "inclusion_dates": entry.inclusion_dates,
                "reconciliation_state": reconciliation_state,
                "sources": sources,
            }
            snapshot_hash = _semantic_hash(payload)
            existing = session.execute(
                select(MonthlyUniverseSnapshot).where(
                    MonthlyUniverseSnapshot.month_start == universe.month_start,
                    MonthlyUniverseSnapshot.ipo_id == entry.ipo_id,
                    MonthlyUniverseSnapshot.snapshot_hash == snapshot_hash,
                )
            ).scalar_one_or_none()
            if existing is not None:
                rows.append(existing)
                continue
            snapshot = MonthlyUniverseSnapshot(
                month_start=universe.month_start,
                ipo_id=entry.ipo_id,
                offering_id_snapshot=entry.offering_id,
                cohort=cohort,
                event_badges_json=list(entry.event_badges),
                inclusion_dates_json={
                    badge: [value.isoformat() for value in dates]
                    for badge, dates in entry.inclusion_dates.items()
                },
                reconciliation_state=reconciliation_state,
                reconciliation_sources_json=list(sources),
                snapshot_hash=snapshot_hash,
            )
            session.add(snapshot)
            rows.append(snapshot)
    return rows


def save_broker_term_version(
    session: Session,
    *,
    broker_name: str,
    effective_from: date,
    values: Optional[dict[str, Any]] = None,
) -> BrokerTermVersion:
    extras = values or {}
    version_key = _semantic_hash(
        {"broker_name": broker_name, "effective_from": effective_from, **extras}
    )
    existing = session.execute(
        select(BrokerTermVersion).where(BrokerTermVersion.version_key == version_key)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    version = BrokerTermVersion(
        version_key=version_key,
        broker_name=broker_name,
        effective_from=effective_from,
    )
    _set_fields(version, extras)
    session.add(version)
    return version


def upsert_data_quality_issue(
    session: Session,
    *,
    issue_type: str,
    severity: str,
    status: str,
    ipo_id: Optional[int] = None,
    source_document_id: Optional[int] = None,
    field_name: Optional[str] = None,
    details: Optional[dict[str, Any]] = None,
    values: Optional[dict[str, Any]] = None,
) -> DataQualityIssue:
    issue_key = _semantic_hash(
        {
            "ipo_id": ipo_id,
            "source_document_id": source_document_id,
            "issue_type": issue_type,
            "field_name": field_name,
            "details": details,
        }
    )
    issue = session.execute(
        select(DataQualityIssue).where(DataQualityIssue.issue_key == issue_key)
    ).scalar_one_or_none()
    if issue is None:
        issue = DataQualityIssue(
            issue_key=issue_key,
            ipo_id=ipo_id,
            source_document_id=source_document_id,
            issue_type=issue_type,
            field_name=field_name,
            severity=severity,
            status=status,
            details_json=details,
        )
        session.add(issue)
    else:
        issue.severity = severity
        issue.status = status
    _set_fields(issue, values or {})
    return issue


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


def save_notification_content_artifact(
    session: Session,
    *,
    notification_event_id: int,
    subject: str,
    text_body: str,
    html_body: str,
    content_redacted: bool = False,
    universe_hash: Optional[str] = None,
    evidence_hash: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> NotificationContentArtifact:
    """Attach immutable delivery content to an existing notification ledger row.

    Retrying the same event with byte-for-byte equivalent content is idempotent. A
    different artifact is rejected so a delivered ledger row can never be silently
    rewritten after the fact.
    """

    event = session.get(NotificationEvent, notification_event_id)
    if event is None:
        raise ValueError(f"Notification event not found: id={notification_event_id}")

    hashes = _notification_content_hashes(subject, text_body, html_body)
    values = {
        "subject": subject,
        "text_body": text_body,
        "html_body": html_body,
        **hashes,
        "content_redacted": bool(content_redacted),
        "universe_hash": universe_hash,
        "evidence_hash": evidence_hash,
        "metadata_json": metadata or None,
    }
    artifact = session.execute(
        select(NotificationContentArtifact).where(
            NotificationContentArtifact.notification_event_id == notification_event_id
        )
    ).scalar_one_or_none()
    if artifact is None:
        artifact = NotificationContentArtifact(
            notification_event_id=notification_event_id,
            **values,
        )
        session.add(artifact)
        session.flush()
        return artifact

    immutable_values = {
        column: getattr(artifact, column)
        for column in (
            "subject",
            "text_body",
            "html_body",
            "subject_sha256",
            "text_sha256",
            "html_sha256",
            "content_sha256",
            "content_redacted",
            "universe_hash",
            "evidence_hash",
            "metadata_json",
        )
    }
    if immutable_values != values:
        raise ValueError("Notification content artifact is immutable and does not match the retry")
    return artifact


def _assign_if_present(target: Any, name: str, value: Any) -> None:
    if value is not None:
        setattr(target, name, value)


def _set_fields(target: Any, values: dict[str, Any]) -> None:
    column_names = {column.name for column in target.__table__.columns}
    for key, value in values.items():
        if key in column_names and value is not None:
            setattr(target, key, value)


def _semantic_hash(payload: dict[str, Any]) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _notification_content_hashes(subject: str, text_body: str, html_body: str) -> dict[str, str]:
    def digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    serialized = json.dumps(
        [subject, text_body, html_body],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return {
        "subject_sha256": digest(subject),
        "text_sha256": digest(text_body),
        "html_sha256": digest(html_body),
        "content_sha256": digest(serialized),
    }
