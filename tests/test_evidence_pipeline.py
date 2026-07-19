from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.collectors.dart import (
    DartDocumentFamily,
    DartDocumentType,
    OfferingStructureResult,
    RawEvidence,
    _extract_demand_lockup,
    _extract_demand_summary,
    _extract_free_float_statement,
    _extract_labeled_value,
    _extract_offering_narrative,
    _extract_price_distribution,
    _extract_retail_aggregate,
    _extract_retail_brokers,
    _extract_unique_match,
    _json_safe_value,
    _numeric_tokens,
    _prepare_parser_text,
    _structure_consistency_warnings,
    build_filing_lineage,
    classify_dart_document,
    classify_dart_document_family,
    parse_dart_document,
)
from app.services.data_quality import assess_evidence_quality
from app.services.evidence import (
    DecisionStage,
    EvidenceObservation,
    KnowledgePhase,
    _cutoff_for_phase,
    _expanded_fields,
    _quality_state,
    _stage_for_phase,
    build_evidence_observations,
    filter_evidence_for_stage,
    guard_observation,
    with_observed_at,
)
from app.services.historical_evidence import (
    HistoricalIpoRecord,
    IpoCohort,
    SampleTier,
    _quantile,
    build_historical_cohort_report,
    classify_ipo_cohort,
    wilson_interval,
)

UTC = timezone.utc
PUBLISHED = datetime(2026, 7, 1, 9, tzinfo=UTC)
OBSERVED = datetime(2026, 7, 1, 10, tzinfo=UTC)

PROSPECTUS_TEXT = """
수요예측 참여 내역 (단위 건 주) 구분 합계
건수 1 2 30 수량 100 200 3,000 경쟁률 (주2) 1 2 100.0 주1) 설명
구분 참여건수 기준 신청수량 기준 참여건수(건) 비율 신청수량(주) 비율
가격미제시 3 10.0% 300 10.0%
10,000원 초과 6 20.0% 600 20.0%
10,000원 (상단) 21 70.0% 2,100 70.0%
9,000원 초과 10,000원 미만 - - - -
9,000원 미만 - - - -
합계 30 100.0% 3,000 100.0%
(다) 의무보유확약기간별 수요예측 참여내역
구분 외국 기관투자자 합계 거래실적 유 주1) 거래실적 무
건수 수량 신청가격 건수 수량 신청가격 건수 수량 신청가격
6개월 확약 - - - - - - 5 500 10,000
미확약 2 200 10,000 - - - 20 2,000 10,000
계 3 300 10,000 - - - 30 3,000 10,000
주1) 공식 합계 (라) 주당 확정 공모가액의 결정
1주당 확정 공모가액 10,000원
상장공모는 신주모집 900주(공모주식의 90.0%), 구주매출 100주(10.0%)의
일반공모 방식에 의합니다.
모집 또는 매출주식의 수 기명식 보통주 1,000주
상장예정주식수 5,000주
상기의 의무보유 수량을 제외한 주식수 2,000주(40.0%)는 상장 직후 시장에서
유통가능한 물량입니다.
"""

RESULT_TEXT = """
청약 및 배정현황 구 분 최초 배정 청약 현황 최종 배정 현황
수량 비율 건수 수량 금액 비율 건수 수량 금액 비율
기관투자자 750 75.0 30 750 7,500,000 1.0 30 750 7,500,000 75.0
일반투자자 250 25.0 1,000 50,000 500,000,000 99.0 900 250 2,500,000 25.0
계 1,000 100 1,030 50,750 507,500,000 100 930 1,000 10,000,000 100
"""


def _observation(**overrides) -> EvidenceObservation:
    values = {
        "field_name": "institutional_competition_ratio",
        "value": 100.0,
        "unit": "ratio_to_one",
        "quality_state": "reported",
        "knowledge_phase": KnowledgePhase.PRE_SUBSCRIPTION,
        "source": "opendart",
        "source_document_id": "opendart:1",
        "published_at": PUBLISHED,
        "observed_at": OBSERVED,
        "parser_name": "parser",
        "parser_version": "1",
        "excerpt": "official row",
        "confidence": 0.98,
        "canonical_evidence_category": "reported_fact",
    }
    values.update(overrides)
    return EvidenceObservation(**values)


