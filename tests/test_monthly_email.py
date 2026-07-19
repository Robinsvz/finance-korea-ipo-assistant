from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from app.models import (
    DemandForecast,
    Ipo,
    IpoFactObservation,
    IpoStructureMetric,
    ListingPrice,
    OutcomeEvaluation,
    SourceDocument,
    SourceSnapshot,
    UnderwriterAllocation,
)
from app.repositories import list_monthly_universe
from app.services.monthly_email import (
    DEFAULT_MAX_HTML_BYTES,
    DISCLAIMER,
    EvidenceFact,
    MonthlyEmailReadModel,
    _aggregate_reconciliation,
    _as_iterable,
    _brief_fact_value,
    _count,
    _database_datetime,
    _date_range,
    _decision_stage,
    _demand_missing_state,
    _derived_distribution_offer_band,
    _distribution_percent,
    _event_badges,
    _fact_meta,
    _format_as_of,
    _ipo_key,
    _is_default_competition,
    _is_retail_result_key,
    _json_value,
    _large_money,
    _million_won,
    _money,
    _observation_is_visible,
    _observation_value,
    _outcome_facts,
    _outcome_missing_state,
    _overlaps,
    _percent,
    _price_band,
    _price_distribution_bands,
    _price_distribution_fact,
    _price_distribution_sort_key,
    _price_distribution_value,
    _ratio,
    _retail_missing_state,
    _return_from_price,
    _safe_source,
    _scaled_money,
    _shares,
    _signed_percent,
    _source_state,
    _stable_hash,
    _top_distribution_bands,
    _top_distribution_summary,
    build_monthly_email_read_model,
    build_monthly_ipo_email,
    build_single_ipo_email,
    load_official_fact_overrides,
    render_monthly_ipo_email,
    render_single_ipo_email,
)


def _ipo(name: str, **overrides) -> Ipo:
    values = {
        "corp_name": name,
        "normalized_name": name,
        "data_origin": "kind",
        "is_sample": False,
        "status": "scheduled",
    }
    values.update(overrides)
    return Ipo(**values)


