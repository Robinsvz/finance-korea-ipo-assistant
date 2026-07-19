from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from math import isclose
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.collectors.kind import parse_kind_listing_html, parse_kind_puboffering_html
from app.exceptions import ExternalSourceError
from app.models import (
    DataQualityIssue,
    DemandForecast,
    Ipo,
    IpoFactObservation,
    IpoStructureMetric,
    SourceDocument,
    SourceSnapshot,
    UnderwriterAllocation,
)
from app.repositories import (
    list_monthly_universe,
    save_monthly_universe_snapshot,
    upsert_data_quality_issue,
)
from app.schemas import MonthlyUniverseResult
from app.services.normalization import normalize_broker_name, normalize_company_name

KIND_SOURCE = "kind"
KIND_LISTING_SOURCE = "kind_listing"
DART_SOURCE = "dart_public"
SAFE_QUALITY_STATES = {"reported", "cross_checked", "verified", "post_event"}
NON_PRODUCTION_ORIGINS = {"sample", "synthetic", "test"}
SEOUL = ZoneInfo("Asia/Seoul")

_BASE_KIND_FIELDS = (
    "demand_period",
    "subscription_period",
    "listing_date",
    "offer_price_final",
    "underwriter_names",
)
_DEMAND_FIELDS = (
    "institutional_competition_ratio",
    "participants_count",
    "lockup_commitment_ratio",
    "offer_price_final",
)
_STRUCTURE_FIELDS = (
    "total_offered_shares",
    "new_shares",
    "secondary_shares",
    "free_float_shares",
    "free_float_ratio",
)
_RETAIL_FIELDS = ("retail_competition_ratio", "retail_applicants_count")
_BROKER_RESULT_FIELDS = (
    "actual_proportional_competition",
    "actual_applicants_count",
    "subscribed_shares",
    "allocated_shares",
)


@dataclass(frozen=True)
class _Fact:
    observation: IpoFactObservation
    document: SourceDocument

    @property
    def value(self) -> Any:
        return self.observation.value_json


@dataclass(frozen=True)
class _OfferingAssessment:
    ipo: Ipo
    stage: str
    missing: tuple[str, ...]
    recovered_dart_fields: frozenset[str]
    broker_details_complete: bool

    @property
    def ready(self) -> bool:
        return not self.missing


@dataclass(frozen=True)
class _SourceRosterAssessment:
    ready: bool
    missing: tuple[str, ...]
    core_names: tuple[str, ...]
    kind_public_offering_names: tuple[str, ...]
    kind_listing_names: tuple[str, ...]
    dart_names: tuple[str, ...]
    expected_listing_names: tuple[str, ...]

    @property
    def details(self) -> dict[str, Any]:
        return {
            "core_names": list(self.core_names),
            "kind_public_offering_names": list(self.kind_public_offering_names),
            "kind_listing_names": list(self.kind_listing_names),
            "dart_names": list(self.dart_names),
            "expected_listing_names": list(self.expected_listing_names),
        }