def _history(
    index: int,
    *,
    target_cutoff: datetime,
    return_pct: float = 1.0,
    **overrides,
) -> HistoricalIpoRecord:
    cutoff = target_cutoff - timedelta(days=60 - index)
    values = {
        "ipo_id": f"ipo-{index}",
        "corp_name": f"회사{index}",
        "ipo_type": "일반",
        "market": "코스닥",
        "decision_stage": DecisionStage.PRE_OPEN,
        "decision_cutoff": cutoff,
        "feature_published_at": cutoff - timedelta(days=2),
        "feature_observed_at": cutoff - timedelta(days=1),
        "outcome_at": cutoff + timedelta(days=2),
        "return_pct": return_pct,
    }
    values.update(overrides)
    return HistoricalIpoRecord(**values)


def test_typed_dart_parser_reads_official_tables_and_provenance() -> None:
    parsed = parse_dart_document(
        PROSPECTUS_TEXT,
        "[기재정정] 투자설명서",
        receipt_no="20260701000001",
        published_at=PUBLISHED,
    )
    demand = parsed.institutional_demand
    structure = parsed.offering_structure
    assert parsed.document_type is DartDocumentType.CORRECTION
    assert parsed.document_family is DartDocumentFamily.INVESTMENT_PROSPECTUS
    assert demand is not None
    assert (demand.competition_ratio, demand.participants_count) == (100.0, 30)
    assert demand.lockup_commitment_ratio == pytest.approx(1 / 3)
    assert demand.offer_price_final == 10_000
    assert demand.above_band_participation_ratio == 0.2
    assert len(demand.price_distribution) == 5
    assert demand.price_distribution[0].requested_shares_ratio == 0.1
    assert demand.price_distribution[-1].participants_count == 0
    assert structure is not None
    assert (
        structure.total_offered_shares,
        structure.new_shares,
        structure.secondary_shares,
    ) == (1_000, 900, 100)
    assert structure.post_listing_shares == 5_000
    assert structure.free_float_shares == 2_000
    assert structure.free_float_ratio == 0.4
    assert parsed.warnings == ()
    assert parsed.raw_evidence
    assert parsed.as_dict()["published_at"] == PUBLISHED.isoformat()
    assert parsed.raw_evidence[0].as_dict()["document_type"] == "correction"


def test_retail_result_is_deterministically_derived_from_official_aggregate() -> None:
    parsed = parse_dart_document(
        RESULT_TEXT,
        "증권발행실적보고서",
        receipt_no="20260702000001",
        published_at=PUBLISHED,
    )
    retail = parsed.retail_subscription
    assert retail is not None
    assert retail.competition_ratio == 200
    assert retail.applicants_count == 1_000
    assert retail.subscribed_shares == 50_000
    assert retail.allocated_shares == 250
    derived = next(
        item for item in retail.evidence if item.field_name == "retail_competition_ratio"
    )
    assert derived.canonical_evidence_category == "derived_reported_fact"


