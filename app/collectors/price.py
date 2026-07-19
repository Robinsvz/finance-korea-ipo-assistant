from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Optional

import exchange_calendars as xcals
import numpy as np

from app.exceptions import ExternalSourceError
from app.services.clock import local_today
from app.services.normalization import normalize_company_name


@dataclass
class ListingPricePayload:
    ticker: str
    listing_date: date
    d0_trade_date: Optional[date] = None
    d1_trade_date: Optional[date] = None
    d3_trade_date: Optional[date] = None
    d0_open_price: Optional[float] = None
    d0_high_price: Optional[float] = None
    d0_low_price: Optional[float] = None
    d0_close_price: Optional[float] = None
    d0_volume: Optional[float] = None
    d0_turnover_value: Optional[float] = None
    d1_close_price: Optional[float] = None
    d1_volume: Optional[float] = None
    d3_close_price: Optional[float] = None
    d3_volume: Optional[float] = None
    source: str = "unknown"


@dataclass
class MarketConditionPayload:
    as_of_date: date
    kosdaq_momentum_20d: Optional[float]
    volatility_20d: Optional[float]
    source: str


class PriceCollector:
    def resolve_ticker(self, corp_name: str, as_of_date: Optional[date] = None) -> str:
        """Resolve an exact KRX company-name match without accepting fuzzy candidates."""
        if not corp_name.strip():
            raise ValueError("corp_name is required")
        errors = []
        methods = (
            (self._resolve_with_pykrx, self._resolve_with_fdr)
            if as_of_date is not None
            else (self._resolve_with_fdr, self._resolve_with_pykrx)
        )
        for method in methods:
            try:
                ticker = method(corp_name, as_of_date or local_today())
                if ticker:
                    return ticker
            except (ImportError, ExternalSourceError, ValueError, OSError, RuntimeError) as exc:
                errors.append(f"{method.__name__}={type(exc).__name__}: {exc}")
        detail = "; ".join(errors) if errors else "no exact match"
        raise ExternalSourceError(f"Ticker resolution failed for {corp_name}. {detail}")

    def collect_listing_prices(self, ticker: str, listing_date: date) -> ListingPricePayload:
        errors = []
        for method in (self._collect_with_pykrx, self._collect_with_fdr):
            try:
                return method(ticker, listing_date)
            except (ImportError, ExternalSourceError, ValueError, OSError, RuntimeError) as exc:
                errors.append(f"{method.__name__}={type(exc).__name__}: {exc}")
        raise ExternalSourceError("Price collection failed. " + "; ".join(errors))

    def collect_market_condition(self, as_of_date: Optional[date] = None) -> MarketConditionPayload:
        as_of_date = as_of_date or local_today()
        errors = []
        for method in (self._market_with_pykrx, self._market_with_fdr):
            try:
                return method(as_of_date)
            except (ImportError, ExternalSourceError, ValueError, OSError, RuntimeError) as exc:
                errors.append(f"{method.__name__}={type(exc).__name__}: {exc}")
        raise ExternalSourceError("Market condition collection failed. " + "; ".join(errors))

    def _collect_with_pykrx(self, ticker: str, listing_date: date) -> ListingPricePayload:
        from pykrx import stock  # type: ignore

        end = listing_date + timedelta(days=21)
        df = stock.get_market_ohlcv_by_date(_ymd(listing_date), _ymd(end), ticker)
        if df.empty:
            raise ExternalSourceError("pykrx returned no rows")
        return _listing_payload(df.reset_index().to_dict("records"), ticker, listing_date, "pykrx")

    def _collect_with_fdr(self, ticker: str, listing_date: date) -> ListingPricePayload:
        import FinanceDataReader as fdr  # type: ignore

        end = listing_date + timedelta(days=21)
        df = fdr.DataReader(ticker, listing_date.isoformat(), end.isoformat())
        if df.empty:
            raise ExternalSourceError("FinanceDataReader returned no rows")
        return _listing_payload(
            df.reset_index().to_dict("records"),
            ticker,
            listing_date,
            "financedatareader",
        )

    def _resolve_with_fdr(self, corp_name: str, _as_of_date: date) -> str:
        import FinanceDataReader as fdr  # type: ignore

        listing = getattr(self, "_fdr_listing_cache", None)
        if listing is None:
            listing = fdr.StockListing("KRX")
            self._fdr_listing_cache = listing
        if listing.empty or not {"Code", "Name"}.issubset(listing.columns):
            raise ExternalSourceError("FinanceDataReader returned no KRX listing rows")
        target = _listing_name(corp_name)
        matches = [
            str(row["Code"]).strip()
            for _, row in listing.iterrows()
            if _listing_name(str(row["Name"])) == target
        ]
        matches = list(dict.fromkeys(value for value in matches if value))
        if len(matches) != 1:
            raise ExternalSourceError(f"FinanceDataReader exact name match count is {len(matches)}")
        return matches[0]

    def _resolve_with_pykrx(self, corp_name: str, as_of_date: date) -> str:
        from pykrx import stock  # type: ignore

        target = _listing_name(corp_name)
        tickers = stock.get_market_ticker_list(_ymd(as_of_date), market="ALL")
        matches = [
            str(ticker)
            for ticker in tickers
            if _listing_name(stock.get_market_ticker_name(ticker)) == target
        ]
        matches = list(dict.fromkeys(value for value in matches if value))
        if len(matches) != 1:
            raise ExternalSourceError(f"pykrx exact name match count is {len(matches)}")
        return matches[0]

    def _market_with_pykrx(self, as_of_date: date) -> MarketConditionPayload:
        from pykrx import stock  # type: ignore

        start = as_of_date - timedelta(days=60)
        df = stock.get_index_ohlcv_by_date(_ymd(start), _ymd(as_of_date), "2001")
        if df.empty or "종가" not in df:
            raise ExternalSourceError("pykrx returned no KOSDAQ index rows")
        momentum, volatility = _market_statistics([float(value) for value in df["종가"].tail(21)])
        return MarketConditionPayload(as_of_date, momentum, volatility, "pykrx:KOSDAQ")

    def _market_with_fdr(self, as_of_date: date) -> MarketConditionPayload:
        import FinanceDataReader as fdr  # type: ignore

        start = as_of_date - timedelta(days=60)
        df = fdr.DataReader("KQ11", start.isoformat(), as_of_date.isoformat())
        if df.empty or "Close" not in df:
            raise ExternalSourceError("FinanceDataReader returned no KOSDAQ index rows")
        momentum, volatility = _market_statistics([float(value) for value in df["Close"].tail(21)])
        return MarketConditionPayload(as_of_date, momentum, volatility, "financedatareader:KQ11")


