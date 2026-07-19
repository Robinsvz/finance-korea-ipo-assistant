from __future__ import annotations

import hashlib
import importlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from types import ModuleType
from typing import Any, Callable, Iterable, Optional, Union
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from sqlalchemy import and_, desc, or_, select
from sqlalchemy.orm import Session

from app.collectors.dart import (
    DartDocumentFamily,
    DartDocumentParseResult,
    RawEvidence,
    classify_dart_document,
    classify_dart_document_family,
    parse_dart_document,
)
from app.collectors.dart_public import DartPublicCollector, PublicDartFiling, PublicDartSection
from app.db import init_db, session_scope
from app.models import (
    DataQualityIssue,
    Ipo,
    IpoFactObservation,
    IpoStructureMetric,
    SourceDocument,
    SourceSnapshot,
)
from app.repositories import (
    append_fact_observation,
    append_schedule_revision,
    get_ipo_or_raise,
    list_monthly_universe,
    save_monthly_universe_snapshot,
    save_source_document,
    save_source_snapshot,
    upsert_data_quality_issue,
    upsert_demand_forecast,
)
from app.services.normalization import normalize_company_name
from app.services.public_financial import PublicFinancialSummary, parse_public_financial_summary

SEOUL = ZoneInfo("Asia/Seoul")
OFFICIAL_SOURCE = "dart_public"
STRUCTURE_SOURCE = "dart_public_official"
_PRODUCTION_BLOCKED_ORIGINS = {"sample", "synthetic", "test"}
_SECTION_KEYWORDS = {
    DartDocumentFamily.SECURITIES_REGISTRATION: (
        "모집 또는 매출",
        "수요예측",
        "공모가격",
        "유통가능",
        "인수",
    ),
    DartDocumentFamily.INVESTMENT_PROSPECTUS: (
        "모집 또는 매출",
        "수요예측",
        "공모가격",
        "유통가능",
        "인수",
    ),
    DartDocumentFamily.ISSUANCE_RESULT: ("청약 및 배정", "청약", "배정", "발행실적"),
    DartDocumentFamily.WITHDRAWAL: ("철회",),
}


@dataclass(frozen=True)
class _ObservationValue:
    field_name: str
    value: Any
    unit: Optional[str]
    quality_state: str
    knowledge_phase: str
    published_at: Optional[datetime]
    observed_at: datetime
    parser_name: Optional[str]
    parser_version: Optional[str]


def collect_public_dart_evidence_for_ipo(
    identifier: Union[str, int],
    collector: Optional[DartPublicCollector] = None,
    observed_at: Optional[datetime] = None,
) -> dict[str, Any]:
    """Collect official public-DART evidence for one stable IPO offering.

    Network access is delegated to ``collector`` so the orchestration remains
    deterministic under fixtures. Every exception after IPO resolution is converted
    into a result and a DQ issue; one bad filing never discards other revisions.
    """
    init_db()
    effective_observed_at = _aware_utc(observed_at or datetime.now(timezone.utc))
    effective_collector = collector or DartPublicCollector()
    with session_scope() as session:
        ipo = get_ipo_or_raise(session, identifier)
        result = _empty_ipo_result(ipo)
        if ipo.is_sample or ipo.data_origin in _PRODUCTION_BLOCKED_ORIGINS:
            result.update(status="skipped", error="non-production IPO")
            return result
        return _collect_for_ipo(session, ipo, effective_collector, effective_observed_at)


def collect_public_dart_evidence_for_month(
    month_start: date,
    month_end: date,
    collector: Optional[DartPublicCollector] = None,
) -> dict[str, Any]:
    """Collect a monthly core and filing-appendix universe with per-IPO isolation."""
    if month_start > month_end:
        raise ValueError("month_start must be on or before month_end")
    init_db()
    with session_scope() as session:
        universe = list_monthly_universe(session, month_start, month_end)
        identifiers = tuple(
            dict.fromkeys(
                entry.ipo_id for entry in (*universe.core, *universe.filing_update_appendix)
            )
        )
    effective_collector = collector or DartPublicCollector()
    results: list[dict[str, Any]] = []
    for identifier in identifiers:
        try:
            item = collect_public_dart_evidence_for_ipo(identifier, effective_collector)
        except Exception as exc:  # an IPO must not abort the monthly batch
            item = {
                "status": "failed",
                "ipo_id": identifier,
                "corp_name": None,
                "error": f"{type(exc).__name__}: {exc}",
            }
        results.append(item)
    reconciliation_state = (
        "reported"
        if results and all(item.get("reconciliation_state") == "reported" for item in results)
        else "unreconciled"
    )
    with session_scope() as session:
        current_universe = list_monthly_universe(session, month_start, month_end)
        snapshots = save_monthly_universe_snapshot(
            session,
            current_universe,
            reconciliation_state=reconciliation_state,
            reconciliation_sources=(OFFICIAL_SOURCE,),
        )
        session.flush()
        snapshot_count = len(snapshots)
    result = _month_result(month_start, month_end, results)
    result["monthly_snapshot_state"] = reconciliation_state
    result["monthly_snapshots_saved"] = snapshot_count
    return result


