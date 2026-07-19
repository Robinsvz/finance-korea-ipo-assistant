from __future__ import annotations

import itertools
from contextlib import nullcontext
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models import (
    DataQualityIssue,
    DemandForecast,
    Ipo,
    IpoFactObservation,
    IpoStructureMetric,
    MonthlyUniverseSnapshot,
    SourceDocument,
    SourceSnapshot,
    UnderwriterAllocation,
)
from app.services import jobs
from app.services import monthly_quality as quality

MONTH_START = date(2026, 7, 1)
MONTH_END = date(2026, 7, 31)
AS_OF = datetime(2026, 7, 19, tzinfo=timezone.utc)
PUBLISHED = datetime(2026, 6, 20, tzinfo=timezone.utc)
OBSERVED = datetime(2026, 7, 18, tzinfo=timezone.utc)
_KEYS = itertools.count(1)


def _ipo(**overrides):
    values = {
        "identity_key": f"kind:identity:{next(_KEYS)}",
        "offering_id": f"kind:offering:{next(_KEYS)}",
        "corp_name": "테스트기업",
        "normalized_name": "테스트기업",
        "kind_biz_no": "KIND-1",
        "dart_corp_code": "DART-1",
        "data_origin": "kind",
        "is_sample": False,
        "demand_start_date": date(2026, 6, 18),
        "demand_end_date": date(2026, 6, 19),
        "subscription_start_date": date(2026, 6, 23),
        "subscription_end_date": date(2026, 6, 24),
        "payment_date": date(2026, 6, 26),
        "payment_date_source": "kind",
        "listing_date": date(2026, 7, 1),
        "offer_price_final": 10_000,
    }
    values.update(overrides)
    return Ipo(**values)


def _document(db_session, ipo, source, *, safe=True, corp_name=None):
    suffix = next(_KEYS)
    metadata = (
        {"raw_row": {"cells": [corp_name or ipo.corp_name]}}
        if source == "kind"
        else {"corp_name": corp_name or ipo.corp_name}
    )
    document = SourceDocument(
        ipo_id=ipo.id,
        source=source,
        document_key=f"{source}:{suffix}",
        document_type="official",
        lineage_root_key=f"{source}:{ipo.id}",
        official_url="https://example.test/official" if safe else None,
        published_at=PUBLISHED,
        fetched_at=OBSERVED,
        raw_text_hash=f"hash-{suffix}",
        parser_name="fixture",
        parser_version="v1",
        metadata_json=metadata,
    )
    db_session.add(document)
    db_session.flush()
    return document


def _observation(
    db_session,
    ipo,
    document,
    field_name,
    value,
    *,
    safe=True,
    quality_state="reported",
    effective_from=PUBLISHED,
):
    suffix = next(_KEYS)
    observation = IpoFactObservation(
        observation_key=f"observation-{suffix}",
        ipo_id=ipo.id,
        source_document_id=document.id,
        field_name=field_name,
        value_json=value,
        quality_state=quality_state,
        knowledge_phase="post_subscription",
        published_at=PUBLISHED,
        observed_at=OBSERVED,
        fetched_at=OBSERVED if safe else None,
        effective_from=effective_from,
        parser_name="fixture",
        parser_version="v1",
        raw_text_hash=f"hash-{suffix}",
    )
    db_session.add(observation)
    return observation