def reconcile_monthly_evidence(
    session: Session,
    month_start: date,
    month_end: date,
    *,
    as_of: Optional[datetime] = None,
) -> dict[str, Any]:
    """Reconcile one production monthly cohort against persisted official evidence.

    This is deliberately stricter than rendering. A value can appear as a labelled
    legacy fallback in an email, but it cannot earn ``cross_checked`` unless the
    source document, timestamps, parser lineage, current-table projection, and
    stage-appropriate required facts all pass.
    """
    if month_start > month_end:
        raise ValueError("month_start must be on or before month_end")
    effective_as_of = _as_utc(as_of or datetime.now(timezone.utc))
    universe = list_monthly_universe(session, month_start, month_end)
    core = tuple(entry.ipo for entry in universe.core)
    source_rosters = (
        _assess_source_rosters(session, universe, effective_as_of)
        if core
        else _SourceRosterAssessment(False, (), (), (), (), (), ())
    )

    duplicate_offering_ids = _duplicate_offering_ids(core)
    assessments = tuple(
        _assess_offering(
            session,
            ipo,
            effective_as_of,
            duplicate_offering_ids=duplicate_offering_ids,
        )
        for ipo in core
    )
    if not core:
        active_gaps = {
            (
                None,
                "monthly_universe_empty",
                "universe",
            )
        }
        _open_gap_issue(
            session,
            month_start=month_start,
            ipo=None,
            issue_type="monthly_universe_empty",
            field_name="universe",
            details={"reason": "no production IPO is present in the monthly core"},
        )
    else:
        active_gaps = set()
        for assessment in assessments:
            for field_name in assessment.missing:
                issue_type = (
                    "monthly_identity_gap"
                    if field_name.startswith("identity.")
                    else "monthly_evidence_gap"
                )
                active_gaps.add((assessment.ipo.id, issue_type, field_name))
                _open_gap_issue(
                    session,
                    month_start=month_start,
                    ipo=assessment.ipo,
                    issue_type=issue_type,
                    field_name=field_name,
                    details={
                        "reason": "required current official evidence is incomplete or conflicting",
                    },
                )
        for field_name in source_rosters.missing:
            active_gaps.add((None, "monthly_universe_source_mismatch", field_name))
            _open_gap_issue(
                session,
                month_start=month_start,
                ipo=None,
                issue_type="monthly_universe_source_mismatch",
                field_name=field_name,
                details={
                    "reason": "official monthly source roster is missing or does not match",
                    **source_rosters.details,
                },
            )

    resolved_monthly = _resolve_recovered_monthly_issues(
        session,
        month_start=month_start,
        active_gaps=active_gaps,
    )
    recovered_required = _resolve_stale_required_field_issues(session, assessments)
    recovered_documents = _resolve_recovered_document_issues(session, assessments)

    ready = bool(core) and source_rosters.ready and all(item.ready for item in assessments)
    has_any_official_pair = bool(core) and all(_has_source_pair(session, ipo.id) for ipo in core)
    state = "cross_checked" if ready else ("reported" if has_any_official_pair else "unreconciled")
    core_only_universe = MonthlyUniverseResult(
        month_start=universe.month_start,
        month_end=universe.month_end,
        core=universe.core,
        filing_update_appendix=(),
    )
    snapshots = save_monthly_universe_snapshot(
        session,
        core_only_universe,
        reconciliation_state=state,
        reconciliation_sources=(KIND_SOURCE, KIND_LISTING_SOURCE, DART_SOURCE),
    )
    session.flush()

    result_rows = [
        {
            "ipo_id": item.ipo.id,
            "offering_id": item.ipo.offering_id,
            "corp_name": item.ipo.corp_name,
            "stage": item.stage,
            "ready": item.ready,
            "missing_fields": list(item.missing),
        }
        for item in assessments
    ]
    completed = sum(item.ready for item in assessments)
    return {
        "month_start": month_start.isoformat(),
        "month_end": month_end.isoformat(),
        "as_of": effective_as_of.isoformat(),
        "processed": len(assessments),
        "success": completed,
        "partial": len(assessments) - completed,
        "ready": ready,
        "reconciliation_state": state,
        "reconciliation_sources": [KIND_SOURCE, KIND_LISTING_SOURCE, DART_SOURCE],
        "source_rosters": source_rosters.details,
        "source_roster_ready": source_rosters.ready,
        "monthly_snapshots_saved": len(snapshots),
        "data_quality_gaps": (
            sum(len(item.missing) for item in assessments)
            + len(source_rosters.missing)
            + (0 if core else 1)
        ),
        "monthly_issues_resolved": resolved_monthly,
        "required_field_issues_resolved": recovered_required,
        "document_issues_resolved": recovered_documents,
        "filing_update_appendix_count": len(universe.filing_update_appendix),
        "offerings": result_rows,
    }


