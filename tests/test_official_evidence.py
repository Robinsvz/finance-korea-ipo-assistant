from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from types import ModuleType, SimpleNamespace

import pytest

from app.collectors.dart import (
    DartDocumentFamily,
    DartDocumentParseResult,
    DartDocumentType,
    InstitutionalDemandResult,
    OfferingStructureResult,
    PriceDistributionEntry,
    RawEvidence,
    RetailBrokerResult,
    RetailSubscriptionResult,
)
from app.collectors.dart_public import PublicDartFiling, PublicDartSection
from app.models import (
    DataQualityIssue,
    DemandForecast,
    Ipo,
    IpoFactObservation,
    IpoScheduleRevision,
    IpoStructureMetric,
    MonthlyUniverseSnapshot,
    SourceDocument,
    SourceSnapshot,
)
from app.repositories import append_fact_observation, save_source_document
from app.services import official_evidence as service
from app.services.public_financial import PublicFinancialSummary

OBSERVED_AT = datetime(2026, 7, 19, 3, tzinfo=timezone.utc)
MONTH_START = date(2026, 7, 1)
MONTH_END = date(2026, 7, 31)
PROSPECTUS_HTML = """
<html><body><table><tr><th>구분</th><th>값</th></tr>
<tr><td>기관투자자 경쟁률</td><td>1,222.5 : 1</td></tr>
<tr><td>총 공모주식수</td><td>2,000,000주</td></tr></table></body></html>
<table><tr><td>수요예측일</td><td>2026.07.06 ~ 2026.07.07</td></tr>
<tr><td>청약기일</td><td>2026.07.14 ~ 2026.07.15</td></tr>
<tr><td>환불일</td><td>2026.07.20</td></tr>
<tr><td>납입기일</td><td>2026.07.20</td></tr>
<tr><td>상장예정일</td><td>2026.07.24</td></tr></table>
"""
RESULT_HTML = """
<html><body><table><tr><th>일반청약 결과</th><th>값</th></tr>
<tr><td>일반청약 경쟁률</td><td>900.5 : 1</td></tr></table></body></html>
"""
FINANCIAL_HTML = """
<p>(단위: 백만원)</p><table>
<tr><th>구분</th><th>2025년</th><th>2026년 1분기</th></tr>
<tr><td>매출액</td><td>1,000</td><td>400</td></tr>
<tr><td>영업이익</td><td>100</td><td>30</td></tr>
<tr><td>당기순이익</td><td>80</td><td>25</td></tr>
</table>
"""


def _patch_scope(monkeypatch, db_session) -> None:
    @contextmanager
    def scope():
        yield db_session
        db_session.flush()

    monkeypatch.setattr(service, "session_scope", scope)
    monkeypatch.setattr(service, "init_db", lambda: None)


def _ipo(**values) -> Ipo:
    defaults = {
        "corp_name": "테스트IPO",
        "normalized_name": "테스트ipo",
        "data_origin": "kind",
        "subscription_start_date": date(2026, 7, 14),
        "subscription_end_date": date(2026, 7, 15),
        "listing_date": date(2026, 7, 24),
    }
    return Ipo(**{**defaults, **values})


def _filing(
    receipt_no: str,
    report_name: str,
    submitted_on: date,
    *,
    corp_name: str = "테스트IPO",
    corp_code: str | None = "00112233",
) -> PublicDartFiling:
    return PublicDartFiling(
        corp_name=corp_name,
        corp_code=corp_code,
        receipt_no=receipt_no,
        report_name=report_name,
        presenter=corp_name,
        submitted_on=submitted_on,
    )


def _section(receipt_no: str, element_id: str = "1") -> PublicDartSection:
    return PublicDartSection(
        receipt_no=receipt_no,
        document_no=f"D{receipt_no[-2:]}",
        element_id=element_id,
        title="Ⅱ. 청약 및 배정에 관한 사항",
        offset=10,
        length=100,
        dtd="dart4.xsd",
    )


class FakeCollector:
    def __init__(
        self,
        filings=(),
        sections=None,
        *,
        fail_search: Exception | None = None,
        relevant_empty: bool = False,
    ) -> None:
        self.filings = tuple(filings)
        self.sections = sections or {}
        self.fail_search = fail_search
        self.relevant_empty = relevant_empty
        self.calls = []

    def search_filings(self, company_name, **kwargs):
        self.calls.append(("search", company_name, kwargs))
        if self.fail_search:
            raise self.fail_search
        return self.filings

    def fetch_matching_sections(self, receipt_no, *keywords):
        self.calls.append(("sections", receipt_no, keywords))
        if keywords and self.relevant_empty:
            return ()
        return tuple(self.sections.get(receipt_no, ()))


def _evidence(
    field_name: str,
    value,
    unit: str,
    document_type: DartDocumentType,
    receipt_no: str,
    published_at: datetime | None,
    *,
    method: str = "table",
) -> RawEvidence:
    return RawEvidence(
        field_name=field_name,
        value=value,
        unit=unit,
        excerpt=f"{field_name}={value}",
        document_type=document_type,
        receipt_no=receipt_no,
        published_at=published_at,
        method=method,
    )