def _complete_evidence(db_session, ipo, *, broker="KB증권"):
    kind = _document(db_session, ipo, "kind")
    dart = _document(db_session, ipo, "dart_public")
    for field_name, value in {
        "demand_period": "2026-06-18~2026-06-19",
        "subscription_period": "2026-06-23~2026-06-24",
        "payment_date": "2026-06-26",
        "listing_date": "2026-07-01",
        "offer_price_final": 10_000,
        "underwriter_names": [broker],
        f"underwriter:{broker}:role": "주관",
    }.items():
        _observation(db_session, ipo, kind, field_name, value)

    dart_values = {
        "institutional_competition_ratio": 1234.5,
        "participants_count": 2000,
        "lockup_commitment_ratio": 0.2,
        "offer_price_final": 10_000,
        "institutional_price_distribution.0": {"label": "상단 이상"},
        "total_offered_shares": 1_000_000,
        "new_shares": 800_000,
        "secondary_shares": 200_000,
        "free_float_shares": 2_000_000,
        "free_float_ratio": 0.25,
        "retail_competition_ratio": 600.0,
        "retail_applicants_count": 100_000,
        f"broker.{broker}.actual_proportional_competition": 600.0,
        f"broker.{broker}.actual_applicants_count": 100_000,
        f"broker.{broker}.subscribed_shares": 300_000_000,
        f"broker.{broker}.allocated_shares": 500_000,
    }
    for field_name, value in dart_values.items():
        _observation(db_session, ipo, dart, field_name, value)

    db_session.add(
        UnderwriterAllocation(
            ipo_id=ipo.id,
            broker_name=broker,
            role="주관",
            source="kind",
        )
    )
    db_session.add(
        DemandForecast(
            ipo_id=ipo.id,
            receipt_no=f"receipt-{next(_KEYS)}",
            institutional_competition_ratio=1234.5,
            participants_count=2000,
            lockup_commitment_ratio=0.2,
            result_published_at=PUBLISHED,
            source="dart_public_official",
        )
    )
    db_session.add(
        IpoStructureMetric(
            ipo_id=ipo.id,
            as_of_date=PUBLISHED.date(),
            source="dart_public_official",
            total_offer_shares=1_000_000,
            new_share_ratio=0.8,
            secondary_share_ratio=0.2,
            free_float_shares_on_listing=2_000_000,
            free_float_ratio_on_listing=0.25,
        )
    )
    listing_date = ipo.actual_listing_date or ipo.listing_date
    public_html = f"""
    <table><tbody><tr onclick="fnDetailView('{ipo.kind_biz_no}')">
    <td title="{ipo.corp_name}"><img alt="코스닥"/>{ipo.corp_name}</td>
    <td>2026-06-01</td><td>2026-06-18 ~ 2026-06-19</td>
    <td>2026-06-23 ~ 2026-06-24</td><td>2026-06-26</td>
    <td>10,000</td><td>10,000</td><td>{listing_date.isoformat()}</td><td>{broker}</td>
    </tr></tbody></table>
    """
    listing_summary = (
        "회사명, 상장일, 상장유형,증권구분, 업종, 국적, 상장주선인/지정자문인, "
        "공모가(원), 공모금액(천원), 최초상장주식수(주)"
    )
    listing_titles = (
        "회사명,상장일,상장유형,증권구분,업종,국적,상장주선인/지정자문인,"
        "공모가 (원),공모금액 (천원),최초상장주식수 (주)"
    )
    listing_html = f"""
    <table class="list" summary="{listing_summary}">
    <tbody><tr onclick="fnDetailView('12345','20260101000001')">
    <td title="{ipo.corp_name}"><img alt="코스닥"/>{ipo.corp_name}</td>
    <td>{listing_date.isoformat()}</td><td>신규상장</td><td>주권</td><td>소프트웨어</td>
    <td>대한민국</td><td>{broker}</td><td>10,000</td>
    <td>10,000,000</td><td>10,000,000</td>
    </tr></tbody></table>
    <script>fn_InitTitle('{listing_titles}', 'true');</script>
    """
    db_session.add_all(
        [
            SourceSnapshot(
                source="kind",
                source_key=f"{MONTH_START.isoformat()}:{MONTH_END.isoformat()}",
                fetched_at=OBSERVED,
                raw_text_hash=f"kind-roster-{ipo.id}",
                raw_text=public_html,
            ),
            SourceSnapshot(
                source="kind_listing",
                source_key=f"{MONTH_START.isoformat()}:{MONTH_END.isoformat()}",
                fetched_at=OBSERVED,
                raw_text_hash=f"listing-roster-{ipo.id}",
                raw_text=listing_html,
            ),
        ]
    )
    db_session.flush()
    return kind, dart