def _assess_source_rosters(
    session: Session,
    universe: MonthlyUniverseResult,
    as_of: datetime,
) -> _SourceRosterAssessment:
    core = tuple(entry.ipo for entry in universe.core)
    core_names = _display_names(core)
    core_normalized = _normalized_names(core_names)

    public_snapshot = _latest_covering_snapshot(
        session,
        KIND_SOURCE,
        universe.month_start,
        universe.month_end,
        as_of,
    )
    listing_snapshot = _latest_covering_snapshot(
        session,
        KIND_LISTING_SOURCE,
        universe.month_start,
        universe.month_end,
        as_of,
    )
    public_records = _parse_snapshot_records(
        public_snapshot,
        marker="<!-- KIND_PAGE -->",
        parser=parse_kind_puboffering_html,
    )
    listing_records = _parse_snapshot_records(
        listing_snapshot,
        marker="<!-- KIND_LISTING_PAGE -->",
        parser=parse_kind_listing_html,
    )
    public_names = _display_names(
        record
        for record in public_records or ()
        if _record_has_monthly_activity(record, universe.month_start, universe.month_end)
    )
    listing_names = _display_names(
        record
        for record in listing_records or ()
        if _date_in_month(
            getattr(record, "listing_date", None),
            universe.month_start,
            universe.month_end,
        )
    )
    as_of_date = as_of.astimezone(SEOUL).date()
    expected_listing_names = _display_names(
        ipo
        for ipo in core
        if _date_in_month(
            ipo.actual_listing_date or ipo.listing_date,
            universe.month_start,
            universe.month_end,
        )
        and (ipo.actual_listing_date or ipo.listing_date) <= as_of_date
    )
    dart_names = _dart_roster_names(session, core, as_of)

    missing: list[str] = []
    if public_snapshot is None:
        missing.append("universe.kind_public_offering_snapshot")
    elif public_records is None:
        missing.append("universe.kind_public_offering_parse")
    elif _normalized_names(public_names) != core_normalized:
        missing.append("universe.kind_public_offering_name_set")
    if listing_snapshot is None:
        missing.append("universe.kind_listing_snapshot")
    elif listing_records is None:
        missing.append("universe.kind_listing_parse")
    elif _normalized_names(listing_names) != _normalized_names(expected_listing_names):
        missing.append("universe.kind_listing_name_set")
    if _normalized_names(dart_names) != core_normalized:
        missing.append("universe.dart_lineage_name_set")
    normalized_missing = tuple(sorted(set(missing)))
    return _SourceRosterAssessment(
        ready=not normalized_missing,
        missing=normalized_missing,
        core_names=core_names,
        kind_public_offering_names=public_names,
        kind_listing_names=listing_names,
        dart_names=dart_names,
        expected_listing_names=expected_listing_names,
    )


def _latest_covering_snapshot(
    session: Session,
    source: str,
    month_start: date,
    month_end: date,
    as_of: datetime,
) -> Optional[SourceSnapshot]:
    snapshots = session.scalars(
        select(SourceSnapshot)
        .where(SourceSnapshot.source == source)
        .order_by(desc(SourceSnapshot.fetched_at), desc(SourceSnapshot.id))
    ).all()
    return next(
        (
            snapshot
            for snapshot in snapshots
            if _as_utc(snapshot.fetched_at) <= as_of
            and _source_key_covers(snapshot.source_key, month_start, month_end)
        ),
        None,
    )


def _source_key_covers(value: str, month_start: date, month_end: date) -> bool:
    try:
        start_text, end_text = value.split(":", 1)
        start = date.fromisoformat(start_text)
        end = date.fromisoformat(end_text)
    except (AttributeError, TypeError, ValueError):
        return False
    return start <= month_start and end >= month_end


def _parse_snapshot_records(
    snapshot: Optional[SourceSnapshot],
    *,
    marker: str,
    parser,
) -> Optional[tuple[Any, ...]]:
    if snapshot is None or not snapshot.raw_text:
        return None
    records: list[Any] = []
    try:
        for index, page in enumerate(snapshot.raw_text.split(marker)):
            records.extend(parser(page, allow_empty=index > 0))
    except (ExternalSourceError, TypeError, ValueError):
        return None
    return tuple(records)


def _record_has_monthly_activity(record: Any, month_start: date, month_end: date) -> bool:
    return any(
        _dates_overlap(start, end, month_start, month_end)
        for start, end in (
            (
                getattr(record, "demand_start_date", None),
                getattr(record, "demand_end_date", None),
            ),
            (
                getattr(record, "subscription_start_date", None),
                getattr(record, "subscription_end_date", None),
            ),
        )
    ) or any(
        _date_in_month(getattr(record, field_name, None), month_start, month_end)
        for field_name in ("refund_date", "payment_date", "listing_date")
    )


def _dates_overlap(
    start: Optional[date],
    end: Optional[date],
    month_start: date,
    month_end: date,
) -> bool:
    if start is None and end is None:
        return False
    effective_start = start or end
    effective_end = end or start
    return bool(effective_start <= month_end and effective_end >= month_start)


def _date_in_month(value: Optional[date], month_start: date, month_end: date) -> bool:
    return value is not None and month_start <= value <= month_end