def _parse_fixture(text, report_name, *, receipt_no=None, published_at=None):
    assert "<table>" in text
    if "발행실적" in report_name:
        raw = (
            _evidence(
                "retail_competition_ratio",
                900.5,
                "ratio_to_one",
                DartDocumentType.ISSUANCE_RESULT,
                receipt_no,
                published_at,
            ),
            _evidence(
                "retail_brokers.미래에셋증권",
                {
                    "broker_name": "미래에셋증권",
                    "competition_ratio": 901.0,
                    "applicants_count": 12345,
                    "subscribed_shares": 900000000,
                    "allocated_shares": 1000000,
                },
                "mixed",
                DartDocumentType.ISSUANCE_RESULT,
                receipt_no,
                published_at,
            ),
        )
        return DartDocumentParseResult(
            document_type=DartDocumentType.ISSUANCE_RESULT,
            document_family=DartDocumentFamily.ISSUANCE_RESULT,
            receipt_no=receipt_no,
            published_at=published_at,
            institutional_demand=None,
            offering_structure=None,
            retail_subscription=RetailSubscriptionResult(
                competition_ratio=900.5,
                applicants_count=None,
                subscribed_shares=None,
                allocated_shares=None,
                brokers=(
                    RetailBrokerResult(
                        broker_name="미래에셋증권",
                        competition_ratio=901.0,
                        applicants_count=12345,
                        subscribed_shares=900000000,
                        allocated_shares=1000000,
                    ),
                ),
                evidence=raw,
            ),
            raw_evidence=raw,
            warnings=(),
        )

    demand_values = {
        "institutional_competition_ratio": (1222.5, "ratio_to_one"),
        "lockup_commitment_ratio": (0.15, "ratio"),
        "above_band_participation_ratio": (0.88, "ratio"),
        "offer_price_final": (8000, "KRW"),
        "participants_count": (2100, "institutions"),
    }
    structure_values = {
        "total_offered_shares": (2000000, "shares"),
        "new_shares": (1500000, "shares"),
        "secondary_shares": (500000, "shares"),
        "free_float_shares": (3200000, "shares"),
        "free_float_ratio": (0.32, "ratio"),
    }
    raw = tuple(
        _evidence(
            key,
            value,
            unit,
            DartDocumentType.INVESTMENT_PROSPECTUS,
            receipt_no,
            published_at,
        )
        for key, (value, unit) in {**demand_values, **structure_values}.items()
    )
    distribution = PriceDistributionEntry(
        label="8,000원 초과",
        price_krw=8000,
        participants_count=1500,
        requested_shares=100000000,
        ratio=0.7,
        requested_shares_ratio=0.8,
    )
    distribution_evidence = _evidence(
        "institutional_price_distribution.0",
        {
            "label": distribution.label,
            "price_krw": distribution.price_krw,
            "participants_count": distribution.participants_count,
            "requested_shares": distribution.requested_shares,
            "ratio": distribution.ratio,
            "requested_shares_ratio": distribution.requested_shares_ratio,
        },
        "mixed",
        DartDocumentType.INVESTMENT_PROSPECTUS,
        receipt_no,
        published_at,
    )
    raw = (*raw[:5], distribution_evidence, *raw[5:])
    return DartDocumentParseResult(
        document_type=DartDocumentType.INVESTMENT_PROSPECTUS,
        document_family=DartDocumentFamily.INVESTMENT_PROSPECTUS,
        receipt_no=receipt_no,
        published_at=published_at,
        institutional_demand=InstitutionalDemandResult(
            competition_ratio=1222.5,
            participants_count=2100,
            lockup_commitment_ratio=0.15,
            offer_price_final=8000,
            above_band_participation_ratio=0.88,
            price_distribution=(distribution,),
            evidence=raw[:6],
        ),
        offering_structure=OfferingStructureResult(
            total_offered_shares=2000000,
            new_shares=1500000,
            secondary_shares=500000,
            post_listing_shares=None,
            free_float_shares=3200000,
            free_float_ratio=0.32,
            evidence=raw[6:],
        ),
        retail_subscription=None,
        raw_evidence=raw,
        warnings=(),
    )


def test_success_persists_lineage_observations_and_current_reads(db_session, monkeypatch):
    _patch_scope(monkeypatch, db_session)
    ipo = _ipo()
    db_session.add(ipo)
    db_session.flush()
    old_document = save_source_document(
        db_session,
        ipo_id=ipo.id,
        source=service.OFFICIAL_SOURCE,
        document_key="20260601000001",
        document_type="investment_prospectus",
        lineage_root_key="dart:00112233:investment_prospectus",
        values={"published_at": datetime(2026, 6, 1, tzinfo=timezone.utc)},
    )
    selected_receipt = "20260710000002"
    precreated = save_source_document(
        db_session,
        ipo_id=ipo.id,
        source=service.OFFICIAL_SOURCE,
        document_key=selected_receipt,
        document_type="investment_prospectus",
        lineage_root_key="dart:00112233:investment_prospectus",
    )
    db_session.flush()
    filings = (
        _filing("20260601000001", "투자설명서", date(2026, 6, 1)),
        _filing(selected_receipt, "(기재정정)투자설명서", date(2026, 7, 10)),
        _filing("20260716000003", "증권발행실적보고서", date(2026, 7, 16)),
        _filing("20260717000004", "사업보고서", date(2026, 7, 17)),
        _filing(
            "20260717000005",
            "투자설명서",
            date(2026, 7, 17),
            corp_name="다른회사",
        ),
    )
    collector = FakeCollector(
        filings,
        {
            "20260601000001": ((_section("20260601000001"), PROSPECTUS_HTML),),
            selected_receipt: ((_section(selected_receipt), PROSPECTUS_HTML),),
            "20260716000003": ((_section("20260716000003"), RESULT_HTML),),
        },
    )
    monkeypatch.setattr(service, "parse_dart_document", _parse_fixture)

    result = service.collect_public_dart_evidence_for_ipo(
        ipo.id, collector=collector, observed_at=OBSERVED_AT
    )

    assert result["status"] == "success"
    assert result["filings_found"] == 4
    assert result["filings_selected"] == 3
    assert result["source_documents"] == 3
    assert result["source_snapshots"] == 6
    assert result["observations"] == 29
    assert result["schedule_revisions"] == 3
    assert result["demand_forecasts"] == 1
    assert result["structure_metrics"] == 1
    assert result["data_quality_issues"] == 0
    assert result["identity_state"] == "reported"
    assert result["reconciliation_state"] == "reported"
    assert result["lineage_receipts"] == [
        "20260601000001",
        selected_receipt,
        "20260716000003",
    ]
    assert ipo.dart_corp_code == "00112233"
    assert ipo.dart_receipt_no == "20260716000003"
    assert ipo.offer_price_final == 8000
    assert precreated.supersedes_document_id == old_document.id
    assert precreated.source_snapshot_id is not None
    assert precreated.official_url.endswith(selected_receipt)
    assert precreated.raw_text_hash == service._raw_hash(
        f"<h1>{_section(selected_receipt).title}</h1>\n{PROSPECTUS_HTML}"
    )
    assert db_session.query(SourceSnapshot).count() == 6
    assert db_session.query(SourceDocument).count() == 3
    assert db_session.query(IpoScheduleRevision).count() == 3
    assert db_session.query(IpoFactObservation).count() == 29
    assert db_session.query(DataQualityIssue).count() == 0

    demand = db_session.query(DemandForecast).one()
    assert demand.institutional_competition_ratio == 1222.5
    assert demand.receipt_no == selected_receipt
    structure = db_session.query(IpoStructureMetric).one()
    assert structure.total_offer_shares == 2000000
    assert structure.new_share_ratio == pytest.approx(0.75)
    assert structure.secondary_share_ratio == pytest.approx(0.25)
    assert structure.free_float_ratio_on_listing == pytest.approx(0.32)
    broker_facts = {
        row.field_name: row.value_json
        for row in db_session.query(IpoFactObservation).all()
        if row.field_name.startswith("broker.")
    }
    assert broker_facts["broker.미래에셋증권.actual_applicants_count"] == 12345
    prospectus_revision = (
        db_session.query(IpoScheduleRevision)
        .filter(IpoScheduleRevision.source_document_id == precreated.id)
        .one()
    )
    assert prospectus_revision.demand_start_date == date(2026, 7, 6)
    assert prospectus_revision.demand_end_date == date(2026, 7, 7)
    assert prospectus_revision.subscription_start_date == date(2026, 7, 14)
    assert prospectus_revision.subscription_end_date == date(2026, 7, 15)
    assert prospectus_revision.refund_date == date(2026, 7, 20)
    assert prospectus_revision.payment_date == date(2026, 7, 20)
    assert prospectus_revision.planned_listing_date == date(2026, 7, 24)

    again = service.collect_public_dart_evidence_for_ipo(
        ipo.id, collector=collector, observed_at=OBSERVED_AT + timedelta(days=1)
    )
    assert again["status"] == "success"
    assert db_session.query(SourceSnapshot).count() == 6
    assert db_session.query(SourceDocument).count() == 3
    assert db_session.query(IpoScheduleRevision).count() == 3
    assert db_session.query(IpoFactObservation).count() == 29
    assert db_session.query(DemandForecast).count() == 1
    assert db_session.query(IpoStructureMetric).count() == 1