def test_monthly_email_renders_full_roster_real_evidence_and_appendix(db_session):
    rich = _ipo(
        "안전&성장<IPO>",
        identity_key="kind:rich",
        ticker="123456",
        market="KOSDAQ",
        ipo_type="NORMAL",
        status="listed",
        demand_start_date=date(2026, 6, 17),
        demand_end_date=date(2026, 6, 23),
        subscription_start_date=date(2026, 7, 1),
        subscription_end_date=date(2026, 7, 2),
        refund_date=date(2026, 7, 6),
        payment_date=date(2026, 7, 6),
        listing_date=date(2026, 7, 13),
        actual_listing_date=date(2026, 7, 13),
        offer_price_min=18_000,
        offer_price_max=20_000,
        offer_price_final=20_000,
        offer_amount_million=10_500,
    )
    pre = _ipo(
        "청약전",
        subscription_start_date=date(2026, 7, 25),
        subscription_end_date=date(2026, 7, 26),
        demand_start_date=date(2026, 7, 19),
        demand_end_date=date(2026, 7, 22),
        listing_date=date(2026, 8, 3),
    )
    active = _ipo(
        "청약중",
        subscription_start_date=date(2026, 7, 18),
        subscription_end_date=date(2026, 7, 20),
        listing_date=date(2026, 7, 28),
    )
    appendix = _ipo("정정공시", report_filed_date=date(2026, 7, 18))
    sample = _ipo("ABC테크", is_sample=True, data_origin="sample")
    db_session.add_all([rich, pre, active, appendix, sample])
    db_session.flush()

    db_session.add_all(
        [
            SourceSnapshot(
                source="kind-official",
                source_key="https://kind.example/item?<unsafe>",
                ipo_id=rich.id,
                fetched_at=datetime(2026, 7, 18, 1, 0, tzinfo=timezone.utc),
                raw_text_hash="kind-rich",
            ),
            SourceSnapshot(
                source="sample",
                source_key="sample-newer",
                ipo_id=rich.id,
                fetched_at=datetime(2026, 7, 19, 1, 0, tzinfo=timezone.utc),
                raw_text_hash="sample-rich",
            ),
            DemandForecast(
                ipo_id=rich.id,
                institutional_competition_ratio=1234.56,
                lockup_commitment_ratio=17.5,
                price_band_top_ratio=89.1,
                above_band_participation_ratio=72.3,
                final_price_position="밴드 상단",
                participants_count=2100,
                result_published_at=datetime(2026, 6, 24, 1, 0, tzinfo=timezone.utc),
                source="dart-official",
                receipt_no="202607190001",
            ),
            DemandForecast(
                ipo_id=rich.id,
                institutional_competition_ratio=9999,
                result_published_at=datetime(2026, 7, 19, 1, 0, tzinfo=timezone.utc),
                source="sample",
                receipt_no="sample-receipt",
            ),
            IpoStructureMetric(
                ipo_id=rich.id,
                total_offer_shares=1_000_000,
                new_share_ratio=80,
                secondary_share_ratio=20,
                free_float_shares_on_listing=300_000,
                free_float_market_cap=6_000_000_000,
                free_float_ratio_on_listing=30,
                vc_overhang_ratio=5.5,
                lockup_expiry_schedule_json={"1개월": "10%"},
                use_of_proceeds_summary="설비 투자",
                risk_flags_json=["오버행"],
                as_of_date=date(2026, 6, 24),
                source="dart-official",
            ),
            UnderwriterAllocation(
                ipo_id=rich.id,
                broker_name="KB&증권",
                role="대표주관",
                public_offer_shares=600_000,
                retail_allocated_shares=150_000,
                actual_applicants_count=123_456,
                actual_proportional_competition=321.45,
                actual_source="broker-official",
                actual_recorded_at=datetime(2026, 7, 2, 8, 0, tzinfo=timezone.utc),
                subscription_fee=2_000,
                account_open_deadline=date(2026, 6, 30),
                source="kind-official",
            ),
            UnderwriterAllocation(
                ipo_id=rich.id,
                broker_name="IBK투자증권",
                role="공동주관",
                actual_applicants_count=9_999,
                actual_proportional_competition=1000.0,
                actual_source="broker-official",
                actual_recorded_at=datetime(2026, 7, 2, 8, 0, tzinfo=timezone.utc),
                source="kind-official",
            ),
            UnderwriterAllocation(
                ipo_id=rich.id,
                broker_name="가짜증권",
                source="sample",
            ),
            ListingPrice(
                ipo_id=rich.id,
                ticker="123456",
                listing_date=date(2026, 7, 13),
                d0_open_price=30_000,
                d0_close_price=22_000,
                d1_close_price=18_000,
                d3_close_price=16_000,
                source="krx-official",
            ),
            OutcomeEvaluation(
                ipo_id=rich.id,
                offer_price=20_000,
                d0_open_return_pct=50,
                d0_close_return_pct=10,
                d1_close_return_pct=-10,
                d3_close_return_pct=-20,
                evaluated_at=datetime(2026, 7, 18, tzinfo=timezone.utc),
            ),
        ]
    )
    db_session.flush()

    core_entries = [
        SimpleNamespace(
            ipo=rich,
            event_badges=("subscription", "planned_listing", "actual_listing"),
            reconciliation_state="cross_checked",
        ),
        SimpleNamespace(ipo=rich, event_badges=("중복",)),
        SimpleNamespace(ipo=pre, event_badges=()),
        SimpleNamespace(ipo=active, event_badges=("청약",)),
        SimpleNamespace(ipo=sample, event_badges=("상장",)),
        SimpleNamespace(ipo_id=999_999, event_badges=("없는 IPO",)),
    ]
    appendix_entries = [
        SimpleNamespace(
            ipo_id=appendix.id,
            event_badges=("정정",),
            reconciliation_state="reported",
        ),
        SimpleNamespace(ipo=rich, event_badges=("정정",)),
    ]
    universe = SimpleNamespace(
        core=core_entries,
        filing_update_appendix=appendix_entries,
        reconciliation_state="공식 대조 완료",
    )

    bundle = build_monthly_ipo_email(
        db_session,
        universe,
        month_start=date(2026, 7, 1),
        month_end=date(2026, 7, 31),
        generated_at=datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc),
    )

    assert len(bundle.parts) == 1
    assert len(bundle.read_model.core) == 3
    assert len(bundle.read_model.filing_appendix) == 1
    assert bundle.universe_hash == bundle.read_model.universe_hash
    assert bundle.evidence_hash == bundle.read_model.evidence_hash
    stages = {brief.corp_name: brief.stage for brief in bundle.read_model.core}
    assert stages == {
        "안전&성장<IPO>": "POST_CLOSE",
        "청약전": "PRE_OPEN",
        "청약중": "IN_SUBSCRIPTION",
    }
    content = bundle.parts[0]
    assert "전체 3개" in content.subject
    assert "청약 이벤트 3 · 상장 이벤트 1 · 데이터 보강 필요 3" in content.subject
    for value in (
        "안전&성장<IPO>",
        "청약전",
        "청약중",
        "정정공시",
        "KB&증권",
        "IBK투자증권",
        "1,234.56:1",
        "17.50%",
        "321.45:1",
        "123,456명",
        "+50.00%",
        "-20.00%",
        "공모 구조",
        "출처 dart-official",
        "as-of",
        DISCLAIMER,
    ):
        assert value in content.text
    assert "ABC테크" not in content.text
    assert "가짜증권" not in content.text
    assert "9,999.00:1" not in content.text
    assert "1,000.00:1" not in content.text
    assert "9999" not in content.text
    assert "안전&amp;성장&lt;IPO&gt;" in content.html
    assert "KB&amp;증권" in content.html
    assert "&lt;unsafe&gt;" not in content.html
    assert "<unsafe>" not in content.html
    assert '<meta name="viewport" content="width=device-width,initial-scale=1">' in content.html
    assert "word-break:break-word" in content.html
    assert "calc(" not in content.html
    assert "min-width" not in content.html
    assert "white-space:nowrap" not in content.html
    assert len(content.html.encode("utf-8")) < DEFAULT_MAX_HTML_BYTES
    assert content.html.index("한눈에 보기") < content.html.index("기본")
    assert "기관 가격분포 원문이 수집되지 않아 차트를 생성하지 않았습니다." in content.html


