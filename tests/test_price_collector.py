from datetime import date, datetime

import pytest

from app.collectors.price import _listing_payload, _market_statistics, _num, _row_date
from app.exceptions import ExternalSourceError


def test_listing_payload_records_real_trade_dates():
    rows = [
        {
            "날짜": datetime(2026, 7, 20),
            "시가": 100,
            "고가": 120,
            "저가": 90,
            "종가": 110,
            "거래량": 10,
        },
        {"날짜": datetime(2026, 7, 21), "종가": 115, "거래량": 11},
        {"날짜": datetime(2026, 7, 22), "종가": 117, "거래량": 12},
        {"날짜": datetime(2026, 7, 23), "종가": 120, "거래량": 13},
    ]
    result = _listing_payload(rows, "123456", date(2026, 7, 20), "pykrx")
    assert result.d0_trade_date == date(2026, 7, 20)
    assert result.d1_trade_date == date(2026, 7, 21)
    assert result.d3_trade_date == date(2026, 7, 23)
    assert result.d3_close_price == 120


def test_listing_payload_rejects_pre_listing_row():
    with pytest.raises(ExternalSourceError, match="before the listing"):
        _listing_payload([{"Date": date(2026, 7, 19)}], "1", date(2026, 7, 20), "fdr")


def test_market_statistics_calculates_momentum():
    momentum, volatility = _market_statistics([100, 110, 121])
    assert momentum == pytest.approx(0.21)
    assert volatility is not None and volatility >= 0


def test_market_statistics_handles_missing_values():
    assert _market_statistics([0, 100]) == (None, None)


def test_row_date_and_non_finite_number():
    assert _row_date({"Date": "2026-07-20 00:00:00"}) == date(2026, 7, 20)
    assert _num(float("nan")) is None