def backfill_stored_public_financial_evidence_for_month(
    month_start: date,
    month_end: date,
) -> dict[str, Any]:
    """Parse recent actual financials from already-preserved official HTML.

    The backfill performs no network call.  It selects the latest preserved
    investment prospectus for each production IPO in the monthly universe and
    appends field-level observations without rewriting the immutable source.
    """

    if month_start > month_end:
        raise ValueError("month_start must be on or before month_end")
    init_db()
    processed = 0
    parsed_count = 0
    observation_count = 0
    missing: list[str] = []
    with session_scope() as session:
        universe = list_monthly_universe(session, month_start, month_end)
        ipo_ids = tuple(dict.fromkeys(entry.ipo_id for entry in universe.core))
        resolved_ipos = tuple(session.get(Ipo, ipo_id) for ipo_id in ipo_ids)
        production_ipos = tuple(
            ipo
            for ipo in resolved_ipos
            if ipo is not None
            and not ipo.is_sample
            and ipo.data_origin not in _PRODUCTION_BLOCKED_ORIGINS
        )
        for ipo in production_ipos:
            processed += 1
            document = (
                session.execute(
                    select(SourceDocument)
                    .where(
                        SourceDocument.ipo_id == ipo.id,
                        SourceDocument.source == OFFICIAL_SOURCE,
                        SourceDocument.document_type == "investment_prospectus",
                    )
                    .order_by(
                        SourceDocument.published_at.desc(),
                        SourceDocument.id.desc(),
                    )
                )
                .scalars()
                .first()
            )
            snapshot = (
                session.get(SourceSnapshot, document.source_snapshot_id)
                if document is not None and document.source_snapshot_id is not None
                else None
            )
            summary = (
                parse_public_financial_summary(snapshot.raw_text)
                if snapshot is not None and snapshot.raw_text
                else None
            )
            if document is None or summary is None:
                missing.append(ipo.corp_name)
                continue
            parsed_count += 1
            observations = _financial_observations(
                summary,
                ipo,
                document.published_at,
                document.fetched_at,
            )
            structure = _latest_structure_metric(session, ipo.id)
            observations += _derived_valuation_observations(
                ipo,
                document.published_at,
                document.fetched_at,
                offer_price=ipo.offer_price_final,
                post_listing_shares=_latest_observation_integer(
                    session, ipo.id, "post_listing_shares"
                ),
                free_float_shares=(
                    structure.free_float_shares_on_listing if structure is not None else None
                ),
            )
            observation_count += _append_observations(session, ipo, document, observations)
    return {
        "processed": processed,
        "financial_summaries": parsed_count,
        "observations": observation_count,
        "missing": missing,
    }