def test_single_ipo_report_has_top_summary_and_count_recalculated_price_chart(db_session):
    ipo = _ipo(
        "분포기업",
        subscription_start_date=date(2026, 7, 20),
        subscription_end_date=date(2026, 7, 21),
        demand_end_date=date(2026, 7, 18),
        offer_price_min=18_500,
        offer_price_max=21_500,
        offer_price_final=21_500,
    )
    db_session.add(ipo)
    db_session.flush()

    rows = (
        ("가격미제시", None, 10, 100, 0.013, 0.011),
        ("21,500원 초과", 21_500, 10, 900, 0.029, 0.030),
        ("21,500원 (상단)", 21_500, 80, 800, 0.956, 0.948),
        ("18,500원 초과 21,500원 미만", 18_500, 5, 50, 0.001, 0.002),
        ("18,500원 (하단)", 18_500, 4, 40, 0.006, 0.009),
        ("18,500원 미만", 18_500, 1, 10, 0.0, 0.0),
    )

    def fact(value, *, raw=None, unit=None):
        rendered = _observation_value(value, unit)
        return EvidenceFact(
            "원문",
            rendered,
            "reported",
            "dart_public",
            "2026.07.18 10:00 KST",
            "https://dart.example/R1",
            raw_value=raw,
        )

    def adapter(_session, _ipos, _generated_at):
        overrides = {
            "offer_price_band": fact("18,500–21,500원"),
            "offer_price_final": fact(21_500, unit="KRW"),
            "institutional_competition_ratio": fact(1500.2, unit="ratio_to_one"),
            "participants_count": fact(110, unit="institutions"),
            "lockup_commitment_ratio": fact(0.25, unit="ratio"),
            "final_price_position": fact("밴드 상단"),
        }
        for index, (label, price, count, shares, ratio, share_ratio) in enumerate(rows):
            raw = {
                "label": label,
                "price_krw": price,
                "participants_count": count,
                "requested_shares": shares,
                # Deliberately inconsistent official ratios: chart must use counts.
                "ratio": ratio,
                "requested_shares_ratio": share_ratio,
            }
            overrides[f"institutional_price_distribution.{index}"] = fact(
                raw, raw=raw, unit="mixed"
            )
        return {ipo.id: overrides}

    bundle = build_single_ipo_email(
        db_session,
        ipo,
        month_start=date(2026, 7, 1),
        month_end=date(2026, 7, 31),
        generated_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
        fact_adapter=adapter,
    )
    content = bundle.parts[0]
    assert content.subject == "[Korea IPO] 청약 D-1 · 분포기업 · 7월 20일 청약 시작"
    assert "전체 1개" not in content.subject
    assert content.text.startswith("[청약 D-1] 내일 일반청약 시작")
    assert "청약 D-1 · 내일 일반청약 시작" in content.html
    assert content.html.index("한눈에 보기") < content.html.index("기관 가격제시 분포")
    for value in (
        "희망밴드",
        "18,500–21,500원",
        "확정 공모가",
        "21,500원",
        "확정가 위치",
        "밴드 상단",
        "최다 가격제시",
        "참여기관 최다: 21,500원 (상단) 72.73% (80건)",
        "신청수량 최다: 21,500원 초과 47.37% (900주)",
        "참여기관 건수 합계(가격미제시 포함)로 재계산한 비중",
        "신청수량 합계(가격미제시 포함)로 재계산한 비중",
        "신청수량 비중",
        "42.11%",
        "800주",
        "참여기관 최다",
        "신청수량 최다",
        "출처 dart_public",
        "as-of 2026.07.18 10:00 KST",
        "근거 https://dart.example/R1",
    ):
        assert value in content.text
        assert value in content.html
    assert 'style="width:72.727273%;' in content.html
    assert 'style="width:47.368421%;' in content.html
    assert "min-width" not in content.html
    assert content.text.count("참여기관 비중 [") == 6
    assert content.text.count("신청수량 비중 [") == 6

    chart = content.text[content.text.index("기관 가격제시 분포 차트:") :]
    chart = chart[chart.index("\n- ") + 1 :]
    ordered_labels = (
        "21,500원 초과",
        "21,500원 (상단)",
        "18,500원 초과 21,500원 미만",
        "18,500원 (하단)",
        "18,500원 미만",
        "가격미제시",
    )
    positions = tuple(chart.index(label) for label in ordered_labels)
    assert positions == tuple(sorted(positions))

    rendered_again = render_single_ipo_email(bundle.read_model)
    assert rendered_again == content
    reconciled = build_single_ipo_email(
        db_session,
        SimpleNamespace(
            ipo=ipo,
            event_badges=("subscription",),
            reconciliation_state="KIND 청약일 일치",
        ),
        month_start=date(2026, 7, 1),
        month_end=date(2026, 7, 31),
        generated_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
        fact_adapter=adapter,
    )
    assert reconciled.read_model.reconciliation_state == "KIND 청약일 일치"
    assert "공식 대조 KIND 청약일 일치" in reconciled.parts[0].text
    assert "공식 대조 KIND 청약일 일치" in reconciled.parts[0].html
    with pytest.raises(ValueError, match="max_html_bytes"):
        render_single_ipo_email(bundle.read_model, max_html_bytes=0)
    with pytest.raises(ValueError, match="single IPO detail"):
        render_single_ipo_email(bundle.read_model, max_html_bytes=100)
    with pytest.raises(ValueError, match="exactly one core IPO"):
        render_single_ipo_email(replace(bundle.read_model, core=()))

    unsafe = _ipo("샘플", is_sample=True, data_origin="sample")
    db_session.add(unsafe)
    db_session.flush()
    with pytest.raises(ValueError, match="exactly one operational"):
        build_single_ipo_email(
            db_session,
            unsafe,
            month_start=date(2026, 7, 1),
            month_end=date(2026, 7, 31),
            generated_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
        )