def test_real_dart_pipe_tables_preserve_lockup_distribution_and_broker_results() -> None:
    lockup = _extract_demand_lockup(
        "구분 | 외국 기관투자자 | 합계 건수 | 수량 | 신청가격 "
        "6개월 확약 | 1 | 5 | 9 미확약 | 1 | 5 | 9 "
        "계 | 2 | 10 | 9 주1) 공식 (라)",
        2,
    )
    assert lockup is not None and lockup[0] == 0.5

    distribution, evidence = _extract_price_distribution(
        "구분 | 참여건수 기준 | 신청수량 기준\n"
        "참여건수(건) | 비율 | 신청수량(주) | 비율\n"
        "가격미제시 | 1 | 10.0% | 100 | 10.0%\n"
        "8,000원 초과 | 9 | 90.0% | 900 | 90.0%\n"
        "7,000원 초과 ~ 8,000원 미만 | - | - | - | -\n"
        "합계 | 10 | 100.0% | 1,000 | 100.0%",
        DartDocumentType.CORRECTION,
        receipt_no="PIPE",
        published_at=PUBLISHED,
    )
    assert len(distribution) == len(evidence) == 3
    assert distribution[1].requested_shares_ratio == 0.9
    assert distribution[2].label == "7,000원 초과 ~ 8,000원 미만"
    assert (
        distribution[2].participants_count,
        distribution[2].requested_shares,
        distribution[2].ratio,
        distribution[2].requested_shares_ratio,
    ) == (0, 0, 0.0, 0.0)

    result = parse_dart_document(
        """
        구 분 | 최초 배정 | 청약 현황 | 최종 배정 현황
        수량 | 비율 | 건수 | 수량 | 금액 | 비율 | 건수 | 수량 | 금액 | 비율
        일반투자자 | 100 | 25.0 | 30 | 2,000 | 1,000 | 99.0 | 30 | 100 | 1,000 | 25.0
        [케이비증권]
        합계 | 20 | 1,500 | 75 | -
        [아이비케이투자증권㈜]
        합계 | 10 | 500 | 25 | -
        """,
        "증권발행실적보고서",
    ).retail_subscription
    assert result is not None
    assert [
        (row.broker_name, row.applicants_count, row.allocated_shares) for row in result.brokers
    ] == [
        ("KB증권", 20, 75),
        ("IBK투자증권", 10, 25),
    ]

    single = parse_dart_document(
        RESULT_TEXT + "\n[미래에셋증권]\n세부 배정표",
        "증권발행실적보고서",
    ).retail_subscription
    assert single is not None
    assert [(row.broker_name, row.competition_ratio) for row in single.brokers] == [
        ("미래에셋증권", 200.0)
    ]


def test_document_classification_withdrawal_unknown_and_lineage() -> None:
    expectations = {
        "증권신고서(지분증권)": DartDocumentType.SECURITIES_REGISTRATION,
        "투자설명서": DartDocumentType.INVESTMENT_PROSPECTUS,
        "[기재정정] 투자설명서": DartDocumentType.CORRECTION,
        "[발행조건확정] 증권신고서": DartDocumentType.TERMS_FINALIZATION,
        "증권발행실적보고서": DartDocumentType.ISSUANCE_RESULT,
        "공모 철회신고서": DartDocumentType.WITHDRAWAL,
        "사업보고서": DartDocumentType.UNKNOWN,
    }
    for name, expected in expectations.items():
        assert classify_dart_document(name) is expected
    assert classify_dart_document_family("상장취소 안내") is DartDocumentFamily.WITHDRAWAL
    assert classify_dart_document_family("정정신고서") is DartDocumentFamily.SECURITIES_REGISTRATION
    assert classify_dart_document_family("기타") is DartDocumentFamily.UNKNOWN

    rows = build_filing_lineage(
        [
            {"report_nm": "증권신고서(지분증권)", "rcept_dt": "20260701", "rcept_no": "1"},
            {
                "report_nm": "[발행조건확정]\u200b 증권신고서(지분증권)",
                "rcept_dt": "20260702",
                "rcept_no": "2",
            },
        ]
    )
    assert rows[0]["filing_lineage"]["supersedes_receipt_no"] == "1"
    assert rows[0]["filing_lineage"]["is_correction"] is True

    withdrawal = parse_dart_document("철회", "철회신고서")
    assert withdrawal.is_withdrawal is True
    assert withdrawal.warnings == ()
    unknown = parse_dart_document("x", "기타보고서")
    assert unknown.warnings == ("unknown_document_type",)


def test_parser_strict_fallbacks_and_html_rows_do_not_promote_ambiguous_values() -> None:
    direct = parse_dart_document(
        """
        기관 수요예측 경쟁률 123.4:1 참여 기관 수 77개
        의무보유확약 비율 12.5% 확정 공모가 8,000원
        """,
        "투자설명서",
    )
    assert direct.institutional_demand is not None
    assert direct.institutional_demand.competition_ratio == 123.4
    assert direct.institutional_demand.participants_count == 77
    assert direct.institutional_demand.lockup_commitment_ratio == 0.125
    assert direct.institutional_demand.offer_price_final == 8_000

    false_positive = parse_dart_document(
        "일반청약자 90% 기관투자자 10% 의무보유 규정상 10% 이상 배정",
        "투자설명서",
    )
    assert false_positive.institutional_demand is None
    assert false_positive.offering_structure is None
    assert false_positive.warnings == ("no_supported_ipo_fields",)

    html = (
        "<html><table><tr><th>구분</th><th>값</th></tr>"
        "<tr><td>확정 공모가</td><td>7,000원</td></tr></table></html>"
    )
    assert "확정 공모가 | 7,000원" in _prepare_parser_text(html)
    assert _prepare_parser_text("plain") == "plain"
    parsed_html = parse_dart_document(html, "투자설명서")
    assert parsed_html.institutional_demand.offer_price_final == 7_000