def _display_names(values: Iterable[Any]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                str(getattr(value, "corp_name", value)).strip()
                for value in values
                if str(getattr(value, "corp_name", value)).strip()
            }
        )
    )


def _normalized_names(values: Iterable[str]) -> set[str]:
    return {normalize_company_name(value) for value in values if normalize_company_name(value)}


def _dart_roster_names(session: Session, core: Iterable[Ipo], as_of: datetime) -> tuple[str, ...]:
    ipo_ids = tuple(ipo.id for ipo in core)
    if not ipo_ids:
        return ()
    documents = session.scalars(
        select(SourceDocument).where(
            SourceDocument.ipo_id.in_(ipo_ids),
            SourceDocument.source == DART_SOURCE,
        )
    ).all()
    return _display_names(
        str((document.metadata_json or {}).get("corp_name") or "")
        for document in documents
        if _safe_document(document)
        and _as_utc(document.published_at) <= as_of
        and _as_utc(document.fetched_at) <= as_of
    )


def _assess_offering(
    session: Session,
    ipo: Ipo,
    as_of: datetime,
    *,
    duplicate_offering_ids: set[str],
) -> _OfferingAssessment:
    as_of_date = as_of.astimezone(SEOUL).date()
    post_demand = ipo.demand_end_date is not None and as_of_date > ipo.demand_end_date
    post_close = ipo.subscription_end_date is not None and as_of_date > ipo.subscription_end_date
    stage = "POST_CLOSE" if post_close else ("POST_DEMAND" if post_demand else "PRE_OPEN")

    documents = tuple(
        session.scalars(select(SourceDocument).where(SourceDocument.ipo_id == ipo.id)).all()
    )
    documents_by_id = {document.id: document for document in documents}
    observations = tuple(
        session.scalars(select(IpoFactObservation).where(IpoFactObservation.ipo_id == ipo.id)).all()
    )
    latest = _latest_facts(observations, documents_by_id)
    kind = latest.get(KIND_SOURCE, {})
    dart = latest.get(DART_SOURCE, {})
    missing: list[str] = []

    if not _stable_offering_id(ipo.offering_id):
        missing.append("identity.offering_id")
    if ipo.offering_id in duplicate_offering_ids:
        missing.append("identity.offering_id_duplicate")
    if ipo.is_sample or (ipo.data_origin or "").lower() in NON_PRODUCTION_ORIGINS:
        missing.append("identity.production_origin")
    if not ipo.kind_biz_no:
        missing.append("identity.kind_biz_no")
    if not ipo.dart_corp_code:
        missing.append("identity.dart_corp_code")

    safe_kind_documents = tuple(
        item for item in documents if item.source == KIND_SOURCE and _safe_document(item)
    )
    safe_dart_documents = tuple(
        item for item in documents if item.source == DART_SOURCE and _safe_document(item)
    )
    if not safe_kind_documents:
        missing.append("source.kind_document")
    if not safe_dart_documents:
        missing.append("source.dart_document")
    if safe_kind_documents and not _source_identity_matches(ipo, safe_kind_documents, KIND_SOURCE):
        missing.append("identity.kind_name_conflict")
    if safe_dart_documents and not _source_identity_matches(ipo, safe_dart_documents, DART_SOURCE):
        missing.append("identity.dart_name_conflict")

    kind_fields = list(_BASE_KIND_FIELDS)
    if ipo.payment_date is not None:
        kind_fields.append("payment_date")
    if ipo.refund_date is not None and ipo.refund_date_source == KIND_SOURCE:
        kind_fields.append("refund_date")
    for field_name in kind_fields:
        if field_name not in kind:
            missing.append(f"kind.{field_name}")

    roster = _underwriter_roster(session, ipo, kind.get("underwriter_names"))
    if not roster:
        missing.append("current.underwriter_roster")
    else:
        for broker_name in roster:
            role_keys = (
                f"underwriter:{broker_name}:role",
                f"underwriter.{broker_name}.role",
            )
            if not any(key in kind for key in role_keys) and not _current_broker_has_role(
                session, ipo.id, broker_name
            ):
                missing.append(f"kind.underwriter.{broker_name}.role")

    recovered_dart_fields = set(dart)
    if any(key.startswith("institutional_price_distribution.") for key in dart):
        recovered_dart_fields.add("institutional_price_distribution")

    if post_demand:
        for field_name in (*_DEMAND_FIELDS, *_STRUCTURE_FIELDS):
            if field_name not in dart:
                missing.append(f"dart.{field_name}")
        if "institutional_price_distribution" not in recovered_dart_fields:
            missing.append("dart.institutional_price_distribution")
        missing.extend(_current_projection_gaps(session, ipo, dart))
        kind_offer = kind.get("offer_price_final")
        dart_offer = dart.get("offer_price_final")
        if kind_offer and dart_offer and not _values_equal(kind_offer.value, dart_offer.value):
            missing.append("cross_source.offer_price_final_conflict")

    broker_details_complete = True
    if post_close:
        for field_name in _RETAIL_FIELDS:
            if field_name not in dart:
                missing.append(f"dart.{field_name}")
        dart_brokers = _dart_result_brokers(dart)
        if dart_brokers - set(roster):
            missing.append("cross_source.underwriter_roster_conflict")
        for broker_name in roster:
            for result_field in _BROKER_RESULT_FIELDS:
                key = f"broker.{broker_name}.{result_field}"
                if key not in dart:
                    broker_details_complete = False
                    missing.append(f"dart.{key}")
        if not roster:
            broker_details_complete = False
        if broker_details_complete:
            recovered_dart_fields.add("retail_broker_details")

    return _OfferingAssessment(
        ipo=ipo,
        stage=stage,
        missing=tuple(sorted(set(missing))),
        recovered_dart_fields=frozenset(recovered_dart_fields),
        broker_details_complete=broker_details_complete,
    )