def test_price_distribution_fallback_ties_conflicts_and_malformed_values(db_session):
    import app.services.monthly_email as monthly_email

    def raw_fact(raw, *, available=True):
        return EvidenceFact(
            "원문",
            "값",
            "reported",
            "dart",
            "2026.07.18",
            "R1",
            available=available,
            raw_value=raw,
        )

    reported_only = _price_distribution_bands(
        {
            "institutional_price_distribution.0": raw_fact(
                {
                    "label": "기타 제시",
                    "ratio": 0.6,
                    "requested_shares_ratio": 0.7,
                    "participants_count": None,
                    "requested_shares": None,
                    "price_krw": "10,000",
                }
            ),
            "institutional_price_distribution.1": raw_fact(
                {
                    "label": "가격미제시",
                    "ratio": 0.4,
                    "requested_shares_ratio": 0.3,
                }
            ),
            "institutional_price_distribution.2": raw_fact("not-a-map"),
            "institutional_price_distribution.3": raw_fact({}, available=False),
        }
    )
    assert [band.label for band in reported_only] == ["기타 제시", "가격미제시"]
    assert reported_only[0].participant_ratio_pct == 60
    assert reported_only[0].participant_ratio_basis == "reported_ratio"
    assert reported_only[0].requested_shares_ratio_pct == 70
    assert monthly_email._distribution_ratio_basis(reported_only).startswith("참여기관 건수 미공시")
    assert monthly_email._requested_shares_ratio_basis(reported_only).startswith("신청수량 미공시")
    assert _price_distribution_sort_key(reported_only[0])[0] == 2

    partial_counts_with_complete_reported_ratios = _price_distribution_bands(
        {
            "institutional_price_distribution.0": raw_fact(
                {
                    "label": "10,000원 (상단)",
                    "participants_count": 10,
                    "requested_shares": 100,
                    "ratio": 0.6,
                    "requested_shares_ratio": 0.7,
                }
            ),
            "institutional_price_distribution.1": raw_fact(
                {
                    "label": "9,000원 (하단)",
                    "participants_count": None,
                    "requested_shares": None,
                    "ratio": 0.4,
                    "requested_shares_ratio": 0.3,
                }
            ),
        }
    )
    assert [
        band.participant_ratio_pct for band in partial_counts_with_complete_reported_ratios
    ] == [
        60,
        40,
    ]
    assert all(
        band.participant_ratio_basis == "reported_ratio"
        for band in partial_counts_with_complete_reported_ratios
    )
    assert [
        band.requested_shares_ratio_pct for band in partial_counts_with_complete_reported_ratios
    ] == [70, 30]
    assert all(
        band.requested_shares_ratio_basis == "reported_ratio"
        for band in partial_counts_with_complete_reported_ratios
    )

    partial_without_complete_ratios = _price_distribution_bands(
        {
            "institutional_price_distribution.0": raw_fact(
                {
                    "label": "10,000원 (상단)",
                    "participants_count": 10,
                    "requested_shares": 100,
                    "ratio": 0.6,
                    "requested_shares_ratio": 0.7,
                }
            ),
            "institutional_price_distribution.1": raw_fact(
                {
                    "label": "9,000원 (하단)",
                    "participants_count": None,
                    "requested_shares": None,
                    "ratio": None,
                    "requested_shares_ratio": None,
                }
            ),
        }
    )
    assert all(band.participant_ratio_pct is None for band in partial_without_complete_ratios)
    assert all(
        band.participant_ratio_basis == "unavailable" for band in partial_without_complete_ratios
    )
    assert all(band.requested_shares_ratio_pct is None for band in partial_without_complete_ratios)
    assert all(
        band.requested_shares_ratio_basis == "unavailable"
        for band in partial_without_complete_ratios
    )

    all_zero_counts = _price_distribution_bands(
        {
            "institutional_price_distribution.0": raw_fact(
                {
                    "label": "10,000원 (상단)",
                    "participants_count": 0,
                    "requested_shares": 0,
                    "ratio": 0.8,
                    "requested_shares_ratio": 0.8,
                }
            ),
            "institutional_price_distribution.1": raw_fact(
                {
                    "label": "9,000원 (하단)",
                    "participants_count": 0,
                    "requested_shares": 0,
                    "ratio": 0.2,
                    "requested_shares_ratio": 0.2,
                }
            ),
        }
    )
    assert all(band.participant_ratio_pct is None for band in all_zero_counts)
    assert all(band.requested_shares_ratio_pct is None for band in all_zero_counts)
    assert monthly_email._distribution_ratio_basis(all_zero_counts) == "참여기관 비중 확인 불가"
    assert monthly_email._requested_shares_ratio_basis(all_zero_counts) == "신청수량 비중 확인 불가"

    missing_ratios = _price_distribution_bands(
        {
            "institutional_price_distribution.0": raw_fact(
                {"label": "가격 구간", "ratio": None, "requested_shares_ratio": None}
            )
        }
    )
    assert missing_ratios[0].participant_ratio_basis == "unavailable"
    assert missing_ratios[0].requested_shares_ratio_basis == "unavailable"

    assert _distribution_percent(None) is None
    assert _distribution_percent(True) is None
    assert _distribution_percent("bad") is None
    assert _distribution_percent(float("nan")) is None
    assert _distribution_percent(-2) is None
    assert _distribution_percent(101) is None
    assert monthly_email._optional_int(True) is None
    assert monthly_email._optional_int("bad") is None

    base = reported_only[0]
    lower = replace(base, label="11,000원 (하단)", price_krw=11_000)
    upper = replace(base, label="10,000원 (상단)", price_krw=10_000)
    assert _derived_distribution_offer_band((lower, upper)) is None
    duplicate_lower = replace(lower, label="9,000원 (하단)", price_krw=9_000)
    assert _derived_distribution_offer_band((lower, duplicate_lower, upper)) is None

    zero = replace(
        base,
        participant_ratio_pct=0,
        requested_shares_ratio_pct=0,
        participants_count=0,
        requested_shares=0,
    )
    assert _top_distribution_bands((zero,)) == ()
    assert _top_distribution_summary((zero,))[0].startswith("확인 불가")
    assert monthly_email._top_requested_share_bands((zero,)) == ()
    assert monthly_email._top_requested_shares_summary((zero,))[0].startswith(
        "신청수량 최다 확인 불가"
    )

    tie_a = replace(
        base,
        label="A",
        participant_ratio_pct=50,
        requested_shares_ratio_pct=50,
        participants_count=5,
        requested_shares=50,
    )
    tie_b = replace(tie_a, label="B")
    assert len(_top_distribution_bands((tie_a, tie_b))) == 2
    assert len(monthly_email._top_requested_share_bands((tie_a, tie_b))) == 2
    assert _top_distribution_summary((tie_a, tie_b))[0].startswith("참여기관 공동 최다")
    assert monthly_email._top_requested_shares_summary((tie_a, tie_b))[0].startswith(
        "신청수량 공동 최다"
    )

    ipo = _ipo("경계분포", subscription_start_date=None)
    db_session.add(ipo)
    db_session.flush()

    def adapter(_session, _ipos, _generated_at):
        return {
            ipo.id: {
                "offer_price_band": EvidenceFact(
                    "밴드", "8,000–10,000원", "reported", "dart", "2026.07.18", "R1"
                )
            }
        }

    bundle = build_single_ipo_email(
        db_session,
        ipo,
        month_start=date(2026, 7, 1),
        month_end=date(2026, 7, 31),
        generated_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
        fact_adapter=adapter,
    )
    assert "청약일 미확인 청약 시작" in bundle.parts[0].subject
    assert "분포 라벨 기준 확인 불가" in bundle.parts[0].text

    brief = bundle.read_model.core[0]
    iso_schedule = tuple(
        replace(fact, value="2026-07-13~2026-07-14") if fact.label == "청약" else fact
        for fact in brief.schedule
    )
    assert monthly_email._subscription_start_subject(replace(brief, schedule=iso_schedule)) == (
        "7월 13일"
    )
    conflicted_bands = (
        replace(base, label="9,000원 (하단)", price_krw=9_000),
        replace(base, label="10,000원 (상단)", price_krw=10_000),
    )
    conflicted = replace(brief, institutional_price_distribution=conflicted_bands)
    conflict_model = replace(bundle.read_model, core=(conflicted,))
    conflict = render_single_ipo_email(conflict_model)
    assert "출처 충돌 — 확인 필요" in conflict.text

    no_data = replace(
        brief,
        offer=tuple(replace(fact, available=False) for fact in brief.offer),
        institutional_price_distribution=(),
    )
    assert monthly_email._offer_band_context(no_data)[0] == "확인 불가"

    zero_brief = replace(brief, institutional_price_distribution=(zero,))
    zero_content = render_single_ipo_email(replace(bundle.read_model, core=(zero_brief,)))
    assert "확인 불가(공시 비중이 모두 0%)" in zero_content.text
    assert "신청수량 최다 확인 불가" in zero_content.html
    assert (
        "참여건수 미공시"
        in render_single_ipo_email(
            replace(
                bundle.read_model, core=(replace(brief, institutional_price_distribution=(base,)),)
            )
        ).text
    )

    shares_only = replace(
        base,
        participant_ratio_pct=None,
        participant_ratio_basis="unavailable",
        requested_shares_ratio_pct=70,
        requested_shares_ratio_basis="reported_ratio",
        requested_shares=700,
    )
    shares_only_content = render_single_ipo_email(
        replace(
            bundle.read_model,
            core=(replace(brief, institutional_price_distribution=(shares_only,)),),
        )
    )
    assert "참여기관 비중 [--------------------] 비중 미공시" in shares_only_content.text
    assert "신청수량 비중 [##############------] 70.00% · 700주" in shares_only_content.text
    assert 'style="width:70.000000%;' in shares_only_content.html