def test_complete_month_is_cross_checked_and_resolves_stale_issues(db_session):
    ipo = _ipo()
    appendix = _ipo(
        corp_name="신고서만",
        normalized_name="신고서만",
        kind_biz_no="KIND-APPENDIX",
        dart_corp_code="DART-APPENDIX",
        demand_start_date=None,
        demand_end_date=None,
        subscription_start_date=None,
        subscription_end_date=None,
        payment_date=None,
        listing_date=None,
        report_filed_date=date(2026, 7, 10),
    )
    db_session.add_all([ipo, appendix])
    db_session.flush()
    _kind, dart = _complete_evidence(db_session, ipo)
    superseded = _document(db_session, ipo, "dart_public")
    superseded.lineage_root_key = dart.lineage_root_key
    superseded.published_at = datetime(2026, 6, 1, tzinfo=timezone.utc)
    old_gap = DataQualityIssue(
        issue_key="old-monthly-gap",
        ipo_id=ipo.id,
        issue_type="monthly_evidence_gap",
        field_name="dart.offer_price_final",
        severity="error",
        status="open",
        details_json={"month_start": MONTH_START.isoformat()},
    )
    recovered = DataQualityIssue(
        issue_key="old-required-gap",
        ipo_id=ipo.id,
        issue_type="required_fields_missing",
        severity="error",
        status="open",
        details_json={"fields": ["institutional_price_distribution", "retail_broker_details"]},
    )
    retained = DataQualityIssue(
        issue_key="still-required-gap",
        ipo_id=ipo.id,
        issue_type="required_fields_missing",
        severity="error",
        status="open",
        details_json={"fields": ["unknown_future_field"]},
    )
    malformed = DataQualityIssue(
        issue_key="malformed-required-gap",
        ipo_id=ipo.id,
        issue_type="required_fields_missing",
        severity="error",
        status="open",
        details_json={"fields": "not-a-list"},
    )
    other_month = DataQualityIssue(
        issue_key="other-month-gap",
        ipo_id=ipo.id,
        issue_type="monthly_evidence_gap",
        field_name="other",
        severity="error",
        status="open",
        details_json={"month_start": "2026-06-01"},
    )
    recovered_warning = DataQualityIssue(
        issue_key="recovered-parser-warning",
        ipo_id=ipo.id,
        source_document_id=dart.id,
        issue_type="parse_warning",
        severity="warning",
        status="open",
        details_json={"warning": "no_supported_ipo_fields"},
    )
    recovered_empty_revision = DataQualityIssue(
        issue_key="recovered-empty-revision",
        ipo_id=ipo.id,
        source_document_id=superseded.id,
        issue_type="missing_evidence",
        severity="error",
        status="open",
    )
    recovered_superseded_warning = DataQualityIssue(
        issue_key="recovered-superseded-warning",
        ipo_id=ipo.id,
        source_document_id=superseded.id,
        issue_type="parse_warning",
        severity="warning",
        status="open",
        details_json={"warning": "no_supported_ipo_fields"},
    )
    db_session.add_all(
        [
            old_gap,
            recovered,
            retained,
            malformed,
            other_month,
            recovered_warning,
            recovered_empty_revision,
            recovered_superseded_warning,
        ]
    )
    db_session.flush()

    result = quality.reconcile_monthly_evidence(db_session, MONTH_START, MONTH_END, as_of=AS_OF)

    assert result["ready"] is True
    assert result["reconciliation_state"] == "cross_checked"
    assert result["success"] == 1
    assert result["partial"] == 0
    assert result["monthly_snapshots_saved"] == 1
    assert result["filing_update_appendix_count"] == 1
    assert result["monthly_issues_resolved"] == 1
    assert result["required_field_issues_resolved"] == 1
    assert result["document_issues_resolved"] == 3
    assert old_gap.status == recovered.status == "resolved"
    assert old_gap.resolved_at and recovered.resolved_at
    assert old_gap.resolution_note and recovered.resolution_note
    assert retained.status == malformed.status == other_month.status == "open"
    assert (
        recovered_warning.status
        == recovered_empty_revision.status
        == recovered_superseded_warning.status
        == "resolved"
    )
    snapshot = db_session.scalar(select(MonthlyUniverseSnapshot))
    assert snapshot.reconciliation_state == "cross_checked"
    assert snapshot.reconciliation_sources_json == ["dart_public", "kind", "kind_listing"]
    assert result["source_roster_ready"] is True
    assert result["source_rosters"]["kind_listing_names"] == ["테스트기업"]

    again = quality.reconcile_monthly_evidence(db_session, MONTH_START, MONTH_END, as_of=AS_OF)
    assert again["monthly_snapshots_saved"] == 1
    assert db_session.query(MonthlyUniverseSnapshot).count() == 1