def _latest_facts(
    observations: Iterable[IpoFactObservation],
    documents_by_id: dict[int, SourceDocument],
) -> dict[str, dict[str, _Fact]]:
    grouped: dict[str, dict[str, list[_Fact]]] = {}
    for observation in observations:
        document = documents_by_id.get(observation.source_document_id)
        if document is not None and document.source in {KIND_SOURCE, DART_SOURCE}:
            grouped.setdefault(document.source, {}).setdefault(observation.field_name, []).append(
                _Fact(observation, document)
            )
    selected: dict[str, dict[str, _Fact]] = {}
    for source, fields in grouped.items():
        for field_name, rows in fields.items():
            latest = max(rows, key=_fact_sort_key)
            if _safe_fact(latest):
                selected.setdefault(source, {})[field_name] = latest
    return selected


def _safe_document(document: SourceDocument) -> bool:
    return bool(
        document.official_url
        and document.published_at
        and document.fetched_at
        and document.raw_text_hash
        and document.parser_name
        and document.parser_version
    )


def _safe_fact(fact: _Fact) -> bool:
    observation = fact.observation
    return bool(
        _safe_document(fact.document)
        and observation.value_json is not None
        and observation.quality_state in SAFE_QUALITY_STATES
        and observation.published_at
        and observation.observed_at
        and observation.fetched_at
        and observation.parser_name
        and observation.parser_version
        and observation.raw_text_hash
    )


def _fact_sort_key(fact: _Fact) -> tuple[datetime, datetime, int]:
    observation = fact.observation
    return (
        _as_utc(observation.effective_from or observation.published_at),
        _as_utc(observation.observed_at),
        observation.id or 0,
    )


def _source_identity_matches(ipo: Ipo, documents: Iterable[SourceDocument], source: str) -> bool:
    expected = normalize_company_name(ipo.corp_name)
    observed_names = set()
    for document in documents:
        metadata = document.metadata_json or {}
        if source == DART_SOURCE and metadata.get("corp_name"):
            observed_names.add(normalize_company_name(str(metadata["corp_name"])))
        elif source == KIND_SOURCE:
            raw_row = metadata.get("raw_row") or {}
            cells = raw_row.get("cells") if isinstance(raw_row, dict) else None
            if cells and cells[0]:
                observed_names.add(normalize_company_name(str(cells[0])))
    return not observed_names or observed_names == {expected}


def _underwriter_roster(session: Session, ipo: Ipo, kind_names: Optional[_Fact]) -> tuple[str, ...]:
    rows = tuple(
        session.scalars(
            select(UnderwriterAllocation)
            .where(UnderwriterAllocation.ipo_id == ipo.id)
            .order_by(UnderwriterAllocation.id)
        ).all()
    )
    roster = {
        normalize_broker_name(row.broker_name)
        for row in rows
        if normalize_broker_name(row.broker_name)
    }
    value = kind_names.value if kind_names else None
    if isinstance(value, list):
        roster.update(
            normalize_broker_name(str(name)) for name in value if normalize_broker_name(str(name))
        )
    return tuple(sorted(roster))