def test_same_day_artificial_eod_visibility_requires_live_proof_before_cutoff():
    cutoff = datetime(2026, 7, 18, 9, 30, tzinfo=timezone.utc)  # 18:30 KST

    def observation(*, published, observed, fetched):
        return IpoFactObservation(
            observation_key=f"{published}-{observed}-{fetched}",
            ipo_id=1,
            field_name="offer_price_final",
            value_json=10_000,
            quality_state="reported",
            knowledge_phase="pre_subscription",
            published_at=published,
            observed_at=observed,
            fetched_at=fetched,
        )

    same_day_eod = datetime(2026, 7, 18, 14, 59, 59, tzinfo=timezone.utc)  # 23:59 KST
    live = observation(
        published=same_day_eod,
        observed=datetime(2026, 7, 18, 8, 30, tzinfo=timezone.utc),
        fetched=datetime(2026, 7, 18, 8, 31, tzinfo=timezone.utc),
    )
    assert _observation_is_visible(live, cutoff, "PRE_OPEN") is True

    historical_backfill = observation(
        published=same_day_eod,
        observed=datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc),
        fetched=datetime(2026, 7, 18, 10, 1, tzinfo=timezone.utc),
    )
    assert _observation_is_visible(historical_backfill, cutoff, "PRE_OPEN") is False

    non_eod_future = observation(
        published=datetime(2026, 7, 18, 13, 0, tzinfo=timezone.utc),
        observed=datetime(2026, 7, 18, 8, 30, tzinfo=timezone.utc),
        fetched=datetime(2026, 7, 18, 8, 31, tzinfo=timezone.utc),
    )
    assert _observation_is_visible(non_eod_future, cutoff, "PRE_OPEN") is False

    no_live_proof = observation(published=same_day_eod, observed=None, fetched=None)
    assert _observation_is_visible(no_live_proof, cutoff, "PRE_OPEN") is False