def test_section_fallback_raw_evidence_and_warning_are_separated(db_session, monkeypatch):
    _patch_scope(monkeypatch, db_session)
    ipo = _ipo()
    db_session.add(ipo)
    db_session.flush()
    receipt = "20260710000011"
    collector = FakeCollector(
        (_filing(receipt, "투자설명서", date(2026, 7, 10)),),
        {receipt: ((_section(receipt), PROSPECTUS_HTML),)},
        relevant_empty=True,
    )
    parsed = _parse_fixture(
        PROSPECTUS_HTML,
        "투자설명서",
        receipt_no=receipt,
        published_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
    )
    parsed = DartDocumentParseResult(**{**parsed.__dict__, "warnings": ("review_only",)})
    monkeypatch.setattr(service, "parse_dart_document", lambda *_a, **_k: parsed)
    monkeypatch.setattr(service, "_resolve_evidence_builder", lambda: None)

    result = service.collect_public_dart_evidence_for_ipo(
        ipo.id, collector=collector, observed_at=OBSERVED_AT
    )

    assert result["status"] == "partial"
    assert result["observations"] == 12
    assert result["data_quality_issues"] == 1
    assert db_session.query(DataQualityIssue).one().issue_type == "parse_warning"
    assert db_session.query(IpoFactObservation).count() == 12
    section_calls = [call for call in collector.calls if call[0] == "sections"]
    assert section_calls[0][2]
    assert section_calls[1][2] == ()


def test_missing_sections_saves_lineage_and_dq_issue(db_session, monkeypatch):
    _patch_scope(monkeypatch, db_session)
    ipo = _ipo()
    db_session.add(ipo)
    db_session.flush()
    receipt = "20260710000012"
    collector = FakeCollector(
        (_filing(receipt, "투자설명서", date(2026, 7, 10)),),
        sections={},
    )

    result = service.collect_public_dart_evidence_for_ipo(
        ipo.id, collector=collector, observed_at=OBSERVED_AT
    )

    assert result["status"] == "partial"
    assert result["source_documents"] == 1
    assert result["source_snapshots"] == 0
    assert result["data_quality_issues"] == 1
    assert db_session.query(SourceDocument).one().source_snapshot_id is None
    assert db_session.query(DataQualityIssue).one().issue_type == "missing_section"


def test_parse_error_isolated_from_document_snapshot(db_session, monkeypatch):
    _patch_scope(monkeypatch, db_session)
    ipo = _ipo()
    db_session.add(ipo)
    db_session.flush()
    receipt = "20260710000013"
    collector = FakeCollector(
        (_filing(receipt, "투자설명서", date(2026, 7, 10)),),
        {receipt: ((_section(receipt), PROSPECTUS_HTML),)},
    )
    monkeypatch.setattr(
        service, "parse_dart_document", lambda *_a, **_k: (_ for _ in ()).throw(ValueError("bad"))
    )

    result = service.collect_public_dart_evidence_for_ipo(
        ipo.id, collector=collector, observed_at=OBSERVED_AT
    )

    assert result["status"] == "partial"
    assert result["source_documents"] == 1
    assert result["source_snapshots"] == 2
    assert result["observations"] == 0
    assert db_session.query(DataQualityIssue).one().issue_type == "parse_error"


def test_supported_document_missing_fields_are_not_overstated(db_session, monkeypatch):
    _patch_scope(monkeypatch, db_session)
    ipo = _ipo()
    db_session.add(ipo)
    db_session.flush()
    receipt = "20260710000015"
    filing = _filing(receipt, "투자설명서", date(2026, 7, 10))
    collector = FakeCollector((filing,), {receipt: ((_section(receipt), PROSPECTUS_HTML),)})
    parsed = DartDocumentParseResult(
        document_type=DartDocumentType.INVESTMENT_PROSPECTUS,
        document_family=DartDocumentFamily.INVESTMENT_PROSPECTUS,
        receipt_no=receipt,
        published_at=OBSERVED_AT - timedelta(days=1),
        institutional_demand=None,
        offering_structure=None,
        retail_subscription=None,
        raw_evidence=(),
        warnings=(),
    )
    monkeypatch.setattr(service, "parse_dart_document", lambda *_a, **_k: parsed)
    counts = service._collect_filing(
        db_session,
        ipo,
        filing,
        collector,
        OBSERVED_AT,
        update_current=False,
    )

    assert counts["observations"] == 0
    assert counts["data_quality_issues"] == 1
    assert db_session.query(DataQualityIssue).one().issue_type == "missing_evidence"

    result = service.collect_public_dart_evidence_for_ipo(
        ipo.id, collector=collector, observed_at=OBSERVED_AT
    )
    assert result["status"] == "partial"
    assert result["evidence_state"] == "incomplete"
    assert "institutional_competition_ratio" in result["missing_required_fields"]
    assert {row.issue_type for row in db_session.query(DataQualityIssue).all()} == {
        "missing_evidence",
        "required_fields_missing",
    }