def test_document_issue_resolution_keeps_unproven_or_unlinked_issues_open(db_session):
    ipo = _ipo()
    db_session.add(ipo)
    db_session.flush()
    _kind, current = _complete_evidence(db_session, ipo)
    unrelated_warning = DataQualityIssue(
        issue_key="unrelated-warning",
        ipo_id=ipo.id,
        source_document_id=current.id,
        issue_type="parse_warning",
        severity="warning",
        status="open",
        details_json={"warning": "manual_review_required"},
    )
    latest_missing = DataQualityIssue(
        issue_key="latest-missing",
        ipo_id=ipo.id,
        source_document_id=current.id,
        issue_type="missing_evidence",
        severity="error",
        status="open",
    )
    unlinked = DataQualityIssue(
        issue_key="unlinked-warning",
        ipo_id=ipo.id,
        issue_type="parse_warning",
        severity="warning",
        status="open",
        details_json=None,
    )
    db_session.add_all([unrelated_warning, latest_missing, unlinked])
    db_session.flush()

    not_ready = quality._OfferingAssessment(
        ipo=ipo,
        stage="POST_CLOSE",
        missing=("gap",),
        recovered_dart_fields=frozenset(),
        broker_details_complete=False,
    )
    assert quality._resolve_recovered_document_issues(db_session, (not_ready,)) == 0
    ready = quality._OfferingAssessment(
        ipo=ipo,
        stage="POST_CLOSE",
        missing=(),
        recovered_dart_fields=frozenset(),
        broker_details_complete=True,
    )
    assert quality._resolve_recovered_document_issues(db_session, (ready,)) == 0
    assert unrelated_warning.status == latest_missing.status == unlinked.status == "open"
    assert quality._document_has_facts(db_session, current.id)
    assert not quality._document_is_superseded(db_session, current)


def test_empty_and_invalid_month_open_then_resolve_universe_issue(db_session):
    with pytest.raises(ValueError, match="month_start"):
        quality.reconcile_monthly_evidence(db_session, MONTH_END, MONTH_START)

    empty = quality.reconcile_monthly_evidence(
        db_session, MONTH_START, MONTH_END, as_of=datetime(2026, 7, 1)
    )
    assert empty["processed"] == 0
    assert empty["ready"] is False
    assert empty["reconciliation_state"] == "unreconciled"
    assert empty["data_quality_gaps"] == 1
    issue = db_session.scalar(
        select(DataQualityIssue).where(DataQualityIssue.issue_type == "monthly_universe_empty")
    )
    assert issue.status == "open"

    ipo = _ipo()
    db_session.add(ipo)
    db_session.flush()
    _complete_evidence(db_session, ipo)
    recovered = quality.reconcile_monthly_evidence(db_session, MONTH_START, MONTH_END, as_of=AS_OF)
    assert recovered["monthly_issues_resolved"] == 1
    assert issue.status == "resolved"


def test_incomplete_preopen_creates_idempotent_identity_and_evidence_issues(db_session):
    ipo = _ipo(
        offering_id="offering:temp:unsafe",
        kind_biz_no=None,
        dart_corp_code=None,
        demand_start_date=date(2026, 7, 25),
        demand_end_date=date(2026, 7, 26),
        subscription_start_date=date(2026, 7, 28),
        subscription_end_date=date(2026, 7, 29),
        payment_date=None,
        listing_date=date(2026, 7, 31),
    )
    db_session.add(ipo)
    db_session.flush()

    first = quality.reconcile_monthly_evidence(db_session, MONTH_START, MONTH_END, as_of=AS_OF)
    second = quality.reconcile_monthly_evidence(db_session, MONTH_START, MONTH_END, as_of=AS_OF)

    missing = set(first["offerings"][0]["missing_fields"])
    assert first["offerings"][0]["stage"] == "PRE_OPEN"
    assert {
        "identity.offering_id",
        "identity.kind_biz_no",
        "identity.dart_corp_code",
        "source.kind_document",
        "source.dart_document",
        "current.underwriter_roster",
    } <= missing
    assert not any(field.startswith("dart.institutional") for field in missing)
    assert first["reconciliation_state"] == "unreconciled"
    assert second["data_quality_gaps"] == first["data_quality_gaps"]
    assert db_session.query(DataQualityIssue).count() == len(missing) + 3