def test_plain_iterable_missing_states_empty_mail_and_input_errors(db_session):
    before = _ipo(
        "미래",
        demand_start_date=date(2026, 7, 20),
        demand_end_date=date(2026, 7, 22),
        subscription_start_date=date(2026, 7, 25),
        subscription_end_date=None,
        listing_date=date(2026, 8, 1),
    )
    no_dates = _ipo("일정미정", data_origin=None)
    synthetic = _ipo("합성", data_origin="synthetic")
    filing_only = _ipo("공시전용", report_filed_date=date(2026, 7, 10))
    db_session.add_all([before, no_dates, synthetic, filing_only])
    db_session.flush()

    bundle = build_monthly_ipo_email(
        db_session,
        [before, no_dates, synthetic],
        month_start=date(2026, 7, 1),
        month_end=date(2026, 7, 31),
        generated_at=datetime(2026, 7, 19, 9, 0),
        title="7월 확인",
    )

    assert [brief.corp_name for brief in bundle.read_model.core] == ["미래", "일정미정"]
    assert "공시 전" in bundle.parts[0].text
    assert "미수집" in bundle.parts[0].text
    assert "월간 포함" in bundle.parts[0].text
    assert "합성" not in bundle.parts[0].text
    assert "7월 확인 전체 2개" in bundle.parts[0].subject

    actual_universe = list_monthly_universe(
        db_session,
        date(2026, 7, 1),
        date(2026, 7, 31),
    )
    actual_bundle = build_monthly_ipo_email(
        db_session,
        actual_universe,
        month_start=date(2026, 7, 1),
        month_end=date(2026, 7, 31),
        generated_at=datetime(2026, 7, 19, 9, 0),
    )
    assert [brief.corp_name for brief in actual_bundle.read_model.filing_appendix] == ["공시전용"]
    assert actual_bundle.read_model.reconciliation_state == "unreconciled"

    legacy_alias = SimpleNamespace(
        core=(),
        filing_appendix=(SimpleNamespace(ipo=filing_only, event_badges=("정정",)),),
        reconciliation_state="legacy",
    )
    legacy_bundle = build_monthly_ipo_email(
        db_session,
        legacy_alias,
        month_start=date(2026, 7, 1),
        month_end=date(2026, 7, 31),
        generated_at=datetime(2026, 7, 19, 9, 0),
    )
    assert legacy_bundle.read_model.filing_appendix[0].corp_name == "공시전용"

    one_hour_later = build_monthly_ipo_email(
        db_session,
        actual_universe,
        month_start=date(2026, 7, 1),
        month_end=date(2026, 7, 31),
        generated_at=datetime(2026, 7, 19, 10, 0),
    )
    assert actual_bundle.evidence_hash == one_hour_later.evidence_hash
    assert actual_bundle.parts[0].text != one_hour_later.parts[0].text

    empty = build_monthly_ipo_email(
        db_session,
        None,
        month_start=date(2026, 7, 1),
        month_end=date(2026, 7, 31),
        generated_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
    )
    assert empty.parts[0].subject.endswith("데이터 보강 필요 0")
    assert "수록 종목 없음" in empty.parts[0].text
    assert "수록 종목 없음" in empty.parts[0].html

    with pytest.raises(ValueError, match="month_start"):
        build_monthly_email_read_model(
            db_session,
            [],
            month_start=date(2026, 8, 1),
            month_end=date(2026, 7, 1),
            generated_at=datetime(2026, 7, 19),
        )
    with pytest.raises(TypeError, match="iterable"):
        build_monthly_email_read_model(
            db_session,
            7,
            month_start=date(2026, 7, 1),
            month_end=date(2026, 7, 31),
            generated_at=datetime(2026, 7, 19),
        )
    with pytest.raises(ValueError, match="positive"):
        render_monthly_ipo_email(empty.read_model, max_html_bytes=0)
    with pytest.raises(ValueError, match="header"):
        render_monthly_ipo_email(empty.read_model, max_html_bytes=100)


def test_deterministic_split_and_atomic_size_failure(db_session):
    ipos = [
        _ipo(
            f"분할기업{i}",
            subscription_start_date=date(2026, 7, i),
            subscription_end_date=date(2026, 7, i + 1),
            listing_date=date(2026, 7, i + 10),
        )
        for i in range(1, 5)
    ]
    db_session.add_all(ipos)
    db_session.flush()
    read_model = build_monthly_email_read_model(
        db_session,
        ipos,
        month_start=date(2026, 7, 1),
        month_end=date(2026, 7, 31),
        generated_at=datetime(2026, 7, 19),
    )
    one_item_model = MonthlyEmailReadModel(
        month_start=read_model.month_start,
        month_end=read_model.month_end,
        generated_at=read_model.generated_at,
        core=read_model.core[:1],
        filing_appendix=(),
        reconciliation_state=read_model.reconciliation_state,
        universe_hash=read_model.universe_hash,
        evidence_hash=read_model.evidence_hash,
        historical_evidence=read_model.historical_evidence,
    )
    one_size = len(render_monthly_ipo_email(one_item_model)[0].html.encode("utf-8"))
    split = render_monthly_ipo_email(
        read_model,
        max_html_bytes=one_size + 500,
    )
    repeated = render_monthly_ipo_email(
        read_model,
        max_html_bytes=one_size + 500,
    )
    assert len(split) > 1
    assert split == repeated
    assert all(f"({index}/{len(split)})" in part.subject for index, part in enumerate(split, 1))
    combined = "\n".join(part.text for part in split)
    assert all(combined.count(ipo.corp_name) == 2 for ipo in ipos)
    assert all(len(part.html.encode("utf-8")) <= one_size + 500 for part in split)

    with pytest.raises(ValueError, match="single IPO"):
        render_monthly_ipo_email(read_model, max_html_bytes=one_size - 100)

    huge_second = replace(read_model.core[1], corp_name="대" * 20_000)
    mixed_model = replace(read_model, core=(read_model.core[0], huge_second))
    with pytest.raises(ValueError, match="single IPO"):
        render_monthly_ipo_email(mixed_model, max_html_bytes=one_size + 500)