def _current_broker_has_role(session: Session, ipo_id: int, broker_name: str) -> bool:
    rows = session.scalars(
        select(UnderwriterAllocation).where(UnderwriterAllocation.ipo_id == ipo_id)
    ).all()
    return any(
        normalize_broker_name(row.broker_name) == broker_name and bool(row.role) for row in rows
    )


def _dart_result_brokers(dart: dict[str, _Fact]) -> set[str]:
    brokers = set()
    for field_name in dart:
        parts = field_name.split(".")
        if len(parts) >= 3 and parts[0] == "broker":
            brokers.add(normalize_broker_name(".".join(parts[1:-1])))
    return {broker for broker in brokers if broker}


def _current_projection_gaps(session: Session, ipo: Ipo, dart: dict[str, _Fact]) -> list[str]:
    missing: list[str] = []
    demand = session.scalars(
        select(DemandForecast)
        .where(DemandForecast.ipo_id == ipo.id)
        .order_by(desc(DemandForecast.result_published_at), desc(DemandForecast.id))
    ).first()
    demand_mapping = {
        "institutional_competition_ratio": "institutional_competition_ratio",
        "participants_count": "participants_count",
        "lockup_commitment_ratio": "lockup_commitment_ratio",
    }
    if demand is None or not str(demand.source or "").startswith(DART_SOURCE):
        missing.append("current.demand_forecast")
    else:
        for observation_name, current_name in demand_mapping.items():
            fact = dart.get(observation_name)
            current_value = getattr(demand, current_name)
            if (
                fact is None
                or current_value is None
                or not _values_equal(fact.value, current_value)
            ):
                missing.append(f"current.demand_forecast.{current_name}")

    structure = session.scalars(
        select(IpoStructureMetric)
        .where(IpoStructureMetric.ipo_id == ipo.id)
        .order_by(desc(IpoStructureMetric.as_of_date), desc(IpoStructureMetric.id))
    ).first()
    if structure is None or not str(structure.source or "").startswith(DART_SOURCE):
        missing.append("current.structure_metric")
        return missing

    comparisons = {
        "total_offered_shares": structure.total_offer_shares,
        "free_float_shares": structure.free_float_shares_on_listing,
        "free_float_ratio": structure.free_float_ratio_on_listing,
    }
    for observation_name, current_value in comparisons.items():
        fact = dart.get(observation_name)
        if fact is None or current_value is None or not _values_equal(fact.value, current_value):
            missing.append(f"current.structure_metric.{observation_name}")
    total = dart.get("total_offered_shares")
    if total and total.value:
        for observation_name, current_value in (
            ("new_shares", structure.new_share_ratio),
            ("secondary_shares", structure.secondary_share_ratio),
        ):
            fact = dart.get(observation_name)
            expected = float(fact.value) / float(total.value) if fact else None
            if (
                expected is None
                or current_value is None
                or not _values_equal(expected, current_value)
            ):
                missing.append(f"current.structure_metric.{observation_name}")
    return missing


def _open_gap_issue(
    session: Session,
    *,
    month_start: date,
    ipo: Optional[Ipo],
    issue_type: str,
    field_name: str,
    details: dict[str, Any],
) -> None:
    upsert_data_quality_issue(
        session,
        issue_type=issue_type,
        severity="error",
        status="open",
        ipo_id=ipo.id if ipo else None,
        field_name=field_name,
        details={
            "month_start": month_start.isoformat(),
            "offering_id": ipo.offering_id if ipo else None,
            **details,
        },
        values={"resolved_at": None, "resolution_note": None},
    )


def _resolve_recovered_monthly_issues(
    session: Session,
    *,
    month_start: date,
    active_gaps: set[tuple[Optional[int], str, str]],
) -> int:
    issues = session.scalars(
        select(DataQualityIssue).where(
            DataQualityIssue.issue_type.in_(
                {
                    "monthly_universe_empty",
                    "monthly_universe_source_mismatch",
                    "monthly_identity_gap",
                    "monthly_evidence_gap",
                }
            ),
            DataQualityIssue.status == "open",
        )
    ).all()
    resolved = 0
    for issue in issues:
        details = issue.details_json or {}
        if details.get("month_start") != month_start.isoformat():
            continue
        key = (issue.ipo_id, issue.issue_type, issue.field_name or "")
        if key not in active_gaps:
            _resolve_issue(issue, "monthly evidence gap was recovered")
            resolved += 1
    return resolved