def test_recovered_required_fields_resolve_the_previous_review_issue(db_session):
    ipo = _ipo()
    db_session.add(ipo)
    db_session.flush()
    document = SourceDocument(
        ipo_id=ipo.id,
        source="dart_public",
        document_key="RECOVERED",
        document_type="correction",
        lineage_root_key="ROOT",
    )
    db_session.add(document)
    db_session.flush()
    issue = DataQualityIssue(
        issue_key="recover-me",
        ipo_id=ipo.id,
        source_document_id=document.id,
        issue_type="required_fields_missing",
        severity="error",
        status="open",
    )
    db_session.add(issue)
    db_session.flush()

    service._resolve_required_field_issues(db_session, ipo.id, document.id)
    service._resolve_required_field_issues(db_session, ipo.id, document.id)

    assert issue.status == "resolved"
    assert issue.resolved_at is not None
    assert "recovered" in issue.resolution_note


def test_withdrawal_has_schedule_revision_without_missing_evidence(db_session, monkeypatch):
    _patch_scope(monkeypatch, db_session)
    ipo = _ipo()
    db_session.add(ipo)
    db_session.flush()
    receipt = "20260710000014"
    filing = _filing(receipt, "철회신고서", date(2026, 7, 10))
    collector = FakeCollector((filing,), {receipt: ((_section(receipt), RESULT_HTML),)})
    parsed = DartDocumentParseResult(
        document_type=DartDocumentType.WITHDRAWAL,
        document_family=DartDocumentFamily.WITHDRAWAL,
        receipt_no=receipt,
        published_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
        institutional_demand=None,
        offering_structure=None,
        retail_subscription=None,
        raw_evidence=(),
        warnings=(),
    )
    monkeypatch.setattr(service, "parse_dart_document", lambda *_a, **_k: parsed)

    result = service.collect_public_dart_evidence_for_ipo(
        ipo.id, collector=collector, observed_at=OBSERVED_AT
    )

    assert result["status"] == "success"
    assert result["data_quality_issues"] == 0
    revision = db_session.query(IpoScheduleRevision).one()
    assert revision.event_status == "withdrawn"
    assert revision.withdrawal_reason == "철회신고서"


@pytest.mark.parametrize(
    ("filings", "stored_code", "issue_type", "error"),
    [
        ((), None, "missing_source", "exact-company IPO filing not found"),
        (
            (
                _filing("20260710000021", "투자설명서", date(2026, 7, 10)),
                _filing(
                    "20260710000022",
                    "증권발행실적보고서",
                    date(2026, 7, 11),
                    corp_code="99887766",
                ),
            ),
            None,
            "ambiguous_identity",
            "DART corp identity is ambiguous",
        ),
        (
            (_filing("20260710000023", "투자설명서", date(2026, 7, 10)),),
            "44556677",
            "identity_conflict",
            "stored DART corp code conflicts with filing",
        ),
        (
            (_filing("20260710000024", "사업보고서", date(2026, 7, 10)),),
            None,
            "missing_source",
            "supported IPO filing not found",
        ),
    ],
)
def test_identity_and_supported_filing_failures_are_dq_issues(
    db_session, monkeypatch, filings, stored_code, issue_type, error
):
    _patch_scope(monkeypatch, db_session)
    ipo = _ipo(dart_corp_code=stored_code)
    db_session.add(ipo)
    db_session.flush()

    result = service.collect_public_dart_evidence_for_ipo(
        ipo.id, collector=FakeCollector(filings), observed_at=OBSERVED_AT
    )

    assert result["status"] == "partial"
    assert result["error"] == error
    assert db_session.query(DataQualityIssue).one().issue_type == issue_type


def test_search_failure_is_returned_and_recorded(db_session, monkeypatch):
    _patch_scope(monkeypatch, db_session)
    ipo = _ipo()
    db_session.add(ipo)
    db_session.flush()
    result = service.collect_public_dart_evidence_for_ipo(
        ipo.id,
        collector=FakeCollector(fail_search=RuntimeError("network")),
        observed_at=OBSERVED_AT,
    )
    assert result["status"] == "failed"
    assert result["error"] == "RuntimeError: network"
    assert db_session.query(DataQualityIssue).one().issue_type == "source_fetch_error"


@pytest.mark.parametrize("origin", ["sample", "synthetic", "test"])
def test_non_production_rows_are_skipped(db_session, monkeypatch, origin):
    _patch_scope(monkeypatch, db_session)
    ipo = _ipo(data_origin=origin, is_sample=origin == "sample")
    db_session.add(ipo)
    db_session.flush()
    result = service.collect_public_dart_evidence_for_ipo(
        ipo.id, collector=FakeCollector(), observed_at=OBSERVED_AT
    )
    assert result["status"] == "skipped"
    assert result["error"] == "non-production IPO"


def test_filing_exception_does_not_abort_other_selected_families(db_session, monkeypatch):
    _patch_scope(monkeypatch, db_session)
    ipo = _ipo()
    db_session.add(ipo)
    db_session.flush()
    filings = (
        _filing("20260710000031", "투자설명서", date(2026, 7, 10)),
        _filing("20260716000032", "증권발행실적보고서", date(2026, 7, 16)),
    )

    class Collector(FakeCollector):
        def fetch_matching_sections(self, receipt_no, *keywords):
            if receipt_no.endswith("31"):
                raise RuntimeError("one filing failed")
            return ((_section(receipt_no), RESULT_HTML),)

    monkeypatch.setattr(service, "parse_dart_document", _parse_fixture)
    result = service.collect_public_dart_evidence_for_ipo(
        ipo.id, collector=Collector(filings), observed_at=OBSERVED_AT
    )
    assert result["status"] == "partial"
    assert result["source_documents"] == 1
    assert result["observations"] == 5
    assert result["data_quality_issues"] == 1