def test_official_observation_adapter_precedes_current_rows_and_enforces_actual_gate(db_session):
    ipo = _ipo(
        "공식근거",
        subscription_start_date=date(2026, 7, 1),
        subscription_end_date=date(2026, 7, 2),
        listing_date=date(2026, 7, 20),
        offer_price_final=10_000,
    )
    db_session.add(ipo)
    db_session.flush()
    db_session.add(
        UnderwriterAllocation(
            ipo_id=ipo.id,
            broker_name="공식증권",
            source="kind",
        )
    )
    official = SourceDocument(
        ipo_id=ipo.id,
        source="dart-official",
        document_key="R1",
        document_type="result",
        lineage_root_key="ROOT",
        official_url="https://dart.example/R1",
        published_at=datetime(2026, 7, 3, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 7, 3, 1, tzinfo=timezone.utc),
    )
    unsafe = SourceDocument(
        ipo_id=ipo.id,
        source="sample",
        document_key="S1",
        document_type="result",
        lineage_root_key="SROOT",
        fetched_at=datetime(2026, 7, 3, 1, tzinfo=timezone.utc),
    )
    db_session.add_all([official, unsafe])
    db_session.flush()

    def observation(
        key,
        field,
        value,
        *,
        document=official,
        quality="verified",
        phase="pre_subscription",
        published=datetime(2026, 7, 3, tzinfo=timezone.utc),
        observed=datetime(2026, 7, 3, 1, tzinfo=timezone.utc),
        unit=None,
    ):
        return IpoFactObservation(
            observation_key=key,
            ipo_id=ipo.id,
            source_document_id=document.id if document else None,
            field_name=field,
            value_json=value,
            unit=unit,
            quality_state=quality,
            knowledge_phase=phase,
            published_at=published,
            observed_at=observed,
        )

    db_session.add_all(
        [
            observation("old", "institutional_competition_ratio", 111, unit=":1"),
            observation(
                "new",
                "institutional_competition_ratio",
                222,
                observed=datetime(2026, 7, 4, 1, tzinfo=timezone.utc),
                unit=":1",
            ),
            observation(
                "late-insert-old-time",
                "institutional_competition_ratio",
                150,
                observed=datetime(2026, 7, 3, 12, tzinfo=timezone.utc),
                unit=":1",
            ),
            observation(
                "future",
                "institutional_competition_ratio",
                999,
                published=datetime(2026, 7, 30, tzinfo=timezone.utc),
                observed=datetime(2026, 7, 30, 1, tzinfo=timezone.utc),
            ),
            observation(
                "retail-1000", "retail_competition_ratio", "1,000:1", phase="post_subscription"
            ),
            observation(
                "retail-real",
                "overall_retail_competition_ratio",
                333.3,
                phase="post_subscription",
                unit=":1",
            ),
            observation(
                "broker-ratio",
                "actual_proportional_competition",
                {"broker_name": "공식증권", "value": 444.4},
                phase="post_subscription",
                unit=":1",
            ),
            observation(
                "broker-applicants",
                "underwriter.공식증권.actual_applicants_count",
                1000,
                phase="post_subscription",
                unit="명",
            ),
            observation("risk-list", "risk_flags_json", ["위험", "확인"], quality="conflict"),
            observation("unknown-quality", "ticker", "654321", quality="brand_new_state"),
            observation("none-value", "market", None),
            observation("unsafe-doc", "offer_price_final", 99_999, document=unsafe),
            observation("missing-doc", "offer_amount_million", 77, document=None),
            observation(
                "colon-key",
                "broker:공식증권:actual_applicants_count",
                2000,
                phase="post_subscription",
                unit="명",
            ),
        ]
    )
    db_session.flush()

    bundle = build_monthly_ipo_email(
        db_session,
        [ipo],
        month_start=date(2026, 7, 1),
        month_end=date(2026, 7, 31),
        generated_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
    )
    text = bundle.parts[0].text
    assert "222:1" in text and "111:1" not in text and "999" not in text
    assert "333.3:1" in text
    assert "444.4:1" in text
    assert "2000명" in text
    assert "1,000:1" not in text
    assert "654321" in text
    assert '["위험","확인"]' in text
    assert "https://dart.example/R1" in text
    assert "99,999원" not in text

    calls = []

    def custom_adapter(_session, rows, generated_at):
        calls.append((rows, generated_at))
        return {
            ipo.id: {
                "offer_price_final": EvidenceFact(
                    "override",
                    "12,345원",
                    "verified",
                    "manual-official",
                    "2026.07.19",
                ),
                "overall_retail_competition_ratio": EvidenceFact(
                    "bad-stage",
                    "777:1",
                    "reported",
                    "broker",
                    "2026.07.19",
                ),
                "underwriter:공식증권:actual_proportional_competition": EvidenceFact(
                    "default",
                    "1,000.00:1",
                    "post_event",
                    "broker",
                    "2026.07.19",
                ),
            }
        }

    custom = build_monthly_ipo_email(
        db_session,
        [ipo],
        month_start=date(2026, 7, 1),
        month_end=date(2026, 7, 31),
        generated_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
        fact_adapter=custom_adapter,
    )
    assert calls and "12,345원" in custom.parts[0].text
    assert "777:1" not in custom.parts[0].text
    assert "1,000.00:1" not in custom.parts[0].text

    transient = _ipo("저장전")
    assert (
        load_official_fact_overrides(
            db_session,
            (transient,),
            datetime(2026, 7, 19, tzinfo=timezone.utc),
        )
        == {}
    )


