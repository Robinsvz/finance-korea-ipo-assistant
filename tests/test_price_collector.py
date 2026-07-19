from datetime import date, datetime

import pandas as pd
import pytest

from app.collectors.price import (
    ListingPricePayload,
    MarketConditionPayload,
    PriceCollector,
    _expected_first_trade_date,
    _listing_name,
    _listing_payload,
    _market_statistics,
    _num,
    _row_date,
)
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


@pytest.mark.parametrize("row_date", [date(2026, 7, 19), date(2026, 7, 21), None])
def test_listing_payload_requires_expected_first_krx_session(row_date):
    with pytest.raises(ExternalSourceError, match="does not match expected"):
        _listing_payload([{"Date": row_date}], "1", date(2026, 7, 20), "fdr")


def test_market_statistics_calculates_momentum():
    momentum, volatility = _market_statistics([100, 110, 121])
    assert momentum == pytest.approx(0.21)
    assert volatility is not None and volatility >= 0


def test_market_statistics_handles_missing_values():
    assert _market_statistics([0, 100]) == (None, None)


def test_row_date_and_non_finite_number():
    assert _row_date({"Date": "2026-07-20 00:00:00"}) == date(2026, 7, 20)
    assert _num(float("nan")) is None


def test_ticker_resolution_uses_exact_normalized_fdr_name(monkeypatch):
    import FinanceDataReader as fdr

    monkeypatch.setattr(
        fdr,
        "StockListing",
        lambda _market: pd.DataFrame(
            [{"Code": "0123A0", "Name": "테스트 주식회사"}, {"Code": "999999", "Name": "다른회사"}]
        ),
    )

    collector = PriceCollector()
    monkeypatch.setattr(
        collector,
        "_resolve_with_pykrx",
        lambda *_args: (_ for _ in ()).throw(ExternalSourceError("historical unavailable")),
    )
    assert collector.resolve_ticker("(주)테스트", date(2026, 7, 20)) == "0123A0"


def test_ticker_resolution_falls_back_and_rejects_empty(monkeypatch):
    collector = PriceCollector()
    monkeypatch.setattr(
        collector,
        "_resolve_with_fdr",
        lambda *_args: (_ for _ in ()).throw(ExternalSourceError("unavailable")),
    )
    monkeypatch.setattr(collector, "_resolve_with_pykrx", lambda *_args: "123456")
    assert collector.resolve_ticker("테스트") == "123456"
    with pytest.raises(ValueError, match="corp_name"):
        collector.resolve_ticker("  ")


def test_ticker_resolution_reports_all_source_failures(monkeypatch):
    collector = PriceCollector()

    def failure(*_args):
        raise ExternalSourceError("no match")

    monkeypatch.setattr(collector, "_resolve_with_fdr", failure)
    monkeypatch.setattr(collector, "_resolve_with_pykrx", failure)
    with pytest.raises(ExternalSourceError, match="Ticker resolution failed"):
        collector.resolve_ticker("없는회사")


def test_pykrx_ticker_resolution_requires_one_exact_match(monkeypatch):
    import pykrx.stock as stock

    monkeypatch.setattr(stock, "get_market_ticker_list", lambda *_args, **_kwargs: ["1", "2"])
    monkeypatch.setattr(
        stock,
        "get_market_ticker_name",
        lambda ticker: {"1": "테스트", "2": "다른회사"}[ticker],
    )
    assert PriceCollector()._resolve_with_pykrx("테스트", date(2026, 7, 20)) == "1"

    monkeypatch.setattr(stock, "get_market_ticker_name", lambda _ticker: "테스트")
    with pytest.raises(ExternalSourceError, match="match count"):
        PriceCollector()._resolve_with_pykrx("테스트", date(2026, 7, 20))


def _listing_result(source="test"):
    return ListingPricePayload(ticker="1", listing_date=date(2026, 7, 20), source=source)


def _market_result(source="test"):
    return MarketConditionPayload(date(2026, 7, 20), 0.1, 0.2, source)


def test_ticker_resolution_handles_methods_returning_no_match(monkeypatch):
    collector = PriceCollector()
    monkeypatch.setattr(collector, "_resolve_with_fdr", lambda *_args: "")
    monkeypatch.setattr(collector, "_resolve_with_pykrx", lambda *_args: "")
    with pytest.raises(ExternalSourceError, match="no exact match"):
        collector.resolve_ticker("없는회사")