def test_low_level_parser_guards_conflicts_and_derived_structure() -> None:
    assert _numeric_tokens("주1) - 1,000 2.5") == ["1,000", "2.5"]
    assert _extract_demand_summary("관련 없음") is None
    malformed = "수요예측 참여 내역 건수 - 수량 - 경쟁률 (주2) - 주1)"
    assert _extract_demand_summary(malformed) is None
    no_counts = "수요예측 참여 내역 건수 - 수량 10 경쟁률 (주2) 2 주1)"
    assert _extract_demand_summary(no_counts) is None
    invalid_summary = "수요예측 참여 내역 건수 10 수량 5 경쟁률 (주2) 0 주1)"
    assert _extract_demand_summary(invalid_summary) is None
    assert _extract_demand_lockup("구분 외국 기관투자자 합계 (라)", None) is None
    assert _extract_demand_lockup("구분 외국 기관투자자 합계 6개월 확약 (라)", None) is None
    assert (
        _extract_demand_lockup(
            "구분 외국 기관투자자 합계 건수 수량 신청가격 6개월 확약 계 1 2 3 (라)",
            None,
        )
        is None
    )
    assert (
        _extract_demand_lockup(
            "구분 외국 기관투자자 합계 건수 수량 신청가격 6개월 확약 미확약 1 계 1 (라)",
            None,
        )
        is None
    )
    assert (
        _extract_demand_lockup(
            "구분 외국 기관투자자 합계 건수 수량 신청가격 6개월 확약 "
            "미확약 1 5 9 계 2 10 9 주1) x (라)",
            3,
        )
        is None
    )
    assert _extract_offering_narrative("상장공모는 신주모집 0주 일반공모 방식") is None
    assert (
        _extract_offering_narrative("상장공모는 신주모집 10주(공모 일부)의 일반공모 방식")[1]
        is None
    )
    assert _extract_unique_match("값 1 값 2", r"값 (\d+)", int) is None
    assert _extract_unique_match("값 x", r"값 (\d+)", int) is None
    assert _extract_unique_match("값 1", r"값 (\d+)", lambda _value: None) is None

    derived = parse_dart_document(
        """
        상장공모는 신주모집 1,000주(공모주식의 100.0%)의 일반공모 방식에 의합니다.
        상장예정주식수 4,000주
        의무보유 수량을 제외한 주식수 1,000주는 상장 직후 유통가능한 물량입니다.
        """,
        "투자설명서",
    ).offering_structure
    assert derived is not None
    assert derived.total_offered_shares == 1_000
    assert derived.secondary_shares == 0
    assert derived.free_float_ratio == 0.25
    derived_fields = {
        item.field_name: item.canonical_evidence_category for item in derived.evidence
    }
    assert derived_fields["total_offered_shares"] == "derived_reported_fact"
    assert derived_fields["free_float_ratio"] == "derived_reported_fact"

    assert (
        _extract_free_float_statement(
            "의무보유 수량을 제외한 주식수 1,000주(25.0%)는 상장 직후 유통가능 "
            "의무보유 수량을 제외한 주식수 1,000주(25.00%)는 상장 직후 유통가능"
        )[1]
        == 0.25
    )
    assert (
        _extract_free_float_statement(
            "의무보유 수량을 제외한 주식수 1,000주(20%)는 상장 직후 유통가능 "
            "의무보유 수량을 제외한 주식수 2,000주(20%)는 상장 직후 유통가능"
        )
        is None
    )
    assert (
        _extract_free_float_statement(
            "의무보유 수량을 제외한 주식수 1,000주지만 아직 유통가능하지 않음"
        )
        is None
    )
    assert (
        _extract_free_float_statement("의무보유 수량을 제외한 주식수 0주는 상장 직후 유통가능")
        is None
    )

    partial = parse_dart_document(
        "상장공모는 신주모집 10주(공모 일부)의 일반공모 방식", "투자설명서"
    ).offering_structure
    assert partial is not None and partial.secondary_shares is None
    assert (
        _extract_free_float_statement(
            "의무보유 수량을 제외한 주식수 1,000주(20%)는 상장 직후 유통가능 "
            "의무보유 수량을 제외한 주식수 1,000주(40%)는 상장 직후 유통가능"
        )
        is None
    )

    mismatch = OfferingStructureResult(100, 80, 30, 1000, 500, 0.4, ())
    assert _structure_consistency_warnings(mismatch) == (
        "offering_share_total_mismatch",
        "free_float_ratio_mismatch",
    )
    assert _structure_consistency_warnings(None) == ()