def test_month_collection_isolates_failures_and_aggregates_counts(db_session, monkeypatch):
    _patch_scope(monkeypatch, db_session)
    first = _ipo(corp_name="A", normalized_name="a")
    second = _ipo(corp_name="B", normalized_name="b")
    db_session.add_all([first, second])
    db_session.flush()

    def collect(identifier, collector=None, observed_at=None):
        assert collector is marker
        assert observed_at is None
        if identifier == second.id:
            raise RuntimeError("isolated")
        return {
            "status": "success",
            "ipo_id": identifier,
            "source_documents": 2,
            "source_snapshots": 3,
            "observations": 4,
            "schedule_revisions": 1,
            "demand_forecasts": 1,
            "structure_metrics": 1,
            "data_quality_issues": 0,
        }

    marker = object()
    monkeypatch.setattr(service, "collect_public_dart_evidence_for_ipo", collect)
    result = service.collect_public_dart_evidence_for_month(MONTH_START, MONTH_END, marker)
    assert result["processed"] == 2
    assert result["succeeded"] == 1
    assert result["failed"] == 1
    assert result["partial"] == 0
    assert result["skipped"] == 0
    assert result["source_documents"] == 2
    assert result["observations"] == 4
    assert result["results"][1]["error"] == "RuntimeError: isolated"
    assert result["reported_reconciliations"] == 0
    assert result["reconciliation"] == {
        str(first.id): "unreconciled",
        str(second.id): "unreconciled",
    }
    assert result["monthly_snapshot_state"] == "unreconciled"
    assert result["monthly_snapshots_saved"] == 2
    assert db_session.query(MonthlyUniverseSnapshot).count() == 2


def test_month_validation_and_default_collector(db_session, monkeypatch):
    _patch_scope(monkeypatch, db_session)
    with pytest.raises(ValueError, match="month_start"):
        service.collect_public_dart_evidence_for_month(MONTH_END, MONTH_START)

    marker = object()
    monkeypatch.setattr(service, "DartPublicCollector", lambda: marker)
    assert service.collect_public_dart_evidence_for_month(MONTH_START, MONTH_END) == {
        "month_start": "2026-07-01",
        "month_end": "2026-07-31",
        "processed": 0,
        "succeeded": 0,
        "partial": 0,
        "failed": 0,
        "skipped": 0,
        "reported_reconciliations": 0,
        "reconciliation": {},
        "monthly_snapshot_state": "unreconciled",
        "monthly_snapshots_saved": 0,
        "source_documents": 0,
        "source_snapshots": 0,
        "observations": 0,
        "schedule_revisions": 0,
        "demand_forecasts": 0,
        "structure_metrics": 0,
        "data_quality_issues": 0,
        "results": [],
    }


def test_stored_public_financial_backfill_is_offline_idempotent_and_derived(
    db_session, monkeypatch
):
    _patch_scope(monkeypatch, db_session)
    good = _ipo(corp_name="재무IPO", normalized_name="재무ipo", offer_price_final=8000)
    missing = _ipo(corp_name="원문없음", normalized_name="원문없음")
    empty = _ipo(corp_name="빈원문", normalized_name="빈원문")
    sample = _ipo(
        corp_name="샘플",
        normalized_name="샘플",
        data_origin="sample",
        is_sample=True,
    )
    db_session.add_all([good, missing, empty, sample])
    db_session.flush()
    snapshot = SourceSnapshot(
        source="dart_public",
        source_key="financial-good",
        ipo_id=good.id,
        raw_text_hash="financial-good",
        raw_text=FINANCIAL_HTML,
    )
    empty_snapshot = SourceSnapshot(
        source="dart_public",
        source_key="financial-empty",
        ipo_id=empty.id,
        raw_text_hash="financial-empty",
        raw_text="",
    )
    db_session.add_all([snapshot, empty_snapshot])
    db_session.flush()
    good_document = SourceDocument(
        ipo_id=good.id,
        source_snapshot_id=snapshot.id,
        source="dart_public",
        document_key="FIN-GOOD",
        document_type="investment_prospectus",
        lineage_root_key="FIN-GOOD",
        published_at=OBSERVED_AT - timedelta(days=10),
        fetched_at=OBSERVED_AT,
        raw_text_hash="financial-good",
    )
    empty_document = SourceDocument(
        ipo_id=empty.id,
        source_snapshot_id=empty_snapshot.id,
        source="dart_public",
        document_key="FIN-EMPTY",
        document_type="investment_prospectus",
        lineage_root_key="FIN-EMPTY",
        published_at=OBSERVED_AT - timedelta(days=10),
        fetched_at=OBSERVED_AT,
        raw_text_hash="financial-empty",
    )
    db_session.add_all([good_document, empty_document])
    db_session.add(
        IpoStructureMetric(
            ipo_id=good.id,
            as_of_date=MONTH_START,
            source="dart_public_official",
            free_float_shares_on_listing=20,
        )
    )
    db_session.flush()
    append_fact_observation(
        db_session,
        ipo_id=good.id,
        source_document_id=good_document.id,
        field_name="post_listing_shares",
        value=100,
        unit="shares",
        quality_state="reported",
        knowledge_phase="pre_subscription",
        values={"observed_at": OBSERVED_AT, "effective_from": OBSERVED_AT},
    )
    db_session.flush()
    universe = SimpleNamespace(
        core=tuple(
            SimpleNamespace(ipo_id=value)
            for value in (good.id, missing.id, empty.id, sample.id, 999999)
        )
    )
    monkeypatch.setattr(service, "list_monthly_universe", lambda *_args: universe)

    result = service.backfill_stored_public_financial_evidence_for_month(MONTH_START, MONTH_END)
    again = service.backfill_stored_public_financial_evidence_for_month(MONTH_START, MONTH_END)

    assert result == {
        "processed": 3,
        "financial_summaries": 1,
        "observations": 6,
        "missing": ["원문없음", "빈원문"],
    }
    assert again == result
    facts = {
        row.field_name: row.value_json
        for row in db_session.query(IpoFactObservation).filter_by(ipo_id=good.id).all()
    }
    assert facts["financial_period"] == "2026년 1분기"
    assert facts["financial_revenue"] == 400
    assert facts["financial_operating_profit"] == 30
    assert facts["financial_net_income"] == 25
    assert facts["market_cap_at_offer"] == 800000
    assert facts["free_float_market_cap"] == 160000
    assert "샘플" not in result["missing"]


