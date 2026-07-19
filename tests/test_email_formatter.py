from datetime import date, datetime, timezone

from app.services.email_formatter import (
    DISCLAIMER,
    INSUFFICIENT_VALIDATION,
    MISSING_RISK,
    DecisionEmailItem,
    ScheduleEmailItem,
    build_decision_email,
)


def test_decision_email_leads_with_actions_and_escapes_all_dynamic_html():
    item = DecisionEmailItem(
        corp_name="안전&성장<IPO>",
        decision="청약 후보",
        next_action="마감 <전> & 증권사 확인",
        total_score=82.25,
        confidence_score=91.04,
        reasons=("기관 경쟁률 > 1,000:1", "의무보유 <확인>"),
        risks=("유통 가능 물량 30% & 점검",),
        subscription_start_date=date(2026, 7, 20),
        subscription_end_date=date(2026, 7, 21),
        refund_date=date(2026, 7, 23),
        listing_date=date(2026, 7, 30),
        offer_price=20_000,
        broker_name="한국&투자<증권>",
        expected_shares=1.234,
        break_even_return_pct=1.257,
        expected_net_profit=12_345.6,
        positive_probability=0.68,
        validation_sample_count=31,
        validation_summary="과거 <유사군> & n=31 / D1 양수 68%",
    )
    schedules = [
        ScheduleEmailItem("상장&기업<1>", "상장 <확정>", date(2026, 7, 24)),
    ]

    content = build_decision_email(
        "[IPO 판단] <오늘>",
        "2026-07-20 & 청약",
        (row for row in [item]),
        datetime(2026, 7, 20, 8, 32, 59, tzinfo=timezone.utc),
        "과거 시점 데이터만 사용 & 누출 방지",
        schedule_items=(row for row in schedules),
    )

    assert content.subject == "[IPO 판단] <오늘> · 판단 1 · 일정 1"
    for value in (
        "확인할 청약 판단 1건 · 주요 일정 1건",
        "오늘 할 일: 마감 <전> & 증권사 확인",
        "82.2 / 100",
        "91.0%",
        "2026.07.20(월)–2026.07.21(화)",
        "2026.07.23(목)",
        "2026.07.30(목)",
        "20,000원",
        "한국&투자<증권>",
        "1.23주",
        "1.26%",
        "68.0%",
        "12,346원",
        "표본 n=31",
        "7월 20일(월) 17:32 KST",
        DISCLAIMER,
    ):
        assert value in content.text
    for value in (
        "안전&amp;성장&lt;IPO&gt;",
        "마감 &lt;전&gt; &amp; 증권사 확인",
        "기관 경쟁률 &gt; 1,000:1",
        "의무보유 &lt;확인&gt;",
        "유통 가능 물량 30% &amp; 점검",
        "한국&amp;투자&lt;증권&gt;",
        "과거 &lt;유사군&gt; &amp; n=31",
        "상장&amp;기업&lt;1&gt;",
        "상장 &lt;확정&gt;",
        "2026-07-20 &amp; 청약",
        "과거 시점 데이터만 사용 &amp; 누출 방지",
    ):
        assert value in content.html
    assert "<오늘>" not in content.html
    assert content.html.count('role="presentation"') >= 8
    assert "<main" not in content.html
    assert "<section" not in content.html
    assert content.html.index("확인할 청약 판단") < content.html.index("검증 상태")


def test_missing_values_never_render_dangling_units_or_false_safety():
    items = [
        DecisionEmailItem(
            corp_name="단일일정",
            decision="데이터 부족",
            subscription_start_date=date(2026, 8, 1),
        ),
        DecisionEmailItem(
            corp_name="종료일만",
            decision="보류",
            subscription_end_date=date(2026, 8, 2),
        ),
        DecisionEmailItem(
            corp_name="같은날",
            decision="검증 대기",
            subscription_start_date=date(2026, 8, 3),
            subscription_end_date=date(2026, 8, 3),
        ),
        DecisionEmailItem(corp_name="미정", decision="관찰"),
    ]

    content = build_decision_email(
        "주간 IPO 판단",
        "8월 첫째 주",
        items,
        datetime(2026, 8, 1, 9, 0),
        "현재 검증 상태",
    )

    assert content.subject == "주간 IPO 판단 · 판단 4"
    assert content.text.count(INSUFFICIENT_VALIDATION) == 4
    assert content.html.count(INSUFFICIENT_VALIDATION) == 4
    assert content.text.count("확인된 긍정 근거가 없습니다.") == 4
    assert content.text.count(MISSING_RISK) == 4
    assert content.html.count(MISSING_RISK) == 4
    assert "청약 2026.08.01(토)" in content.text
    assert "청약 2026.08.02(일)" in content.text
    assert "청약 2026.08.03(월)" in content.text
    assert "점수: 정보 없음" in content.text
    assert "데이터 완성도: 정보 없음" in content.text
    assert "공모가: 정보 없음" in content.text
    assert "배정 추정:" not in content.text
    assert "검증 가치:" not in content.text
    assert "-%" not in content.text
    assert "-주" not in content.text
    assert "등록된 위험 없음" not in content.text


def test_schedule_only_email_is_sorted_and_has_clear_zero_decision_summary():
    content = build_decision_email(
        "주간 브리핑",
        "2026.07.20–2026.07.26",
        [],
        datetime(2026, 7, 19, 8, 37),
        "검증 표본 확보 중",
        schedule_items=[
            ScheduleEmailItem("B", "상장", date(2026, 7, 24)),
            ScheduleEmailItem("A", "환불", date(2026, 7, 22)),
        ],
    )

    assert content.subject == "주간 브리핑 · 일정 2"
    assert "청약 판단은 없고 주요 일정 2건이 있습니다." in content.text
    assert "청약 판단 대상은 없고 주요 일정 2건이 있습니다." in content.html
    assert content.text.index("A") < content.text.index("B")
    assert "청약 판단\n[" not in content.text


def test_empty_decision_email_has_explicit_quiet_state_for_preview_only():
    content = build_decision_email(
        "오늘 IPO 판단",
        "2026-09-01",
        [],
        datetime(2026, 9, 1, 8, 30),
        "검증 완료 종목 없음",
    )

    assert content.subject == "오늘 IPO 판단 · 새 일정 없음"
    assert "새로 확인할 IPO 일정이 없습니다." in content.text
    assert "새로 확인할 IPO 일정이 없습니다." in content.html
    assert content.text.endswith(DISCLAIMER)
    assert DISCLAIMER in content.html