def test_retail_fallback_broker_and_invalid_aggregate() -> None:
    direct = parse_dart_document(
        """
        일반청약 경쟁률 50:1 일반청약자수 100명
        일반청약 신청주식수 5,000주 일반청약 배정주식수 100주
        | 테스트증권 | 경쟁률 55:1 | 청약자 90명 | 신청주식수 4,500주 | 배정주식수 90주 |
        [테스트증권]
        합계 | 90 | 4,500 | - | 90 | -
        """,
        "증권발행실적보고서",
        receipt_no="1",
        published_at=PUBLISHED,
    )
    assert direct.retail_subscription is not None
    assert direct.retail_subscription.competition_ratio == 50
    assert direct.retail_subscription.brokers[0].broker_name == "테스트증권"
    assert (
        _extract_retail_aggregate(
            "구 분 최초 배정 청약 현황 최종 배정 현황 일반투자자 - - - - - - - - - -"
        )
        is None
    )
    assert (
        _extract_retail_aggregate(
            "구 분 최초 배정 청약 현황 최종 배정 현황 일반투자자 0 0 0 0 0 0 0 0 0 0"
        )
        is None
    )
    only_one = parse_dart_document("일반청약 경쟁률 2:1", "증권발행실적보고서")
    assert only_one.retail_subscription.competition_ratio == 2
    empty = parse_dart_document("관련 값 없음", "증권발행실적보고서")
    assert empty.retail_subscription is None
    assert empty.warnings == ("no_supported_ipo_fields",)
    broker_noise = parse_dart_document(
        "| 일반 | 값 |\n| 가짜증권 | 값 없음 |\n일반청약 경쟁률 2:1",
        "증권발행실적보고서",
    )
    assert broker_noise.retail_subscription.brokers == ()
    duplicate_free_empty = parse_dart_document(
        "[첫증권]\n값 없음\n[둘증권]\n값 없음",
        "증권발행실적보고서",
    )
    assert duplicate_free_empty.retail_subscription is None
    empty_brokers, empty_evidence = _extract_retail_brokers(
        "[첫증권]\n값 없음\n[둘증권]\n값 없음",
        DartDocumentType.ISSUANCE_RESULT,
        aggregate=None,
        receipt_no=None,
        published_at=None,
    )
    assert empty_brokers == () and empty_evidence == []
    invalid_broker_summary = parse_dart_document(
        "[테스트증권]\n합계 | 0 | 100 | x | 10 | x",
        "증권발행실적보고서",
    )
    assert invalid_broker_summary.retail_subscription is None

    assert _extract_labeled_value("라벨 1", ("라벨",), r"(\d+)", lambda _value: None) is None
    fallback = _extract_labeled_value("라벨\n123", ("라벨",), r"(\d+)", int)
    assert fallback[0] == 123 and fallback[2] == "text_fallback"
    assert (
        build_filing_lineage([{"report_nm": "[정정]", "rcept_no": "1"}])[0]["filing_lineage"][
            "base_report_name"
        ]
        == "unknown"
    )
    assert "empty" not in _prepare_parser_text("<html><table><tr></tr></table></html>")