def test_stored_public_financial_backfill_validation_and_helper_edges(db_session, monkeypatch):
    _patch_scope(monkeypatch, db_session)
    with pytest.raises(ValueError, match="month_start"):
        service.backfill_stored_public_financial_evidence_for_month(MONTH_END, MONTH_START)

    ipo = _ipo(offer_price_final=0)
    db_session.add(ipo)
    db_session.flush()
    assert service._latest_structure_metric(db_session, ipo.id) is None
    assert service._latest_observation_integer(db_session, ipo.id, "missing") is None
    bad = IpoFactObservation(
        observation_key="bad-integer",
        ipo_id=ipo.id,
        field_name="post_listing_shares",
        value_json="not-a-number",
        quality_state="reported",
        knowledge_phase="pre_subscription",
        observed_at=OBSERVED_AT,
    )
    db_session.add(bad)
    db_session.flush()
    assert service._latest_observation_integer(db_session, ipo.id, "post_listing_shares") is None
    assert (
        service._derived_valuation_observations(
            ipo,
            OBSERVED_AT,
            OBSERVED_AT,
            offer_price=None,
            post_listing_shares=100,
            free_float_shares=20,
        )
        == ()
    )
    assert (
        service._derived_valuation_observations(
            ipo,
            OBSERVED_AT,
            OBSERVED_AT,
            offer_price=10,
            post_listing_shares=0,
            free_float_shares=-1,
        )
        == ()
    )
    summary = PublicFinancialSummary(
        period="2026년",
        revenue=Decimal("1.5"),
        operating_profit=Decimal("1"),
        net_income=Decimal("-1"),
        unit="원",
        statement_basis="별도",
    )
    observations = service._financial_observations(summary, ipo, None, OBSERVED_AT)
    assert len(observations) == 5
    assert observations[1].value == 1.5
    assert observations[-1].value == "별도"


def test_stored_financial_backfill_skips_unknown_and_nonproduction_ids(db_session, monkeypatch):
    _patch_scope(monkeypatch, db_session)
    sample = _ipo(data_origin="sample", is_sample=True)
    db_session.add(sample)
    db_session.flush()
    monkeypatch.setattr(
        service,
        "list_monthly_universe",
        lambda *_args: SimpleNamespace(
            core=(SimpleNamespace(ipo_id=sample.id), SimpleNamespace(ipo_id=999999))
        ),
    )

    assert service.backfill_stored_public_financial_evidence_for_month(MONTH_START, MONTH_END) == {
        "processed": 0,
        "financial_summaries": 0,
        "observations": 0,
        "missing": [],
    }


def test_noncurrent_filing_failures_financial_parse_and_blocking_missing_evidence(
    db_session, monkeypatch
):
    _patch_scope(monkeypatch, db_session)
    missing_ipo = _ipo(corp_name="누락", normalized_name="누락")
    parse_ipo = _ipo(corp_name="파싱", normalized_name="파싱")
    warning_ipo = _ipo(corp_name="경고", normalized_name="경고")
    blocking_ipo = _ipo(corp_name="차단", normalized_name="차단")
    db_session.add_all([missing_ipo, parse_ipo, warning_ipo, blocking_ipo])
    db_session.flush()

    missing_filing = _filing("20260710000101", "투자설명서", date(2026, 7, 10))
    missing_counts = service._collect_filing(
        db_session,
        missing_ipo,
        missing_filing,
        FakeCollector((missing_filing,), {}),
        OBSERVED_AT,
        update_current=False,
    )
    assert missing_counts["blocking_warnings"] == []

    parse_filing = _filing("20260710000102", "투자설명서", date(2026, 7, 10))
    monkeypatch.setattr(
        service,
        "parse_dart_document",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad noncurrent")),
    )
    parse_counts = service._collect_filing(
        db_session,
        parse_ipo,
        parse_filing,
        FakeCollector(
            (parse_filing,),
            {parse_filing.receipt_no: ((_section(parse_filing.receipt_no), PROSPECTUS_HTML),)},
        ),
        OBSERVED_AT,
        update_current=False,
    )
    assert parse_counts["blocking_warnings"] == []

    warning_filing = _filing("20260710000103", "투자설명서", date(2026, 7, 10))
    warning_parsed = _parse_fixture(
        PROSPECTUS_HTML,
        warning_filing.report_name,
        receipt_no=warning_filing.receipt_no,
        published_at=OBSERVED_AT - timedelta(days=1),
    )
    warning_parsed = DartDocumentParseResult(
        **{**warning_parsed.__dict__, "warnings": ("noncurrent review",)}
    )
    monkeypatch.setattr(service, "parse_dart_document", lambda *_args, **_kwargs: warning_parsed)
    warning_counts = service._collect_filing(
        db_session,
        warning_ipo,
        warning_filing,
        FakeCollector(
            (warning_filing,),
            {
                warning_filing.receipt_no: (
                    (_section(warning_filing.receipt_no), PROSPECTUS_HTML + FINANCIAL_HTML),
                )
            },
        ),
        OBSERVED_AT,
        update_current=False,
    )
    assert warning_counts["blocking_warnings"] == []
    assert warning_counts["observations"] >= 16
    assert (
        db_session.query(IpoFactObservation)
        .filter_by(ipo_id=warning_ipo.id, field_name="financial_revenue")
        .one()
        .value_json
        == 400
    )

    empty = DartDocumentParseResult(
        document_type=DartDocumentType.INVESTMENT_PROSPECTUS,
        document_family=DartDocumentFamily.INVESTMENT_PROSPECTUS,
        receipt_no="20260710000104",
        published_at=OBSERVED_AT - timedelta(days=1),
        institutional_demand=None,
        offering_structure=None,
        retail_subscription=None,
        raw_evidence=(),
        warnings=(),
    )
    blocking_filing = _filing("20260710000104", "투자설명서", date(2026, 7, 10))
    monkeypatch.setattr(service, "parse_dart_document", lambda *_args, **_kwargs: empty)
    monkeypatch.setattr(service, "_required_evidence_gaps", lambda _parsed: ())
    blocking_counts = service._collect_filing(
        db_session,
        blocking_ipo,
        blocking_filing,
        FakeCollector(
            (blocking_filing,),
            {
                blocking_filing.receipt_no: (
                    (_section(blocking_filing.receipt_no), PROSPECTUS_HTML),
                )
            },
        ),
        OBSERVED_AT,
        update_current=True,
    )
    assert blocking_counts["blocking_warnings"] == ["20260710000104: missing_evidence"]


def test_month_marks_snapshot_reported_only_when_every_identity_is_reported(
    db_session, monkeypatch
):
    _patch_scope(monkeypatch, db_session)
    ipo = _ipo()
    db_session.add(ipo)
    db_session.flush()

    def collect(identifier, collector=None, observed_at=None):
        assert identifier == ipo.id
        return {
            "status": "success",
            "ipo_id": identifier,
            "corp_name": ipo.corp_name,
            "reconciliation_state": "reported",
        }

    monkeypatch.setattr(service, "collect_public_dart_evidence_for_ipo", collect)
    result = service.collect_public_dart_evidence_for_month(
        MONTH_START, MONTH_END, collector=object()
    )
    assert result["monthly_snapshot_state"] == "reported"
    assert result["monthly_snapshots_saved"] == 1
    assert result["reported_reconciliations"] == 1
    assert db_session.query(MonthlyUniverseSnapshot).one().reconciliation_state == "reported"