def test_listing_collection_first_fallback_and_all_failures(monkeypatch):
    collector = PriceCollector()
    monkeypatch.setattr(collector, "_collect_with_pykrx", lambda *_args: _listing_result("p"))
    assert collector.collect_listing_prices("1", date(2026, 7, 20)).source == "p"

    monkeypatch.setattr(
        collector,
        "_collect_with_pykrx",
        lambda *_args: (_ for _ in ()).throw(ExternalSourceError("no")),
    )
    monkeypatch.setattr(collector, "_collect_with_fdr", lambda *_args: _listing_result("f"))
    assert collector.collect_listing_prices("1", date(2026, 7, 20)).source == "f"

    monkeypatch.setattr(
        collector,
        "_collect_with_fdr",
        lambda *_args: (_ for _ in ()).throw(ValueError("bad")),
    )
    with pytest.raises(ExternalSourceError, match="Price collection failed") as exc:
        collector.collect_listing_prices("1", date(2026, 7, 20))
    assert "ExternalSourceError" in str(exc.value)
    assert "ValueError" in str(exc.value)


def test_market_collection_first_fallback_and_all_failures(monkeypatch):
    collector = PriceCollector()
    monkeypatch.setattr(collector, "_market_with_pykrx", lambda *_args: _market_result("p"))
    assert collector.collect_market_condition(date(2026, 7, 20)).source == "p"

    monkeypatch.setattr(
        collector,
        "_market_with_pykrx",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("no")),
    )
    monkeypatch.setattr(collector, "_market_with_fdr", lambda *_args: _market_result("f"))
    assert collector.collect_market_condition().source == "f"

    monkeypatch.setattr(
        collector,
        "_market_with_fdr",
        lambda *_args: (_ for _ in ()).throw(OSError("bad")),
    )
    with pytest.raises(ExternalSourceError, match="Market condition collection failed"):
        collector.collect_market_condition(date(2026, 7, 20))


def test_provider_listing_collectors_success_and_empty(monkeypatch):
    import FinanceDataReader as fdr
    import pykrx.stock as stock

    listing_date = date(2026, 7, 20)
    pykrx_frame = pd.DataFrame(
        [{"시가": 100, "고가": 110, "저가": 90, "종가": 105, "거래량": 5, "거래대금": 500}],
        index=pd.DatetimeIndex([listing_date]),
    )
    monkeypatch.setattr(stock, "get_market_ohlcv_by_date", lambda *_args: pykrx_frame)
    result = PriceCollector()._collect_with_pykrx("1", listing_date)
    assert result.source == "pykrx"
    assert result.d0_turnover_value == 500

    monkeypatch.setattr(stock, "get_market_ohlcv_by_date", lambda *_args: pd.DataFrame())
    with pytest.raises(ExternalSourceError, match="pykrx returned no rows"):
        PriceCollector()._collect_with_pykrx("1", listing_date)

    fdr_frame = pd.DataFrame(
        [{"Open": 100, "High": 110, "Low": 90, "Close": 105, "Volume": 5}],
        index=pd.DatetimeIndex([listing_date]),
    )
    monkeypatch.setattr(fdr, "DataReader", lambda *_args: fdr_frame)
    result = PriceCollector()._collect_with_fdr("1", listing_date)
    assert result.source == "financedatareader"
    assert result.d0_turnover_value is None

    monkeypatch.setattr(fdr, "DataReader", lambda *_args: pd.DataFrame())
    with pytest.raises(ExternalSourceError, match="FinanceDataReader returned no rows"):
        PriceCollector()._collect_with_fdr("1", listing_date)


def test_fdr_resolution_uses_cache_and_validates_listing(monkeypatch):
    import FinanceDataReader as fdr

    calls = []
    listing = pd.DataFrame([{"Code": "1", "Name": "테스트"}])

    def stock_listing(_market):
        calls.append(1)
        return listing

    monkeypatch.setattr(fdr, "StockListing", stock_listing)
    collector = PriceCollector()
    assert collector._resolve_with_fdr("테스트", date(2026, 7, 20)) == "1"
    assert collector._resolve_with_fdr("테스트", date(2026, 7, 21)) == "1"
    assert len(calls) == 1

    collector = PriceCollector()
    monkeypatch.setattr(fdr, "StockListing", lambda _market: pd.DataFrame())
    with pytest.raises(ExternalSourceError, match="no KRX listing"):
        collector._resolve_with_fdr("테스트", date(2026, 7, 20))

    monkeypatch.setattr(fdr, "StockListing", lambda _market: pd.DataFrame([{"Other": "x"}]))
    with pytest.raises(ExternalSourceError, match="no KRX listing"):
        PriceCollector()._resolve_with_fdr("테스트", date(2026, 7, 20))

    duplicated = pd.DataFrame([{"Code": "1", "Name": "테스트"}, {"Code": "2", "Name": "테스트"}])
    monkeypatch.setattr(fdr, "StockListing", lambda _market: duplicated)
    with pytest.raises(ExternalSourceError, match="match count is 2"):
        PriceCollector()._resolve_with_fdr("테스트", date(2026, 7, 20))

    blank = pd.DataFrame([{"Code": "", "Name": "테스트"}])
    monkeypatch.setattr(fdr, "StockListing", lambda _market: blank)
    with pytest.raises(ExternalSourceError, match="match count is 0"):
        PriceCollector()._resolve_with_fdr("테스트", date(2026, 7, 20))