def test_evidence_builder_stage_guard_and_broker_contract() -> None:
    parsed = parse_dart_document(
        RESULT_TEXT
        + "\n| 테스트증권 | 경쟁률 55:1 | 청약자 90명 | 신청주식수 4,500주 | 배정주식수 90주 |",
        "증권발행실적보고서",
        receipt_no="20260702000001",
        published_at=PUBLISHED,
    )
    observations = build_evidence_observations(
        parsed,
        observed_at=OBSERVED,
        canonical_ipo_id="ipo-1",
    )
    assert observations
    assert {item.knowledge_phase for item in observations} == {KnowledgePhase.POST_SUBSCRIPTION}
    assert any(
        item.field_name == "broker.테스트증권.actual_proportional_competition"
        for item in observations
    )
    derived = next(item for item in observations if item.field_name == "retail_competition_ratio")
    assert derived.quality_state == "reported"
    assert derived.as_persistence_values()["knowledge_phase"] == "post_subscription"
    assert filter_evidence_for_stage(observations, "PRE_OPEN", OBSERVED) == ()
    assert len(filter_evidence_for_stage(observations, DecisionStage.POST_CLOSE, OBSERVED)) == len(
        observations
    )
    replaced = with_observed_at(observations, OBSERVED + timedelta(hours=1))
    assert replaced[0].observed_at == OBSERVED + timedelta(hours=1)

    before = build_evidence_observations(
        parsed,
        {DecisionStage.POST_CLOSE: PUBLISHED - timedelta(seconds=1)},
        observed_at=OBSERVED,
    )
    assert before == ()
    wrong_stage_only = build_evidence_observations(
        parsed,
        {DecisionStage.PRE_OPEN: OBSERVED},
        observed_at=OBSERVED,
    )
    assert wrong_stage_only == ()
    after = build_evidence_observations(
        parsed,
        {DecisionStage.POST_CLOSE: OBSERVED + timedelta(seconds=1)},
        observed_at=OBSERVED,
    )
    assert after
    sample = build_evidence_observations(parsed, observed_at=OBSERVED, data_origin="synthetic")
    assert sample[0].is_sample is True
    assert guard_observation(sample[0], "POST_CLOSE", OBSERVED).reason == "sample_or_synthetic"


@pytest.mark.parametrize(
    ("observation", "stage", "cutoff", "reason"),
    [
        (_observation(value=None), "PRE_OPEN", OBSERVED, "missing_value"),
        (_observation(quality_state="conflict"), "PRE_OPEN", OBSERVED, "unsafe_quality_state"),
        (_observation(published_at=None), "PRE_OPEN", OBSERVED, "missing_published_at"),
        (_observation(observed_at=None), "PRE_OPEN", OBSERVED, "missing_observed_at"),
        (
            _observation(published_at=OBSERVED + timedelta(seconds=1)),
            "PRE_OPEN",
            OBSERVED,
            "published_after_cutoff",
        ),
        (
            _observation(observed_at=OBSERVED + timedelta(seconds=1)),
            "PRE_OPEN",
            OBSERVED,
            "observed_after_cutoff",
        ),
        (
            _observation(
                field_name="retail_competition_ratio",
                knowledge_phase=KnowledgePhase.PRE_SUBSCRIPTION,
            ),
            "PRE_OPEN",
            OBSERVED,
            "future_knowledge_phase",
        ),
    ],
)
def test_evidence_guard_rejection_reasons(observation, stage, cutoff, reason) -> None:
    assert guard_observation(observation, stage, cutoff).reason == reason