def test_default_collector_naive_time_and_missing_identifier(db_session, monkeypatch):
    _patch_scope(monkeypatch, db_session)
    ipo = _ipo()
    db_session.add(ipo)
    db_session.flush()
    fake = FakeCollector()
    monkeypatch.setattr(service, "DartPublicCollector", lambda: fake)
    result = service.collect_public_dart_evidence_for_ipo(
        ipo.id, observed_at=datetime(2026, 7, 19, 3)
    )
    assert result["status"] == "partial"
    with pytest.raises(ValueError, match="IPO not found"):
        service.collect_public_dart_evidence_for_ipo(999, collector=fake)


def test_optional_evidence_builder_resolution(monkeypatch):
    module = ModuleType("app.services.evidence")

    def builder(*_args, **_kwargs):
        return ()

    module.build_evidence_observations = builder
    monkeypatch.setattr(service.importlib, "import_module", lambda _name: module)
    assert service._resolve_evidence_builder() is builder

    monkeypatch.setattr(service.importlib, "import_module", lambda _name: ModuleType("empty"))
    assert service._resolve_evidence_builder() is None

    def missing(_name):
        exc = ModuleNotFoundError("missing")
        exc.name = "app.services.evidence"
        raise exc

    monkeypatch.setattr(service.importlib, "import_module", missing)
    assert service._resolve_evidence_builder() is None

    def dependency_missing(_name):
        exc = ModuleNotFoundError("dependency")
        exc.name = "dependency"
        raise exc

    monkeypatch.setattr(service.importlib, "import_module", dependency_missing)
    with pytest.raises(ModuleNotFoundError, match="dependency"):
        service._resolve_evidence_builder()