def test_provider_market_collectors_success_empty_and_missing_column(monkeypatch):
    import FinanceDataReader as fdr
    import pykrx.stock as stock

    closes = pd.DataFrame({"종가": [100, 101, 102]})
    monkeypatch.setattr(stock, "get_index_ohlcv_by_date", lambda *_args: closes)
    result = PriceCollector()._market_with_pykrx(date(2026, 7, 20))
    assert result.source == "pykrx:KOSDAQ"
    assert result.kosdaq_momentum_20d == pytest.approx(0.02)

    monkeypatch.setattr(stock, "get_index_ohlcv_by_date", lambda *_args: pd.DataFrame())
    with pytest.raises(ExternalSourceError, match="no KOSDAQ"):
        PriceCollector()._market_with_pykrx(date(2026, 7, 20))
    monkeypatch.setattr(
        stock, "get_index_ohlcv_by_date", lambda *_args: pd.DataFrame({"다른열": [1]})
    )
    with pytest.raises(ExternalSourceError, match="no KOSDAQ"):
        PriceCollector()._market_with_pykrx(date(2026, 7, 20))

    monkeypatch.setattr(fdr, "DataReader", lambda *_args: pd.DataFrame({"Close": [100, 99]}))
    result = PriceCollector()._market_with_fdr(date(2026, 7, 20))
    assert result.source == "financedatareader:KQ11"

    monkeypatch.setattr(fdr, "DataReader", lambda *_args: pd.DataFrame())
    with pytest.raises(ExternalSourceError, match="no KOSDAQ"):
        PriceCollector()._market_with_fdr(date(2026, 7, 20))
    monkeypatch.setattr(fdr, "DataReader", lambda *_args: pd.DataFrame({"Other": [1]}))
    with pytest.raises(ExternalSourceError, match="no KOSDAQ"):
        PriceCollector()._market_with_fdr(date(2026, 7, 20))


def test_listing_payload_empty_short_fdr_rows_and_numeric_helpers():
    with pytest.raises(ExternalSourceError, match="no listing price rows"):
        _listing_payload([], "1", date(2026, 7, 20), "fdr")

    result = _listing_payload(
        [{"Date": date(2026, 7, 20), "Open": "100", "Close": "bad"}],
        "1",
        date(2026, 7, 20),
        "fdr",
    )
    assert result.d1_trade_date is None
    assert result.d3_trade_date is None
    assert result.d0_open_price == 100
    assert result.d0_close_price is None
    assert _market_statistics([100, 110])[1] == 0
    assert _num(None) is None
    assert _num("bad") is None
    assert _num(float("inf")) is None
    assert _num("1.5") == 1.5


def test_expected_session_row_date_and_listing_name_edge_cases(monkeypatch):
    class EmptyCalendar:
        last_session = pd.Timestamp("2026-12-31")

        def sessions_in_range(self, _start, _end):
            return pd.DatetimeIndex([])

    monkeypatch.setattr("app.collectors.price.xcals.get_calendar", lambda _name: EmptyCalendar())
    with pytest.raises(ExternalSourceError, match="does not cover"):
        _expected_first_trade_date(date(2026, 7, 20))

    assert _row_date({"날짜": datetime(2026, 7, 20)}) == date(2026, 7, 20)
    assert _row_date({"날짜": date(2026, 7, 20)}) == date(2026, 7, 20)
    assert _row_date({"날짜": "invalid", "Date": "2026-07-21"}) == date(2026, 7, 21)
    assert _row_date({"날짜": "invalid"}) is None
    assert _row_date({}) is None
    assert _listing_name("주식회사 테스트") == "테스트"
    assert _listing_name("테스트 주식회사") == "테스트"
    monkeypatch.setattr(
        "app.collectors.price.normalize_company_name", lambda _value: "주식회사테스트"
    )
    assert _listing_name("ignored") == "테스트"
    monkeypatch.setattr(
        "app.collectors.price.normalize_company_name", lambda _value: "테스트주식회사"
    )
    assert _listing_name("ignored") == "테스트"