def test_evidence_guard_phase_inference_quality_and_stage_validation() -> None:
    assert guard_observation(_observation(), "PRE_OPEN", OBSERVED).allowed is True
    during = _observation(
        field_name="live_competition", knowledge_phase=KnowledgePhase.DURING_SUBSCRIPTION
    )
    assert guard_observation(during, DecisionStage.IN_SUBSCRIPTION, OBSERVED).allowed is True
    listing = _observation(
        field_name="d1_close_return", knowledge_phase=KnowledgePhase.POST_LISTING
    )
    assert guard_observation(listing, DecisionStage.POST_CLOSE, OBSERVED).allowed is True
    with pytest.raises(ValueError, match="unknown decision stage"):
        guard_observation(_observation(), "bad", OBSERVED)
    assert _quality_state("conflict") == "conflict"
    assert _quality_state("user_scenario") == "estimated"
    empty_broker = RawEvidence(
        "retail_brokers.",
        {"broker_name": ""},
        "mixed",
        "x",
        DartDocumentType.ISSUANCE_RESULT,
    )
    assert _expanded_fields(empty_broker) == ()
    assert _stage_for_phase(KnowledgePhase.PRE_SUBSCRIPTION) is DecisionStage.PRE_OPEN
    assert _stage_for_phase(KnowledgePhase.DURING_SUBSCRIPTION) is DecisionStage.IN_SUBSCRIPTION
    assert (
        _cutoff_for_phase({DecisionStage.POST_CLOSE: OBSERVED}, KnowledgePhase.PRE_SUBSCRIPTION)
        == OBSERVED
    )
    assert (
        _cutoff_for_phase({DecisionStage.PRE_OPEN: OBSERVED}, KnowledgePhase.POST_SUBSCRIPTION)
        is None
    )

    prospectus = parse_dart_document("확정 공모가 1,000원", "투자설명서", published_at=PUBLISHED)
    pre = build_evidence_observations(prospectus)
    assert pre[0].knowledge_phase is KnowledgePhase.PRE_SUBSCRIPTION


def test_data_quality_compact_report_detects_errors_warnings_and_conflicts() -> None:
    good = _observation()
    duplicate = replace(good)
    conflict = replace(good, value=200.0)
    bad = _observation(
        field_name="retail_competition_ratio",
        source="",
        source_document_id=None,
        published_at=None,
        observed_at=OBSERVED - timedelta(days=10),
        quality_state="estimated",
        data_origin="test",
        is_sample=True,
    )
    report = assess_evidence_quality(
        (good, duplicate, conflict, bad),
        required_fields=("institutional_competition_ratio", "missing_field"),
        cutoff=OBSERVED,
        max_age=timedelta(days=1),
    )
    codes = {item.code for item in report.findings}
    assert report.ready is False
    assert report.error_count > 0 and report.warning_count > 0
    assert {
        "required_field_missing",
        "sample_or_synthetic",
        "source_missing",
        "timestamp_missing",
        "stale",
        "non_reported_quality",
        "duplicate_observation",
        "conflicting_values",
    }.issubset(codes)
    assert assess_evidence_quality((good,)).ready is True
    future = (
        replace(good, field_name="future_pub", published_at=OBSERVED + timedelta(1)),
        replace(good, field_name="future_obs", observed_at=OBSERVED + timedelta(1)),
        replace(good, field_name="unsafe", quality_state="missing"),
    )
    future_codes = {item.code for item in assess_evidence_quality(future, cutoff=OBSERVED).findings}
    assert {"future_published", "future_observed", "unsafe_quality_state"}.issubset(future_codes)


def test_historical_cohorts_tiers_statistics_and_validation_gate() -> None:
    target_cutoff = datetime(2026, 7, 20, tzinfo=UTC)
    target = _history(
        999, target_cutoff=target_cutoff, ipo_id="target", decision_cutoff=target_cutoff
    )
    four = [
        _history(index, target_cutoff=target_cutoff, return_pct=index - 2) for index in range(4)
    ]
    insufficient = build_historical_cohort_report(target, four)
    assert insufficient.sample_tier is SampleTier.INSUFFICIENT
    assert insufficient.median_return_pct is None

    exploratory = build_historical_cohort_report(
        target,
        [
            _history(index, target_cutoff=target_cutoff, return_pct=float(index))
            for index in range(5)
        ],
    )
    assert exploratory.sample_tier is SampleTier.EXPLORATORY
    assert exploratory.median_return_pct == 2
    assert exploratory.q1_return_pct == 1
    assert exploratory.q3_return_pct == 3
    assert exploratory.positive_rate is None

    records = [
        _history(index, target_cutoff=target_cutoff, return_pct=1 if index < 20 else -1)
        for index in range(30)
    ]
    research = build_historical_cohort_report(target, records)
    assert research.sample_tier is SampleTier.DECISION_GRADE
    assert research.positive_rate == pytest.approx(2 / 3)
    assert research.loss_rate == pytest.approx(1 / 3)
    assert research.positive_rate_wilson_95[0] < research.positive_rate
    assert research.recommendation_eligible is False
    assert research.validation_status == "validation_required"
    ready = build_historical_cohort_report(target, records, validation_approved=True)
    assert ready.recommendation_eligible is True
    assert ready.validation_status == "ready"