def test_helper_contracts_and_price_fallback_cover_all_boundary_states():
    ipo = _ipo(
        "경계",
        identity_key=None,
        subscription_start_date=date(2026, 7, 20),
        subscription_end_date=date(2026, 7, 21),
        demand_end_date=date(2026, 7, 19),
        listing_date=date(2026, 7, 30),
        offer_price_final=10_000,
    )
    assert _ipo_key(ipo)[0] == "natural"
    ipo.identity_key = "kind:transient"
    assert _ipo_key(ipo) == ("identity", "kind:transient")
    ipo.identity_key = None
    assert _decision_stage(ipo, date(2026, 7, 19)) == "PRE_OPEN"
    assert _decision_stage(ipo, date(2026, 7, 20)) == "IN_SUBSCRIPTION"
    assert _decision_stage(ipo, date(2026, 7, 22)) == "POST_CLOSE"
    assert _demand_missing_state(ipo, date(2026, 7, 19)) == "not_published"
    assert _demand_missing_state(ipo, date(2026, 7, 20)) == "missing"
    assert _retail_missing_state("PRE_OPEN") == "not_published"
    assert _retail_missing_state("POST_CLOSE") == "missing"
    assert _outcome_missing_state(ipo, date(2026, 7, 30), 0) == "waiting"
    assert _outcome_missing_state(ipo, date(2026, 8, 5), 3) == "missing"
    ipo.listing_date = None
    assert _outcome_missing_state(ipo, date(2026, 8, 5), 0) == "missing"

    assert _overlaps(None, None, date(2026, 7, 1), date(2026, 7, 31)) is False
    assert _overlaps(None, date(2026, 7, 3), date(2026, 7, 1), date(2026, 7, 31)) is True
    assert _overlaps(date(2026, 7, 3), None, date(2026, 8, 1), date(2026, 8, 31)) is False
    assert _event_badges(ipo, date(2026, 7, 1), date(2026, 7, 31)) == (
        "수요예측",
        "청약",
    )

    assert list(_as_iterable(None)) == []
    assert _source_state(None) == "legacy_unverified"
    assert _source_state("manual") == "legacy_unverified"
    assert _source_state("dart") == "reported"
    assert _safe_source("dart") is True
    assert _safe_source("my-test-fixture") is False
    assert _is_default_competition("1,000:1") is True
    assert _is_default_competition("not-a-number") is False
    assert _format_as_of(None) == "미확인"
    assert _database_datetime(None).tzinfo == timezone.utc
    assert _format_as_of(date(2026, 7, 1)) == "2026.07.01"
    assert "KST" in _format_as_of(datetime(2026, 7, 1, tzinfo=timezone.utc))
    assert _date_range(date(2026, 7, 1), date(2026, 7, 2)) == "2026.07.01–2026.07.02"
    assert _date_range(date(2026, 7, 1), date(2026, 7, 1)) == "2026.07.01"
    assert _date_range(None, None) is None
    assert _price_band(10_000, 12_000) == "10,000–12,000원"
    assert _price_band(10_000, 10_000) == "10,000원"
    assert _price_band(None, 12_000) == "12,000원"
    assert _money(None) is None and _money(1000) == "1,000원"
    assert _million_won(None) is None and _million_won(100) == "100백만원"
    assert _shares(None) is None and _shares(1000) == "1,000주"
    assert _count(None, "명") is None and _count(2, "명") == "2명"
    assert _ratio(None) is None and _ratio(1.2) == "1.20:1"
    assert _percent(None) is None and _percent(1.2) == "1.20%"
    assert _percent(0.2) == "20.00%" and _percent(-0.2) == "-20.00%"
    assert _signed_percent(None) is None and _signed_percent(-1.2) == "-1.20%"
    assert _json_value(None) is None and _json_value({}) is None and _json_value([]) is None
    assert _json_value({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    assert _observation_value(None, None) == "미수집"
    assert _observation_value(8_000, "KRW") == "8,000원"
    assert _observation_value(8_000, "원") == "8,000원"
    assert _observation_value(13_757, "백만원") == "13,757백만원 (137.57억원)"
    assert _observation_value(-1_464, "천원") == "-1,464천원 (-0.01억원)"
    assert _observation_value(1396.29, "ratio_to_one") == "1,396.29:1"
    assert _observation_value(0.2739, "ratio") == "27.39%"
    assert _observation_value(12.5, "percent") == "12.50%"
    assert _observation_value(1000, "shares") == "1,000주"
    assert _observation_value(1000, "applicants") == "1,000명"
    assert _observation_value(1000, "institutions") == "1,000개 기관"
    distribution = {
        "label": "8,000원 초과",
        "participants_count": 10,
        "ratio": 0.25,
        "requested_shares": 1000,
    }
    assert _observation_value(distribution, "mixed") == ("8,000원 초과 (10건, 25.00%, 1,000주)")
    assert _price_distribution_value({}) == "가격 구간"
    assert _observation_value(["a"], None) == '["a"]'
    assert _observation_value(3, "개") == "3개"
    assert _scaled_money(8_435_362, "천원", 1_000) == "8,435,362천원 (84.35억원)"
    assert _large_money(149_974_400_000) == "149,974,400,000원 (1,499.7억원)"
    assert _is_retail_result_key("retail_competition_ratio") is True
    assert _is_retail_result_key("broker:x:actual_applicants_count") is True
    assert _is_retail_result_key("broker:x:subscribed_shares") is True
    assert _is_retail_result_key("broker:x:allocated_shares") is True
    assert _is_retail_result_key("market") is False
    assert _price_distribution_fact({}, "missing").available is False
    distribution_fact = EvidenceFact("raw", "상단 90%", "reported", "dart", "2026.07.01", "R1")
    hidden_distribution_fact = replace(distribution_fact, available=False, source_ref="R2")
    combined_distribution = _price_distribution_fact(
        {
            "institutional_price_distribution.1": hidden_distribution_fact,
            "institutional_price_distribution.0": distribution_fact,
        },
        "missing",
    )
    assert combined_distribution.label == "기관 가격분포"
    assert combined_distribution.value == "상단 90%"
    assert combined_distribution.source_ref == "R1"
    assert _return_from_price(None, 100) is None
    assert _return_from_price(100, None) is None
    assert _return_from_price(120, 100) == pytest.approx(20)

    price = ListingPrice(
        d0_open_price=12_000,
        d0_close_price=11_000,
        d1_close_price=9_000,
        d3_close_price=8_000,
        source="krx",
        updated_at=datetime(2026, 8, 5, tzinfo=timezone.utc),
    )
    ipo.listing_date = date(2026, 7, 30)
    facts = _outcome_facts(ipo, None, price, date(2026, 8, 5))
    assert [fact.value for fact in facts] == ["+20.00%", "+10.00%", "-10.00%", "-20.00%"]
    price.source = "sample"
    assert all(not fact.available for fact in _outcome_facts(ipo, None, price, date(2026, 8, 5)))

    fact = EvidenceFact("값", "1", "reported", "dart", "2026.07.01", "receipt")
    assert _fact_meta(fact) == "공시값 · 출처 dart · as-of 2026.07.01 · 근거 receipt"
    assert _brief_fact_value((fact,), "값") == "1"
    assert len(_stable_hash(fact)) == 64
    assert _aggregate_reconciliation(()) == "미검증"
    assert (
        _aggregate_reconciliation(
            (
                SimpleNamespace(reconciliation_state="a"),
                SimpleNamespace(reconciliation_state="b"),
            )
        )
        == "혼합(a, b)"
    )


def test_render_guard_detects_post_render_oversize(monkeypatch):
    model = MonthlyEmailReadModel(
        month_start=date(2026, 7, 1),
        month_end=date(2026, 7, 31),
        generated_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
        core=(),
        filing_appendix=(),
        reconciliation_state="미검증",
        universe_hash="u",
        evidence_hash="e",
    )
    import app.services.monthly_email as monthly_email

    original = monthly_email._partition_items
    monkeypatch.setattr(monthly_email, "_partition_items", lambda *_args: ((),))
    with pytest.raises(ValueError, match="rendered monthly"):
        render_monthly_ipo_email(model, max_html_bytes=1)
    monkeypatch.setattr(monthly_email, "_partition_items", original)