def test_post_demand_detects_unsafe_conflicting_and_current_projection_gaps(db_session):
    ipo = _ipo(
        subscription_start_date=date(2026, 7, 25),
        subscription_end_date=date(2026, 7, 26),
        payment_date=None,
    )
    db_session.add(ipo)
    db_session.flush()
    kind = _document(db_session, ipo, "kind", corp_name="다른기업")
    dart = _document(db_session, ipo, "dart_public", corp_name="다른기업")
    for field_name, value in {
        "demand_period": "period",
        "subscription_period": "period",
        "listing_date": "2026-07-01",
        "offer_price_final": 9_000,
        "underwriter_names": ["KB증권"],
    }.items():
        _observation(db_session, ipo, kind, field_name, value)
    _observation(
        db_session,
        ipo,
        dart,
        "institutional_competition_ratio",
        100,
        safe=False,
    )
    _observation(
        db_session,
        ipo,
        dart,
        "offer_price_final",
        10_000,
        quality_state="conflict",
    )
    db_session.add(
        UnderwriterAllocation(ipo_id=ipo.id, broker_name="KB증권", role=None, source="kind")
    )
    db_session.flush()

    result = quality.reconcile_monthly_evidence(db_session, MONTH_START, MONTH_END, as_of=AS_OF)

    missing = set(result["offerings"][0]["missing_fields"])
    assert result["offerings"][0]["stage"] == "POST_DEMAND"
    assert result["reconciliation_state"] == "reported"
    assert {
        "identity.kind_name_conflict",
        "identity.dart_name_conflict",
        "kind.underwriter.KB증권.role",
        "dart.institutional_competition_ratio",
        "dart.offer_price_final",
        "current.demand_forecast",
        "current.structure_metric",
    } <= missing


def test_post_close_detects_broker_roster_and_result_gaps(db_session):
    ipo = _ipo()
    db_session.add(ipo)
    db_session.flush()
    _complete_evidence(db_session, ipo)
    dart = db_session.scalar(
        select(SourceDocument).where(
            SourceDocument.ipo_id == ipo.id,
            SourceDocument.source == "dart_public",
        )
    )
    _observation(
        db_session,
        ipo,
        dart,
        "broker.다른증권.actual_applicants_count",
        3,
        effective_from=datetime(2026, 6, 21, tzinfo=timezone.utc),
    )
    latest = db_session.scalar(
        select(IpoFactObservation).where(
            IpoFactObservation.ipo_id == ipo.id,
            IpoFactObservation.field_name == "broker.KB증권.allocated_shares",
        )
    )
    latest.quality_state = "missing"
    db_session.flush()

    result = quality.reconcile_monthly_evidence(db_session, MONTH_START, MONTH_END, as_of=AS_OF)
    missing = set(result["offerings"][0]["missing_fields"])
    assert "cross_source.underwriter_roster_conflict" in missing
    assert "dart.broker.KB증권.allocated_shares" in missing


def test_helper_edges_and_job_wrapper(db_session, monkeypatch):
    first = _ipo(offering_id="same")
    second = _ipo(offering_id="same")
    blank = _ipo(offering_id="")
    assert quality._duplicate_offering_ids((first, second, blank)) == {"same"}
    assert quality._stable_offering_id("stable") is True
    assert quality._stable_offering_id(None) is False
    assert quality._values_equal(1.0, 1) is True
    assert quality._values_equal("1", 1) is False
    assert quality._as_utc(datetime(2026, 7, 1)).tzinfo == timezone.utc
    assert quality._as_utc(None).year == 1

    calls = []
    monkeypatch.setattr(jobs, "init_db", lambda: calls.append("init"))
    monkeypatch.setattr(jobs, "session_scope", lambda: nullcontext(db_session))
    monkeypatch.setattr(
        quality,
        "reconcile_monthly_evidence",
        lambda session, start, end: {
            "session": session,
            "start": start,
            "end": end,
        },
    )
    result = jobs.reconcile_current_month_evidence(date(2024, 2, 15))
    assert calls == ["init"]
    assert result == {
        "session": db_session,
        "start": date(2024, 2, 1),
        "end": date(2024, 2, 29),
    }


