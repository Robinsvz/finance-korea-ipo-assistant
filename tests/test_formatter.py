from datetime import date

from app.models import FinancialMetric, Ipo
from app.services.formatter import format_financial, format_schedule


def test_financial_formatter_explains_prior_zero_yoy():
    ipo = Ipo(corp_name="재무기업", normalized_name="재무기업")
    metric = FinancialMetric(
        fiscal_year=2025,
        revenue=100,
        revenue_yoy_growth=None,
        revenue_yoy_status="prior_zero",
    )
    assert "전년도 매출 0으로 산정 불가" in format_financial(ipo, metric)


def test_schedule_includes_refund_and_listing_events():
    ipo = Ipo(
        corp_name="일정기업",
        normalized_name="일정기업",
        refund_date=date(2026, 7, 20),
        listing_date=date(2026, 7, 24),
    )
    text = format_schedule("이번 주", [ipo])
    assert "2026-07-20 · 환불" in text
    assert "2026-07-24 · 상장" in text


def test_today_schedule_excludes_other_dates_for_same_ipo():
    ipo = Ipo(
        corp_name="오늘기업",
        normalized_name="오늘기업",
        refund_date=date(2026, 7, 20),
        listing_date=date(2026, 7, 24),
    )
    text = format_schedule("오늘", [ipo], date(2026, 7, 20), date(2026, 7, 20))
    assert "환불" in text
    assert "상장" not in text