def test_historical_leakage_sample_and_cohort_filters() -> None:
    target_cutoff = datetime(2026, 7, 20, tzinfo=UTC)
    target = _history(
        999, target_cutoff=target_cutoff, ipo_id="target", decision_cutoff=target_cutoff
    )
    base = _history(1, target_cutoff=target_cutoff)
    candidates = [
        replace(base, ipo_id="target"),
        replace(base, ipo_id="dup"),
        replace(base, ipo_id="dup", return_pct=2),
        replace(base, ipo_id="sample", is_sample=True),
        replace(base, ipo_id="incomplete", is_complete=False),
        replace(base, ipo_id="spac", corp_name="테스트스팩"),
        replace(base, ipo_id="market", market="KOSPI"),
        replace(base, ipo_id="stage", decision_stage="POST_CLOSE"),
        replace(base, ipo_id="return", return_pct=float("nan")),
        replace(base, ipo_id="missing-feature", feature_published_at=None),
        replace(base, ipo_id="missing-outcome", outcome_at=None),
        replace(base, ipo_id="future", decision_cutoff=target_cutoff),
        replace(base, ipo_id="late-pub", feature_published_at=base.decision_cutoff + timedelta(1)),
        replace(base, ipo_id="late-obs", feature_observed_at=base.decision_cutoff + timedelta(1)),
        replace(base, ipo_id="bad-order", outcome_at=base.decision_cutoff),
        replace(base, ipo_id="unknown-outcome", outcome_at=target_cutoff + timedelta(1)),
        replace(base, ipo_id="bad-stage", decision_stage="bad"),
    ]
    report = build_historical_cohort_report(target, candidates)
    assert report.sample_count == 0
    assert set(report.excluded_counts) == {
        "target_self",
        "duplicate_ipo_id",
        "sample_or_synthetic",
        "incomplete_record",
        "cohort_mismatch",
        "market_mismatch",
        "stage_mismatch",
        "invalid_return",
        "missing_feature_timestamp",
        "missing_outcome_timestamp",
        "future_candidate",
        "feature_published_after_candidate_cutoff",
        "feature_observed_after_candidate_cutoff",
        "invalid_temporal_order",
        "outcome_unknown_at_target_cutoff",
    }
    with pytest.raises(ValueError, match="sample or synthetic target"):
        build_historical_cohort_report(replace(target, data_origin="dummy"), ())


def test_cohort_classification_wilson_and_quantile_validation() -> None:
    assert classify_ipo_cohort("테스트기업", "일반") is IpoCohort.OPERATING_COMPANY
    assert classify_ipo_cohort("테스트스팩") is IpoCohort.SPAC
    assert classify_ipo_cohort("테스트", "REIT") is IpoCohort.REIT
    assert classify_ipo_cohort("부동산투자회사") is IpoCohort.REIT
    assert wilson_interval(0, 0) == (None, None)
    assert wilson_interval(1, 1)[1] == 1
    with pytest.raises(ValueError, match="total"):
        wilson_interval(0, -1)
    with pytest.raises(ValueError, match="successes"):
        wilson_interval(2, 1)
    with pytest.raises(ValueError, match="positive finite"):
        wilson_interval(0, 1, z=float("inf"))
    with pytest.raises(ValueError, match="ordered values"):
        _quantile([], 0.5)
    with pytest.raises(ValueError, match="probability"):
        _quantile([1], 2)
    assert _quantile([1], 0.5) == 1
    assert _json_safe_value((PUBLISHED, DartDocumentType.CORRECTION)) == [
        PUBLISHED.isoformat(),
        "correction",
    ]