def _collect_for_ipo(
    session: Session,
    ipo: Ipo,
    collector: DartPublicCollector,
    observed_at: datetime,
) -> dict[str, Any]:
    result = _empty_ipo_result(ipo)
    search_start, search_end = _search_window(ipo, observed_at)
    try:
        filings = collector.search_filings(
            ipo.corp_name,
            start=search_start,
            end=search_end,
            final_only=False,
        )
    except Exception as exc:
        _issue(
            session,
            result,
            ipo,
            "source_fetch_error",
            "error",
            {"step": "search", "error": f"{type(exc).__name__}: {exc}"},
        )
        result.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        return result

    exact = tuple(
        filing
        for filing in filings
        if normalize_company_name(filing.corp_name) == normalize_company_name(ipo.corp_name)
    )
    result["filings_found"] = len(exact)
    if not exact:
        _issue(
            session,
            result,
            ipo,
            "missing_source",
            "error",
            {"field": "ipo_filing", "searched_company": ipo.corp_name},
        )
        result.update(status="partial", error="exact-company IPO filing not found")
        return result

    corp_codes = {filing.corp_code for filing in exact if filing.corp_code}
    if len(corp_codes) != 1:
        _issue(
            session,
            result,
            ipo,
            "ambiguous_identity",
            "error",
            {"corp_codes": sorted(corp_codes)},
        )
        result.update(status="partial", error="DART corp identity is ambiguous")
        return result
    corp_code = next(iter(corp_codes))
    if ipo.dart_corp_code and ipo.dart_corp_code != corp_code:
        _issue(
            session,
            result,
            ipo,
            "identity_conflict",
            "error",
            {"stored": ipo.dart_corp_code, "observed": corp_code},
        )
        result.update(status="partial", error="stored DART corp code conflicts with filing")
        return result
    ipo.dart_corp_code = corp_code

    selected = _select_effective_filings(exact)
    result["filings_selected"] = len(selected)
    if not selected:
        _issue(
            session,
            result,
            ipo,
            "missing_source",
            "error",
            {"field": "supported_ipo_filing", "receipts": [row.receipt_no for row in exact]},
        )
        result.update(status="partial", error="supported IPO filing not found")
        return result

    result["identity_state"] = "reported"
    result["lineage_receipts"] = [filing.receipt_no for filing in selected]
    current_receipts = _current_effective_receipts(selected)
    ipo.dart_receipt_no = selected[-1].receipt_no
    for filing in selected:
        try:
            filing_counts = _collect_filing(
                session,
                ipo,
                filing,
                collector,
                observed_at,
                update_current=filing.receipt_no in current_receipts,
            )
        except Exception as exc:
            _issue(
                session,
                result,
                ipo,
                "parse_error",
                "error",
                {
                    "receipt_no": filing.receipt_no,
                    "step": "filing",
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            result["warnings"].append(f"{filing.receipt_no}: {type(exc).__name__}")
            result["failed_receipts"].append(filing.receipt_no)
            continue
        for key, value in filing_counts.items():
            if key == "warnings":
                result[key].extend(value)
            else:
                result[key] += value
    result["reconciliation_state"] = (
        "reported" if result["source_documents"] == len(selected) else "unreconciled"
    )
    blocking_evidence = bool(
        result["failed_receipts"]
        or result["missing_required_fields"]
        or result["blocking_warnings"]
    )
    result["evidence_state"] = "incomplete" if blocking_evidence else "complete"
    result["status"] = "partial" if blocking_evidence else "success"
    return result


def _collect_filing(
    session: Session,
    ipo: Ipo,
    filing: PublicDartFiling,
    collector: DartPublicCollector,
    observed_at: datetime,
    *,
    update_current: bool,
) -> dict[str, Any]:
    counts: dict[str, Any] = {
        "source_documents": 0,
        "source_snapshots": 0,
        "observations": 0,
        "schedule_revisions": 0,
        "demand_forecasts": 0,
        "structure_metrics": 0,
        "data_quality_issues": 0,
        "warnings": [],
        "blocking_warnings": [],
        "missing_required_fields": [],
    }
    family = classify_dart_document_family(filing.report_name)
    published_at = _published_datetime(filing.submitted_on, observed_at)
    keywords = _SECTION_KEYWORDS[family]
    sections = collector.fetch_matching_sections(filing.receipt_no, *keywords)
    if not sections:
        sections = collector.fetch_matching_sections(filing.receipt_no)

    section_snapshots = []
    for section, raw_html in sections:
        snapshot = save_source_snapshot(
            session,
            OFFICIAL_SOURCE,
            _section_source_key(filing.receipt_no, section),
            raw_html,
            ipo_id=ipo.id,
            payload_json=_section_metadata(section, filing),
        )
        section_snapshots.append(snapshot)
        counts["source_snapshots"] += 1

    aggregate = _aggregate_sections(sections)
    aggregate_snapshot = None
    if aggregate:
        aggregate_snapshot = save_source_snapshot(
            session,
            OFFICIAL_SOURCE,
            f"{filing.receipt_no}:aggregate",
            aggregate,
            ipo_id=ipo.id,
            payload_json={
                "receipt_no": filing.receipt_no,
                "report_name": filing.report_name,
                "section_count": len(sections),
            },
        )
        counts["source_snapshots"] += 1
    session.flush()

    parsed = None
    parse_error = None
    if aggregate:
        try:
            parsed = parse_dart_document(
                aggregate,
                filing.report_name,
                receipt_no=filing.receipt_no,
                published_at=published_at,
            )
        except Exception as exc:
            parse_error = exc

    document = _save_document(
        session,
        ipo,
        filing,
        family,
        aggregate_snapshot,
        parsed,
        published_at,
        observed_at,
        sections,
    )
    counts["source_documents"] += 1
    session.flush()
    schedule_fields = _parse_official_schedule(aggregate)
    stable_observed_at = _aware_utc(document.fetched_at)
    append_schedule_revision(
        session,
        ipo_id=ipo.id,
        source_document_id=document.id,
        event_status=(
            "withdrawn"
            if family is DartDocumentFamily.WITHDRAWAL
            else ("scheduled" if schedule_fields else "filed")
        ),
        values={
            **schedule_fields,
            "withdrawal_reason": filing.report_name
            if family is DartDocumentFamily.WITHDRAWAL
            else None,
            "published_at": published_at,
            "observed_at": stable_observed_at,
            "effective_from": published_at,
        },
    )
    counts["schedule_revisions"] += 1

    if not aggregate:
        _filing_issue(
            session,
            counts,
            ipo,
            document,
            "missing_section",
            "error",
            {"receipt_no": filing.receipt_no},
        )
        counts["warnings"].append(f"{filing.receipt_no}: missing_section")
        if update_current:
            counts["blocking_warnings"].append(f"{filing.receipt_no}: missing_section")
        return counts
    if parse_error is not None:
        _filing_issue(
            session,
            counts,
            ipo,
            document,
            "parse_error",
            "error",
            {
                "receipt_no": filing.receipt_no,
                "error": f"{type(parse_error).__name__}: {parse_error}",
            },
        )
        counts["warnings"].append(f"{filing.receipt_no}: parse_error")
        if update_current:
            counts["blocking_warnings"].append(f"{filing.receipt_no}: parse_error")
        return counts

    assert parsed is not None
    for warning in parsed.warnings:
        _filing_issue(
            session,
            counts,
            ipo,
            document,
            "parse_warning",
            "warning",
            {"receipt_no": filing.receipt_no, "warning": warning},
        )
        counts["warnings"].append(f"{filing.receipt_no}: {warning}")
        if update_current:
            counts["blocking_warnings"].append(f"{filing.receipt_no}: {warning}")

    observations = _evidence_observations(parsed, ipo, observed_at)
    if family in {
        DartDocumentFamily.SECURITIES_REGISTRATION,
        DartDocumentFamily.INVESTMENT_PROSPECTUS,
    }:
        financial = parse_public_financial_summary(aggregate)
        if financial is not None:
            observations += _financial_observations(
                financial,
                ipo,
                published_at,
                stable_observed_at,
            )
        structure = parsed.offering_structure
        demand = parsed.institutional_demand
        observations += _derived_valuation_observations(
            ipo,
            published_at,
            stable_observed_at,
            offer_price=(
                demand.offer_price_final
                if demand is not None and demand.offer_price_final is not None
                else ipo.offer_price_final
            ),
            post_listing_shares=(structure.post_listing_shares if structure else None),
            free_float_shares=(structure.free_float_shares if structure else None),
        )
    counts["observations"] += _append_observations(session, ipo, document, observations)

    missing_required = _required_evidence_gaps(parsed) if update_current else ()
    if missing_required:
        _filing_issue(
            session,
            counts,
            ipo,
            document,
            "required_fields_missing",
            "error",
            {"receipt_no": filing.receipt_no, "fields": list(missing_required)},
        )
        counts["missing_required_fields"].extend(missing_required)
        counts["warnings"].append(f"{filing.receipt_no}: required_fields_missing")
        counts["blocking_warnings"].append(f"{filing.receipt_no}: required_fields_missing")
    else:
        if update_current:
            _resolve_required_field_issues(session, ipo.id, document.id)
        if not observations and family is not DartDocumentFamily.WITHDRAWAL:
            _filing_issue(
                session,
                counts,
                ipo,
                document,
                "missing_evidence",
                "error",
                {"receipt_no": filing.receipt_no, "document_family": family.value},
            )
            counts["warnings"].append(f"{filing.receipt_no}: missing_evidence")
            if update_current:
                counts["blocking_warnings"].append(f"{filing.receipt_no}: missing_evidence")

    if update_current and _update_demand_forecast(session, ipo, filing, parsed, document):
        counts["demand_forecasts"] += 1
    if update_current and _update_structure_metric(session, ipo, filing, parsed):
        counts["structure_metrics"] += 1
    return counts


def _save_document(
    session: Session,
    ipo: Ipo,
    filing: PublicDartFiling,
    family: DartDocumentFamily,
    snapshot: Any,
    parsed: Optional[DartDocumentParseResult],
    published_at: Optional[datetime],
    observed_at: datetime,
    sections: Iterable[tuple[PublicDartSection, str]],
) -> SourceDocument:
    lineage_root = f"dart:{ipo.dart_corp_code}:{family.value}"
    prior_predicates = [
        SourceDocument.ipo_id == ipo.id,
        SourceDocument.source == OFFICIAL_SOURCE,
        SourceDocument.lineage_root_key == lineage_root,
        SourceDocument.document_key != filing.receipt_no,
        SourceDocument.published_at.is_not(None),
    ]
    if published_at is not None:
        prior_predicates.append(
            or_(
                SourceDocument.published_at < published_at,
                and_(
                    SourceDocument.published_at == published_at,
                    SourceDocument.document_key < filing.receipt_no,
                ),
            )
        )
    prior = (
        session.execute(
            select(SourceDocument)
            .where(*prior_predicates)
            .order_by(desc(SourceDocument.published_at), desc(SourceDocument.id))
        )
        .scalars()
        .first()
    )
    parser_version = parsed.parser_version if parsed else None
    values = {
        "source_snapshot_id": snapshot.id if snapshot else None,
        "supersedes_document_id": prior.id if prior else None,
        "official_url": filing.url,
        "published_at": published_at,
        "fetched_at": observed_at,
        "raw_text_hash": snapshot.raw_text_hash if snapshot else None,
        "parser_name": "dart-ipo-evidence" if parsed else None,
        "parser_version": parser_version,
        "metadata_json": {
            "corp_code": filing.corp_code,
            "corp_name": filing.corp_name,
            "report_name": filing.report_name,
            "submitted_on": filing.submitted_on.isoformat() if filing.submitted_on else None,
            "published_precision": "date",
            "section_keys": [
                _section_source_key(filing.receipt_no, section) for section, _raw in sections
            ],
        },
    }
    document = save_source_document(
        session,
        ipo_id=ipo.id,
        source=OFFICIAL_SOURCE,
        document_key=filing.receipt_no,
        document_type=(
            parsed.document_type.value
            if parsed
            else classify_dart_document(filing.report_name).value
        ),
        lineage_root_key=lineage_root,
        values=values,
    )
    for field_name, value in values.items():
        if getattr(document, field_name) is None and value is not None:
            setattr(document, field_name, value)
    return document


def _evidence_observations(
    parsed: DartDocumentParseResult,
    ipo: Ipo,
    observed_at: datetime,
) -> tuple[_ObservationValue, ...]:
    builder = _resolve_evidence_builder()
    if builder is None:
        return tuple(_fallback_observation(item, ipo, observed_at) for item in parsed.raw_evidence)
    built = builder(
        parsed,
        observed_at=observed_at,
        canonical_ipo_id=ipo.offering_id,
        data_origin="official",
    )
    return tuple(
        _ObservationValue(
            field_name=str(item.field_name),
            value=item.value,
            unit=getattr(item, "unit", None),
            quality_state=_string_value(getattr(item, "quality_state", "reported")),
            knowledge_phase=_string_value(
                getattr(item, "knowledge_phase", _knowledge_phase(ipo, item.published_at))
            ),
            published_at=getattr(item, "published_at", None),
            observed_at=_aware_utc(getattr(item, "observed_at", observed_at)),
            parser_name=getattr(item, "parser_name", None),
            parser_version=getattr(item, "parser_version", parsed.parser_version),
        )
        for item in built
    )


def _financial_observations(
    summary: PublicFinancialSummary,
    ipo: Ipo,
    published_at: Optional[datetime],
    observed_at: datetime,
) -> tuple[_ObservationValue, ...]:
    values: tuple[tuple[str, Any, Optional[str]], ...] = (
        ("financial_period", summary.period, None),
        ("financial_revenue", _decimal_number(summary.revenue), summary.unit),
        (
            "financial_operating_profit",
            _decimal_number(summary.operating_profit),
            summary.unit,
        ),
        ("financial_net_income", _decimal_number(summary.net_income), summary.unit),
    )
    if summary.statement_basis:
        values += (("financial_statement_basis", summary.statement_basis, None),)
    return tuple(
        _ObservationValue(
            field_name=field_name,
            value=value,
            unit=unit,
            quality_state="reported",
            knowledge_phase=_knowledge_phase(ipo, published_at),
            published_at=published_at,
            observed_at=_aware_utc(observed_at),
            parser_name="dart-public-financial",
            parser_version="dart-public-financial-v1",
        )
        for field_name, value, unit in values
    )


def _derived_valuation_observations(
    ipo: Ipo,
    published_at: Optional[datetime],
    observed_at: datetime,
    *,
    offer_price: Optional[int],
    post_listing_shares: Optional[int],
    free_float_shares: Optional[int],
) -> tuple[_ObservationValue, ...]:
    if offer_price is None or offer_price <= 0:
        return ()
    values = []
    if post_listing_shares is not None and post_listing_shares > 0:
        values.append(("market_cap_at_offer", offer_price * post_listing_shares))
    if free_float_shares is not None and free_float_shares > 0:
        values.append(("free_float_market_cap", offer_price * free_float_shares))
    return tuple(
        _ObservationValue(
            field_name=field_name,
            value=value,
            unit="KRW",
            quality_state="reported",
            knowledge_phase=_knowledge_phase(ipo, published_at),
            published_at=published_at,
            observed_at=_aware_utc(observed_at),
            parser_name="dart-public-derived",
            parser_version="dart-public-derived-v1",
        )
        for field_name, value in values
    )


def _append_observations(
    session: Session,
    ipo: Ipo,
    document: SourceDocument,
    observations: tuple[_ObservationValue, ...],
) -> int:
    for observation in observations:
        published_at = observation.published_at or document.published_at
        normalized_published_at = _aware_utc(published_at) if published_at is not None else None
        normalized_fetched_at = _aware_utc(document.fetched_at)
        append_fact_observation(
            session,
            ipo_id=ipo.id,
            source_document_id=document.id,
            field_name=observation.field_name,
            value=observation.value,
            unit=observation.unit,
            quality_state=observation.quality_state,
            knowledge_phase=observation.knowledge_phase,
            values={
                "published_at": normalized_published_at,
                "observed_at": observation.observed_at,
                "fetched_at": normalized_fetched_at,
                "effective_from": normalized_published_at,
                "parser_name": observation.parser_name,
                "parser_version": observation.parser_version,
                "raw_text_hash": document.raw_text_hash,
            },
        )
    return len(observations)


def _latest_structure_metric(session: Session, ipo_id: int) -> Optional[IpoStructureMetric]:
    return (
        session.execute(
            select(IpoStructureMetric)
            .where(IpoStructureMetric.ipo_id == ipo_id)
            .order_by(IpoStructureMetric.as_of_date.desc(), IpoStructureMetric.id.desc())
        )
        .scalars()
        .first()
    )


def _latest_observation_integer(
    session: Session,
    ipo_id: int,
    field_name: str,
) -> Optional[int]:
    observation = (
        session.execute(
            select(IpoFactObservation)
            .where(
                IpoFactObservation.ipo_id == ipo_id,
                IpoFactObservation.field_name == field_name,
                IpoFactObservation.quality_state.in_(("reported", "verified", "cross_checked")),
            )
            .order_by(
                IpoFactObservation.effective_from.desc(),
                IpoFactObservation.observed_at.desc(),
                IpoFactObservation.id.desc(),
            )
        )
        .scalars()
        .first()
    )
    if observation is None or observation.value_json is None:
        return None
    try:
        return int(observation.value_json)
    except (TypeError, ValueError):
        return None


def _decimal_number(value: Any) -> Any:
    return int(value) if value == value.to_integral_value() else float(value)


def _fallback_observation(
    evidence: RawEvidence,
    ipo: Ipo,
    observed_at: datetime,
) -> _ObservationValue:
    return _ObservationValue(
        field_name=evidence.field_name,
        value=evidence.value,
        unit=evidence.unit,
        quality_state="reported",
        knowledge_phase=_knowledge_phase(ipo, evidence.published_at),
        published_at=evidence.published_at,
        observed_at=observed_at,
        parser_name=evidence.parser_name,
        parser_version=evidence.parser_version,
    )


def _resolve_evidence_builder() -> Optional[Callable[..., tuple[Any, ...]]]:
    try:
        module: ModuleType = importlib.import_module("app.services.evidence")
    except ModuleNotFoundError as exc:
        if exc.name == "app.services.evidence":
            return None
        raise
    return getattr(module, "build_evidence_observations", None)


def _update_demand_forecast(
    session: Session,
    ipo: Ipo,
    filing: PublicDartFiling,
    parsed: DartDocumentParseResult,
    document: SourceDocument,
) -> bool:
    demand = parsed.institutional_demand
    if demand is None:
        return False
    mapping = {
        "institutional_competition_ratio": demand.competition_ratio,
        "lockup_commitment_ratio": demand.lockup_commitment_ratio,
        "above_band_participation_ratio": demand.above_band_participation_ratio,
        "offer_price_final": demand.offer_price_final,
        "participants_count": demand.participants_count,
    }
    trusted = {
        field_name: value
        for field_name, value in mapping.items()
        if _has_unambiguous_evidence(parsed.raw_evidence, field_name, value)
    }
    if not trusted:
        return False
    final_price = trusted.pop("offer_price_final", None)
    if final_price is not None:
        ipo.offer_price_final = int(final_price)
    upsert_demand_forecast(
        session,
        ipo_id=ipo.id,
        receipt_no=filing.receipt_no,
        values={
            **trusted,
            "result_published_at": parsed.published_at,
            "source": STRUCTURE_SOURCE,
            "raw_text_hash": document.raw_text_hash,
            "evidence_json": [item.as_dict() for item in parsed.raw_evidence],
            "parser_version": parsed.parser_version,
        },
    )
    return True


def _update_structure_metric(
    session: Session,
    ipo: Ipo,
    filing: PublicDartFiling,
    parsed: DartDocumentParseResult,
) -> bool:
    structure = parsed.offering_structure
    if structure is None:
        return False
    direct = {
        field_name: value
        for field_name, value in {
            "total_offered_shares": structure.total_offered_shares,
            "new_shares": structure.new_shares,
            "secondary_shares": structure.secondary_shares,
            "free_float_shares": structure.free_float_shares,
            "free_float_ratio": structure.free_float_ratio,
        }.items()
        if _has_unambiguous_evidence(parsed.raw_evidence, field_name, value)
    }
    values: dict[str, Any] = {}
    total = direct.get("total_offered_shares")
    if total:
        values["total_offer_shares"] = int(total)
        if direct.get("new_shares") is not None:
            values["new_share_ratio"] = float(direct["new_shares"]) / float(total)
        if direct.get("secondary_shares") is not None:
            values["secondary_share_ratio"] = float(direct["secondary_shares"]) / float(total)
    if direct.get("free_float_shares") is not None:
        values["free_float_shares_on_listing"] = int(direct["free_float_shares"])
        offer_price = (
            parsed.institutional_demand.offer_price_final
            if parsed.institutional_demand is not None
            and parsed.institutional_demand.offer_price_final is not None
            else ipo.offer_price_final
        )
        if offer_price is not None and offer_price > 0:
            values["free_float_market_cap"] = int(direct["free_float_shares"]) * int(offer_price)
    if direct.get("free_float_ratio") is not None:
        values["free_float_ratio_on_listing"] = float(direct["free_float_ratio"])
    if not values:
        return False
    as_of_date = filing.submitted_on or date.min
    row = session.execute(
        select(IpoStructureMetric).where(
            IpoStructureMetric.ipo_id == ipo.id,
            IpoStructureMetric.as_of_date == as_of_date,
            IpoStructureMetric.source == STRUCTURE_SOURCE,
        )
    ).scalar_one_or_none()
    if row is None:
        row = IpoStructureMetric(
            ipo_id=ipo.id,
            as_of_date=as_of_date,
            source=STRUCTURE_SOURCE,
        )
        session.add(row)
    for field_name, value in values.items():
        setattr(row, field_name, value)
    return True


def _has_unambiguous_evidence(evidence: Iterable[RawEvidence], field_name: str, value: Any) -> bool:
    if value is None:
        return False
    matches = [item.value for item in evidence if item.field_name == field_name]
    encoded = {_json_value(item) for item in matches}
    return bool(matches) and encoded == {_json_value(value)}


def _select_effective_filings(
    filings: Iterable[PublicDartFiling],
) -> tuple[PublicDartFiling, ...]:
    selected: dict[str, PublicDartFiling] = {}
    for filing in filings:
        family = classify_dart_document_family(filing.report_name)
        if family not in _SECTION_KEYWORDS:
            continue
        selected[filing.receipt_no] = filing
    return tuple(sorted(selected.values(), key=_filing_sort_key))


def _current_effective_receipts(filings: Iterable[PublicDartFiling]) -> set[str]:
    ordered = tuple(sorted(filings, key=_filing_sort_key))
    if ordered and classify_dart_document_family(ordered[-1].report_name) is (
        DartDocumentFamily.WITHDRAWAL
    ):
        return set()
    demand_candidates = [
        filing
        for filing in ordered
        if classify_dart_document_family(filing.report_name)
        in {
            DartDocumentFamily.SECURITIES_REGISTRATION,
            DartDocumentFamily.INVESTMENT_PROSPECTUS,
        }
    ]
    result_candidates = [
        filing
        for filing in ordered
        if classify_dart_document_family(filing.report_name) is DartDocumentFamily.ISSUANCE_RESULT
    ]
    receipts = {demand_candidates[-1].receipt_no} if demand_candidates else set()
    if result_candidates:
        receipts.add(result_candidates[-1].receipt_no)
    return receipts


def _search_window(ipo: Ipo, observed_at: datetime) -> tuple[date, date]:
    dates = [
        value
        for value in (
            ipo.report_filed_date,
            ipo.demand_start_date,
            ipo.subscription_start_date,
            ipo.subscription_end_date,
            ipo.actual_listing_date,
            ipo.listing_date,
        )
        if value is not None
    ]
    end = observed_at.astimezone(SEOUL).date()
    anchor = min(dates) if dates else end
    return min(anchor - timedelta(days=540), end - timedelta(days=540)), end


def _parse_official_schedule(raw_html: str) -> dict[str, date]:
    if not raw_html:
        return {}
    soup = BeautifulSoup(raw_html, "lxml")
    result: dict[str, date] = {}
    definitions = (
        (
            ("수요예측일", "수요예측기간", "수요예측 실시일"),
            "demand_start_date",
            "demand_end_date",
        ),
        (
            ("청약기일", "청약기간", "일반청약자 청약일"),
            "subscription_start_date",
            "subscription_end_date",
        ),
        (("환불일", "환불기일"), "refund_date", None),
        (("납입기일", "납입일"), "payment_date", None),
        (("상장예정일", "상장일"), "planned_listing_date", None),
    )
    for row in soup.select("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in row.select("th, td")]
        if len(cells) < 2:
            continue
        label = cells[0].replace(" ", "")
        dates = _date_tokens(" ".join(cells[1:]))
        if not dates:
            continue
        for labels, start_field, end_field in definitions:
            if any(item.replace(" ", "") in label for item in labels):
                result[start_field] = dates[0]
                if end_field is not None:
                    result[end_field] = dates[-1]
                break
    return result


def _date_tokens(text: str) -> tuple[date, ...]:
    matches = re.findall(
        r"(20\d{2})\s*(?:[.년/-])\s*(\d{1,2})\s*(?:[.월/-])\s*(\d{1,2})\s*일?",
        text,
    )
    values = []
    for year, month, day in matches:
        try:
            values.append(date(int(year), int(month), int(day)))
        except ValueError:
            continue
    return tuple(dict.fromkeys(values))


def _required_evidence_gaps(parsed: DartDocumentParseResult) -> tuple[str, ...]:
    fields = {item.field_name for item in parsed.raw_evidence if item.value is not None}
    family = parsed.document_family
    if family in {
        DartDocumentFamily.SECURITIES_REGISTRATION,
        DartDocumentFamily.INVESTMENT_PROSPECTUS,
    }:
        required = {
            "institutional_competition_ratio",
            "participants_count",
            "lockup_commitment_ratio",
            "offer_price_final",
            "total_offered_shares",
            "free_float_shares",
            "free_float_ratio",
        }
        missing = required - fields
        if not any(name.startswith("institutional_price_distribution.") for name in fields):
            missing.add("institutional_price_distribution")
        return tuple(sorted(missing))
    if family is DartDocumentFamily.ISSUANCE_RESULT:
        missing = set()
        if "retail_competition_ratio" not in fields:
            missing.add("retail_competition_ratio")
        broker_values = [
            item.value
            for item in parsed.raw_evidence
            if item.field_name.startswith("retail_brokers.") and isinstance(item.value, dict)
        ]
        required_broker_fields = {
            "competition_ratio",
            "applicants_count",
            "subscribed_shares",
            "allocated_shares",
        }
        if not broker_values or any(
            any(value.get(field) is None for field in required_broker_fields)
            for value in broker_values
        ):
            missing.add("retail_broker_details")
        return tuple(sorted(missing))
    return ()


def _knowledge_phase(ipo: Ipo, published_at: Optional[datetime]) -> str:
    if published_at is None:
        return "post_subscription"
    published_date = published_at.astimezone(SEOUL).date()
    if ipo.subscription_start_date and published_date < ipo.subscription_start_date:
        return "pre_subscription"
    if (
        ipo.subscription_start_date
        and ipo.subscription_end_date
        and ipo.subscription_start_date <= published_date <= ipo.subscription_end_date
    ):
        return "during_subscription"
    if ipo.actual_listing_date and published_date >= ipo.actual_listing_date:
        return "post_listing"
    return "post_subscription"


def _published_datetime(
    value: Optional[date], observed_at: Optional[datetime] = None
) -> Optional[datetime]:
    if value is None:
        return None
    end_of_day = datetime.combine(value, time.max, tzinfo=SEOUL).astimezone(timezone.utc)
    return min(end_of_day, _aware_utc(observed_at)) if observed_at else end_of_day


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _filing_sort_key(filing: PublicDartFiling) -> tuple[date, str]:
    return filing.submitted_on or date.min, filing.receipt_no


def _aggregate_sections(sections: Iterable[tuple[PublicDartSection, str]]) -> str:
    return "\n".join(f"<h1>{section.title}</h1>\n{raw_html}" for section, raw_html in sections)


def _section_source_key(receipt_no: str, section: PublicDartSection) -> str:
    return f"{receipt_no}:section:{section.document_no}:{section.element_id}"


def _section_metadata(section: PublicDartSection, filing: PublicDartFiling) -> dict[str, Any]:
    return {
        "receipt_no": filing.receipt_no,
        "report_name": filing.report_name,
        "document_no": section.document_no,
        "element_id": section.element_id,
        "title": section.title,
        "offset": section.offset,
        "length": section.length,
        "dtd": section.dtd,
    }


def _issue(
    session: Session,
    result: dict[str, Any],
    ipo: Ipo,
    issue_type: str,
    severity: str,
    details: dict[str, Any],
) -> None:
    upsert_data_quality_issue(
        session,
        issue_type=issue_type,
        severity=severity,
        status="open",
        ipo_id=ipo.id,
        details=details,
    )
    result["data_quality_issues"] += 1


def _filing_issue(
    session: Session,
    counts: dict[str, Any],
    ipo: Ipo,
    document: SourceDocument,
    issue_type: str,
    severity: str,
    details: dict[str, Any],
) -> None:
    upsert_data_quality_issue(
        session,
        issue_type=issue_type,
        severity=severity,
        status="open",
        ipo_id=ipo.id,
        source_document_id=document.id,
        details=details,
    )
    counts["data_quality_issues"] += 1


def _resolve_required_field_issues(session: Session, ipo_id: int, source_document_id: int) -> None:
    issues = session.scalars(
        select(DataQualityIssue).where(
            DataQualityIssue.ipo_id == ipo_id,
            DataQualityIssue.source_document_id == source_document_id,
            DataQualityIssue.issue_type == "required_fields_missing",
            DataQualityIssue.status == "open",
        )
    ).all()
    resolved_at = datetime.now(timezone.utc)
    for issue in issues:
        issue.status = "resolved"
        issue.resolved_at = resolved_at
        issue.resolution_note = "required evidence was recovered by a later parser run"


def _empty_ipo_result(ipo: Ipo) -> dict[str, Any]:
    return {
        "status": "pending",
        "ipo_id": ipo.id,
        "offering_id": ipo.offering_id,
        "corp_name": ipo.corp_name,
        "filings_found": 0,
        "filings_selected": 0,
        "source_documents": 0,
        "source_snapshots": 0,
        "observations": 0,
        "schedule_revisions": 0,
        "demand_forecasts": 0,
        "structure_metrics": 0,
        "data_quality_issues": 0,
        "warnings": [],
        "blocking_warnings": [],
        "missing_required_fields": [],
        "identity_state": "unreconciled",
        "reconciliation_state": "unreconciled",
        "evidence_state": "incomplete",
        "lineage_receipts": [],
        "failed_receipts": [],
        "error": None,
    }


def _month_result(
    month_start: date, month_end: date, results: list[dict[str, Any]]
) -> dict[str, Any]:
    statuses = [item["status"] for item in results]
    count_keys = (
        "source_documents",
        "source_snapshots",
        "observations",
        "schedule_revisions",
        "demand_forecasts",
        "structure_metrics",
        "data_quality_issues",
    )
    return {
        "month_start": month_start.isoformat(),
        "month_end": month_end.isoformat(),
        "processed": len(results),
        "succeeded": statuses.count("success"),
        "partial": statuses.count("partial"),
        "failed": statuses.count("failed"),
        "skipped": statuses.count("skipped"),
        "reported_reconciliations": sum(
            item.get("reconciliation_state") == "reported" for item in results
        ),
        "reconciliation": {
            str(item.get("corp_name") or item.get("ipo_id")): item.get(
                "reconciliation_state", "unreconciled"
            )
            for item in results
        },
        **{key: sum(int(item.get(key, 0)) for item in results) for key in count_keys},
        "results": results,
    }


def _json_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _string_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _raw_hash(value: str) -> str:
    """Kept public for audits that need to verify exported aggregate text."""
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()