def test_source_identity_and_fact_helpers_ignore_unusable_values(db_session):
    ipo = _ipo(corp_name="같은기업", normalized_name="같은기업")
    db_session.add(ipo)
    db_session.flush()
    kind = _document(db_session, ipo, "kind")
    dart = _document(db_session, ipo, "dart_public")
    unrelated = _document(db_session, ipo, "other")
    empty_metadata = _document(db_session, ipo, "kind")
    empty_metadata.metadata_json = {}
    assert quality._source_identity_matches(ipo, (empty_metadata,), "kind") is True
    assert quality._source_identity_matches(ipo, (kind,), "kind") is True
    assert quality._source_identity_matches(ipo, (dart,), "dart_public") is True
    assert quality._source_identity_matches(ipo, (unrelated,), "other") is True

    old = _observation(db_session, ipo, dart, "field", 1)
    new = _observation(
        db_session,
        ipo,
        dart,
        "field",
        2,
        quality_state="legacy_unverified",
        effective_from=datetime(2026, 6, 21, tzinfo=timezone.utc),
    )
    missing_value = _observation(db_session, ipo, dart, "none", None)
    _observation(db_session, ipo, unrelated, "ignored", 1)
    db_session.flush()
    selected = quality._latest_facts((old, new, missing_value), {dart.id: dart})
    assert selected.get("dart_public", {}) == {}
    assert quality._latest_facts((old,), {}) == {}
    assert quality._safe_document(kind) is True
    kind.parser_version = None
    assert quality._safe_document(kind) is False
    assert quality._fact_sort_key(quality._Fact(old, dart))[2] == old.id
    assert quality._dart_result_brokers(
        {"broker.KB.allocated_shares": quality._Fact(old, dart), "other": quality._Fact(old, dart)}
    ) == {"KB증권"}


def test_recovered_required_issue_without_current_assessment_is_retained(db_session):
    unrelated = _ipo(
        corp_name="다른달기업",
        normalized_name="다른달기업",
        listing_date=date(2026, 8, 1),
    )
    db_session.add(unrelated)
    db_session.flush()
    issue = DataQualityIssue(
        issue_key="other-required-gap",
        ipo_id=unrelated.id,
        issue_type="required_fields_missing",
        severity="error",
        status="open",
        details_json={"fields": ["offer_price_final"]},
    )
    db_session.add(issue)
    db_session.flush()
    assert quality._resolve_stale_required_field_issues(db_session, ()) == 0
    assert issue.status == "open"


def test_current_projection_reports_individual_mismatches(db_session):
    ipo = _ipo()
    db_session.add(ipo)
    db_session.flush()
    _complete_evidence(db_session, ipo)
    demand = db_session.scalar(select(DemandForecast).where(DemandForecast.ipo_id == ipo.id))
    structure = db_session.scalar(
        select(IpoStructureMetric).where(IpoStructureMetric.ipo_id == ipo.id)
    )
    demand.participants_count = None
    structure.total_offer_shares = 2
    structure.free_float_shares_on_listing = None
    structure.free_float_ratio_on_listing = 0.5
    structure.new_share_ratio = None
    structure.secondary_share_ratio = 0.9
    db_session.flush()

    documents = {
        row.id: row
        for row in db_session.scalars(select(SourceDocument).where(SourceDocument.ipo_id == ipo.id))
    }
    observations = db_session.scalars(
        select(IpoFactObservation).where(IpoFactObservation.ipo_id == ipo.id)
    )
    dart = quality._latest_facts(observations, documents)["dart_public"]
    gaps = quality._current_projection_gaps(db_session, ipo, dart)
    assert "current.demand_forecast.participants_count" in gaps
    assert "current.structure_metric.total_offered_shares" in gaps
    assert "current.structure_metric.free_float_shares" in gaps
    assert "current.structure_metric.free_float_ratio" in gaps
    assert "current.structure_metric.new_shares" in gaps
    assert "current.structure_metric.secondary_shares" in gaps
    no_total = {key: value for key, value in dart.items() if key != "total_offered_shares"}
    assert "current.structure_metric.total_offered_shares" in quality._current_projection_gaps(
        db_session, ipo, no_total
    )