def _resolve_stale_required_field_issues(
    session: Session, assessments: Iterable[_OfferingAssessment]
) -> int:
    by_ipo = {item.ipo.id: item for item in assessments}
    issues = session.scalars(
        select(DataQualityIssue).where(
            DataQualityIssue.issue_type == "required_fields_missing",
            DataQualityIssue.status == "open",
            DataQualityIssue.ipo_id.in_(tuple(by_ipo)),
        )
    ).all()
    resolved = 0
    for issue in issues:
        assessment = by_ipo.get(issue.ipo_id)
        details = issue.details_json or {}
        fields = details.get("fields") if isinstance(details, dict) else None
        if not assessment or not isinstance(fields, list):
            continue
        if all(str(field) in assessment.recovered_dart_fields for field in fields):
            _resolve_issue(
                issue,
                "required current evidence was recovered by monthly reconciliation",
            )
            resolved += 1
    return resolved


def _resolve_recovered_document_issues(
    session: Session,
    assessments: Iterable[_OfferingAssessment],
) -> int:
    """Close stale parser issues only when their concrete recovery is provable."""
    ready_by_ipo = {item.ipo.id: item for item in assessments if item.ready}
    if not ready_by_ipo:
        return 0
    issues = session.scalars(
        select(DataQualityIssue).where(
            DataQualityIssue.issue_type.in_(("parse_warning", "missing_evidence")),
            DataQualityIssue.status == "open",
            DataQualityIssue.ipo_id.in_(tuple(ready_by_ipo)),
        )
    ).all()
    resolved = 0
    for issue in issues:
        document = (
            session.get(SourceDocument, issue.source_document_id)
            if issue.source_document_id is not None
            else None
        )
        if document is None:
            continue
        if issue.issue_type == "parse_warning":
            details = issue.details_json or {}
            warning = details.get("warning") if isinstance(details, dict) else None
            recovered = warning == "no_supported_ipo_fields" and (
                _document_has_facts(session, document.id)
                or _document_is_superseded(session, document)
            )
            note = "the parser warning was recovered or superseded by official facts"
        else:
            recovered = _document_is_superseded(session, document)
            note = "the empty revision was superseded by a later official revision"
        if recovered:
            _resolve_issue(issue, note)
            resolved += 1
    return resolved


def _document_has_facts(session: Session, document_id: int) -> bool:
    return (
        session.scalars(
            select(IpoFactObservation.id).where(
                IpoFactObservation.source_document_id == document_id,
                IpoFactObservation.value_json.is_not(None),
            )
        ).first()
        is not None
    )


def _document_is_superseded(session: Session, document: SourceDocument) -> bool:
    revisions = session.scalars(
        select(SourceDocument).where(
            SourceDocument.ipo_id == document.ipo_id,
            SourceDocument.source == document.source,
            SourceDocument.lineage_root_key == document.lineage_root_key,
            SourceDocument.id != document.id,
        )
    ).all()
    current_key = (_as_utc(document.published_at), document.id or 0)
    return any(
        (_as_utc(revision.published_at), revision.id or 0) > current_key for revision in revisions
    )


def _resolve_issue(issue: DataQualityIssue, note: str) -> None:
    issue.status = "resolved"
    issue.resolved_at = datetime.now(timezone.utc)
    issue.resolution_note = note


def _has_source_pair(session: Session, ipo_id: int) -> bool:
    sources = set(
        session.scalars(select(SourceDocument.source).where(SourceDocument.ipo_id == ipo_id)).all()
    )
    return {KIND_SOURCE, DART_SOURCE} <= sources


def _duplicate_offering_ids(ipos: Iterable[Ipo]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for ipo in ipos:
        value = ipo.offering_id or ""
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates


def _stable_offering_id(value: Optional[str]) -> bool:
    normalized = (value or "").strip().lower()
    return bool(normalized) and not normalized.startswith(("temp:", "offering:temp:"))


def _values_equal(first: Any, second: Any) -> bool:
    if isinstance(first, (int, float)) and isinstance(second, (int, float)):
        return isclose(float(first), float(second), rel_tol=1e-8, abs_tol=1e-8)
    return first == second


def _as_utc(value: Optional[datetime]) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