def test_helper_boundaries():
    ipo = _ipo(
        subscription_start_date=date(2026, 7, 14),
        subscription_end_date=date(2026, 7, 15),
        actual_listing_date=date(2026, 7, 24),
    )
    assert service._knowledge_phase(ipo, None) == "post_subscription"
    assert (
        service._knowledge_phase(ipo, datetime(2026, 7, 13, 3, tzinfo=timezone.utc))
        == "pre_subscription"
    )
    assert (
        service._knowledge_phase(ipo, datetime(2026, 7, 14, 3, tzinfo=timezone.utc))
        == "during_subscription"
    )
    assert (
        service._knowledge_phase(ipo, datetime(2026, 7, 24, 3, tzinfo=timezone.utc))
        == "post_listing"
    )
    assert (
        service._knowledge_phase(ipo, datetime(2026, 7, 20, 3, tzinfo=timezone.utc))
        == "post_subscription"
    )
    assert service._published_datetime(None) is None
    assert service._published_datetime(date(2026, 7, 10)).date() == date(2026, 7, 10)
    assert service._aware_utc(OBSERVED_AT) is OBSERVED_AT
    assert service._string_value(DartDocumentType.WITHDRAWAL) == "withdrawal"
    assert service._json_value({"b": 1, "a": 2}) == '{"a": 2, "b": 1}'
    assert service._raw_hash("abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )
    assert service._aggregate_sections(()) == ""
    assert service._date_tokens("2026.02.30") == ()
    assert (
        service._parse_official_schedule(
            "<table><tr></tr><tr><td>기타</td><td>2026.01.01</td></tr>"
            "<tr><td>환불일</td><td>날짜 미정</td></tr></table>"
        )
        == {}
    )

    no_dates = _ipo(
        report_filed_date=None,
        subscription_start_date=None,
        subscription_end_date=None,
        listing_date=None,
    )
    assert service._search_window(no_dates, OBSERVED_AT) == (
        date(2025, 1, 25),
        date(2026, 7, 19),
    )


def test_unambiguous_evidence_guards_and_empty_current_tables(db_session):
    evidence = _evidence(
        "field",
        1,
        "count",
        DartDocumentType.INVESTMENT_PROSPECTUS,
        "20260710000041",
        OBSERVED_AT,
    )
    conflict = _evidence(
        "field",
        2,
        "count",
        DartDocumentType.INVESTMENT_PROSPECTUS,
        "20260710000041",
        OBSERVED_AT,
    )
    assert service._has_unambiguous_evidence((evidence,), "field", 1)
    assert not service._has_unambiguous_evidence((evidence,), "field", None)
    assert not service._has_unambiguous_evidence((), "field", 1)
    assert not service._has_unambiguous_evidence((evidence, conflict), "field", 1)

    ipo = _ipo()
    db_session.add(ipo)
    db_session.flush()
    parsed = DartDocumentParseResult(
        document_type=DartDocumentType.UNKNOWN,
        document_family=DartDocumentFamily.UNKNOWN,
        receipt_no=None,
        published_at=None,
        institutional_demand=None,
        offering_structure=None,
        retail_subscription=None,
        raw_evidence=(),
        warnings=(),
    )
    filing = _filing("20260710000041", "사업보고서", date(2026, 7, 10))
    document = SimpleNamespace(raw_text_hash=None)
    assert not service._update_demand_forecast(db_session, ipo, filing, parsed, document)
    assert not service._update_structure_metric(db_session, ipo, filing, parsed)


def test_partial_typed_current_values_and_undated_document(db_session):
    ipo = _ipo(dart_corp_code="00112233")
    db_session.add(ipo)
    db_session.flush()
    receipt = "20260710000042"
    competition = _evidence(
        "institutional_competition_ratio",
        777.7,
        "ratio_to_one",
        DartDocumentType.INVESTMENT_PROSPECTUS,
        receipt,
        OBSERVED_AT,
    )
    demand = InstitutionalDemandResult(
        competition_ratio=777.7,
        participants_count=None,
        lockup_commitment_ratio=None,
        offer_price_final=None,
        above_band_participation_ratio=None,
        price_distribution=(),
        evidence=(competition,),
    )
    parsed_demand = DartDocumentParseResult(
        document_type=DartDocumentType.INVESTMENT_PROSPECTUS,
        document_family=DartDocumentFamily.INVESTMENT_PROSPECTUS,
        receipt_no=receipt,
        published_at=OBSERVED_AT,
        institutional_demand=demand,
        offering_structure=None,
        retail_subscription=None,
        raw_evidence=(competition,),
        warnings=(),
    )
    filing = _filing(receipt, "투자설명서", date(2026, 7, 10))
    assert service._update_demand_forecast(
        db_session, ipo, filing, parsed_demand, SimpleNamespace(raw_text_hash="h")
    )
    assert ipo.offer_price_final is None

    no_evidence_demand = DartDocumentParseResult(**{**parsed_demand.__dict__, "raw_evidence": ()})
    assert not service._update_demand_forecast(
        db_session,
        ipo,
        _filing("20260710000043", "투자설명서", date(2026, 7, 11)),
        no_evidence_demand,
        SimpleNamespace(raw_text_hash=None),
    )

    total_evidence = _evidence(
        "total_offered_shares",
        100,
        "shares",
        DartDocumentType.INVESTMENT_PROSPECTUS,
        receipt,
        OBSERVED_AT,
    )
    total_only = OfferingStructureResult(
        total_offered_shares=100,
        new_shares=None,
        secondary_shares=None,
        post_listing_shares=None,
        free_float_shares=None,
        free_float_ratio=None,
        evidence=(total_evidence,),
    )
    parsed_total = DartDocumentParseResult(
        **{
            **parsed_demand.__dict__,
            "institutional_demand": None,
            "offering_structure": total_only,
            "raw_evidence": (total_evidence,),
        }
    )
    assert service._update_structure_metric(db_session, ipo, filing, parsed_total)

    free_evidence = _evidence(
        "free_float_shares",
        20,
        "shares",
        DartDocumentType.INVESTMENT_PROSPECTUS,
        receipt,
        OBSERVED_AT,
    )
    free_only = OfferingStructureResult(
        total_offered_shares=None,
        new_shares=None,
        secondary_shares=None,
        post_listing_shares=None,
        free_float_shares=20,
        free_float_ratio=None,
        evidence=(free_evidence,),
    )
    parsed_free = DartDocumentParseResult(
        **{
            **parsed_total.__dict__,
            "offering_structure": free_only,
            "raw_evidence": (free_evidence,),
        }
    )
    undated_filing = _filing(receipt, "투자설명서", date(2026, 7, 10))
    object.__setattr__(undated_filing, "submitted_on", None)
    assert service._update_structure_metric(db_session, ipo, undated_filing, parsed_free)

    no_evidence_structure = DartDocumentParseResult(**{**parsed_free.__dict__, "raw_evidence": ()})
    assert not service._update_structure_metric(
        db_session,
        ipo,
        _filing("20260710000044", "투자설명서", date(2026, 7, 12)),
        no_evidence_structure,
    )

    document = service._save_document(
        db_session,
        ipo,
        undated_filing,
        DartDocumentFamily.INVESTMENT_PROSPECTUS,
        None,
        None,
        None,
        OBSERVED_AT,
        (),
    )
    assert document.published_at is None
    assert document.metadata_json["submitted_on"] is None


def test_revision_selection_and_current_receipt_boundaries():
    new = _filing("20260710000062", "투자설명서", date(2026, 7, 10))
    old = _filing("20260709000061", "투자설명서", date(2026, 7, 9))
    duplicate = _filing("20260710000062", "투자설명서", date(2026, 7, 10))
    result = _filing("20260716000063", "증권발행실적보고서", date(2026, 7, 16))
    withdrawal = _filing("20260717000064", "철회신고서", date(2026, 7, 17))
    assert service._select_effective_filings((new, old, duplicate)) == (old, duplicate)
    assert service._current_effective_receipts((old, new, result)) == {
        new.receipt_no,
        result.receipt_no,
    }
    assert service._current_effective_receipts((old, withdrawal)) == set()
    assert service._current_effective_receipts((result,)) == {result.receipt_no}
    assert service._current_effective_receipts(()) == set()


def test_required_evidence_gap_boundaries():
    result = _parse_fixture(
        RESULT_HTML,
        "증권발행실적보고서",
        receipt_no="20260716000071",
        published_at=OBSERVED_AT,
    )
    assert service._required_evidence_gaps(result) == ()

    empty_result = DartDocumentParseResult(**{**result.__dict__, "raw_evidence": ()})
    assert service._required_evidence_gaps(empty_result) == (
        "retail_broker_details",
        "retail_competition_ratio",
    )

    incomplete_broker = _evidence(
        "retail_brokers.미래에셋증권",
        {
            "broker_name": "미래에셋증권",
            "competition_ratio": 100.0,
            "applicants_count": 10,
            "subscribed_shares": 1000,
            "allocated_shares": None,
        },
        "mixed",
        DartDocumentType.ISSUANCE_RESULT,
        "20260716000071",
        OBSERVED_AT,
    )
    competition = _evidence(
        "retail_competition_ratio",
        100.0,
        "ratio_to_one",
        DartDocumentType.ISSUANCE_RESULT,
        "20260716000071",
        OBSERVED_AT,
    )
    partial_result = DartDocumentParseResult(
        **{**result.__dict__, "raw_evidence": (competition, incomplete_broker)}
    )
    assert service._required_evidence_gaps(partial_result) == ("retail_broker_details",)

    unknown = DartDocumentParseResult(
        **{
            **result.__dict__,
            "document_type": DartDocumentType.UNKNOWN,
            "document_family": DartDocumentFamily.UNKNOWN,
            "raw_evidence": (
                _evidence(
                    "ignored",
                    None,
                    "count",
                    DartDocumentType.UNKNOWN,
                    "20260716000071",
                    OBSERVED_AT,
                ),
            ),
        }
    )
    assert service._required_evidence_gaps(unknown) == ()


def test_observation_builder_handles_enums_defaults_and_naive_time(monkeypatch):
    class Quality(Enum):
        REPORTED = "reported"

    parsed = _parse_fixture(
        PROSPECTUS_HTML,
        "투자설명서",
        receipt_no="20260710000051",
        published_at=OBSERVED_AT - timedelta(days=1),
    )
    item = SimpleNamespace(
        field_name="custom",
        value=3,
        unit=None,
        quality_state=Quality.REPORTED,
        published_at=None,
        observed_at=datetime(2026, 7, 19, 3),
        parser_name=None,
    )
    monkeypatch.setattr(service, "_resolve_evidence_builder", lambda: lambda *_a, **_k: (item,))
    result = service._evidence_observations(parsed, _ipo(), OBSERVED_AT)
    assert result[0].quality_state == "reported"
    assert result[0].knowledge_phase == "post_subscription"
    assert result[0].observed_at.tzinfo == timezone.utc
    assert result[0].parser_version == parsed.parser_version