def test_direct_assessment_covers_repository_filtered_guard_paths(db_session):
    ipo = _ipo(
        is_sample=True,
        data_origin="sample",
        refund_date=date(2026, 6, 26),
        refund_date_source="kind",
    )
    db_session.add(ipo)
    db_session.flush()
    kind = _document(db_session, ipo, "kind")
    dart = _document(db_session, ipo, "dart_public")
    for field_name, value in {
        "demand_period": "period",
        "subscription_period": "period",
        "listing_date": "2026-07-01",
        "offer_price_final": 9_000,
        "underwriter_names": [],
    }.items():
        _observation(db_session, ipo, kind, field_name, value)
    _observation(db_session, ipo, dart, "offer_price_final", 10_000)
    db_session.flush()

    assessment = quality._assess_offering(
        db_session,
        ipo,
        AS_OF,
        duplicate_offering_ids={ipo.offering_id},
    )

    assert "identity.offering_id_duplicate" in assessment.missing
    assert "identity.production_origin" in assessment.missing
    assert "kind.refund_date" in assessment.missing
    assert "cross_source.offer_price_final_conflict" in assessment.missing
    assert "dart.retail_competition_ratio" in assessment.missing
    assert "dart.retail_applicants_count" in assessment.missing
    assert "current.underwriter_roster" in assessment.missing
    assert assessment.broker_details_complete is False


def test_source_roster_error_and_mismatch_boundaries(db_session, monkeypatch):
    ipo = _ipo()
    db_session.add(ipo)
    db_session.flush()
    _complete_evidence(db_session, ipo)
    universe = quality.list_monthly_universe(db_session, MONTH_START, MONTH_END)
    parse_snapshot_records = quality._parse_snapshot_records

    monkeypatch.setattr(quality, "_parse_snapshot_records", lambda *args, **kwargs: None)
    parse_failure = quality._assess_source_rosters(db_session, universe, AS_OF)
    assert {
        "universe.kind_public_offering_parse",
        "universe.kind_listing_parse",
    } <= set(parse_failure.missing)

    public_record = SimpleNamespace(
        corp_name="다른기업",
        demand_start_date=None,
        demand_end_date=None,
        subscription_start_date=MONTH_START,
        subscription_end_date=MONTH_START,
        refund_date=None,
        payment_date=None,
        listing_date=None,
    )
    listing_record = SimpleNamespace(corp_name="다른기업", listing_date=MONTH_START)
    monkeypatch.setattr(
        quality,
        "_parse_snapshot_records",
        lambda _snapshot, *, marker, parser: (
            (listing_record,) if "LISTING" in marker else (public_record,)
        ),
    )
    name_mismatch = quality._assess_source_rosters(db_session, universe, AS_OF)
    assert {
        "universe.kind_public_offering_name_set",
        "universe.kind_listing_name_set",
    } <= set(name_mismatch.missing)
    monkeypatch.setattr(quality, "_parse_snapshot_records", parse_snapshot_records)

    assert quality._source_key_covers(None, MONTH_START, MONTH_END) is False

    def reject_snapshot(*_args, **_kwargs):
        raise quality.ExternalSourceError("invalid fixture")

    snapshot = SourceSnapshot(raw_text="invalid")
    assert (
        quality._parse_snapshot_records(snapshot, marker="<!-- PAGE -->", parser=reject_snapshot)
        is None
    )
    assert quality._dates_overlap(None, None, MONTH_START, MONTH_END) is False
    assert quality._dart_roster_names(db_session, (), AS_OF) == ()