def _listing_payload(
    rows: list[dict[str, Any]],
    ticker: str,
    listing_date: date,
    source: str,
) -> ListingPricePayload:
    if not rows:
        raise ExternalSourceError(f"{source} returned no listing price rows")
    d0 = rows[0]
    d1 = rows[1] if len(rows) > 1 else {}
    d3 = rows[3] if len(rows) > 3 else {}
    d0_date = _row_date(d0)
    expected_d0 = _expected_first_trade_date(listing_date)
    if d0_date != expected_d0:
        raise ExternalSourceError(
            f"{source} first trade date {d0_date} does not match expected KRX session {expected_d0}"
        )
    korean = source == "pykrx"
    return ListingPricePayload(
        ticker=ticker,
        listing_date=listing_date,
        d0_trade_date=d0_date,
        d1_trade_date=_row_date(d1),
        d3_trade_date=_row_date(d3),
        d0_open_price=_num(d0.get("시가" if korean else "Open")),
        d0_high_price=_num(d0.get("고가" if korean else "High")),
        d0_low_price=_num(d0.get("저가" if korean else "Low")),
        d0_close_price=_num(d0.get("종가" if korean else "Close")),
        d0_volume=_num(d0.get("거래량" if korean else "Volume")),
        d0_turnover_value=_num(d0.get("거래대금")) if korean else None,
        d1_close_price=_num(d1.get("종가" if korean else "Close")),
        d1_volume=_num(d1.get("거래량" if korean else "Volume")),
        d3_close_price=_num(d3.get("종가" if korean else "Close")),
        d3_volume=_num(d3.get("거래량" if korean else "Volume")),
        source=source,
    )


def _market_statistics(closes: list[float]) -> tuple[Optional[float], Optional[float]]:
    values = [value for value in closes if value > 0]
    if len(values) < 2:
        return None, None
    momentum = values[-1] / values[0] - 1
    daily_returns = np.diff(np.log(np.asarray(values, dtype=float)))
    volatility = (
        float(np.std(daily_returns, ddof=1) * np.sqrt(252)) if len(daily_returns) > 1 else 0.0
    )
    return float(momentum), volatility


def _expected_first_trade_date(listing_date: date) -> date:
    calendar = xcals.get_calendar("XKRX")
    end = min(listing_date + timedelta(days=10), calendar.last_session.date())
    sessions = calendar.sessions_in_range(listing_date, end)
    if len(sessions) == 0:
        raise ExternalSourceError("XKRX calendar does not cover the expected listing session")
    return sessions[0].date()


def _row_date(row: dict[str, Any]) -> Optional[date]:
    for key in ("날짜", "Date", "date", "index"):
        value = row.get(key)
        if value is None:
            continue
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        try:
            return datetime.fromisoformat(str(value).split(" ")[0]).date()
        except ValueError:
            continue
    return None


def _ymd(value: date) -> str:
    return value.strftime("%Y%m%d")


def _listing_name(value: str) -> str:
    normalized = normalize_company_name(value)
    legal_suffix = "주식회사"
    if normalized.startswith(legal_suffix):
        normalized = normalized[len(legal_suffix) :]
    if normalized.endswith(legal_suffix):
        normalized = normalized[: -len(legal_suffix)]
    return normalized


def _num(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if np.isfinite(numeric) else None
