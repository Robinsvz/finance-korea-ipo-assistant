from contextlib import contextmanager
from datetime import date

import pytest

from app.collectors.price import ListingPricePayload
from app.models import Ipo, ListingPrice, OutcomeEvaluation
from app.services import jobs


def _ipo(name, listing_date, *, final_price=None, band_price=None, sample=False):
    return Ipo(
        identity_key=f"test:{name}",
        corp_name=name,
        normalized_name=name,
        listing_date=listing_date,
        offer_price_final=final_price,
        offer_price_max=band_price,
        is_sample=sample,
        data_origin="synthetic" if sample else "kind",
        status="listed",
    )


def test_history_backfill_excludes_samples_uses_listing_date_and_requires_final_price(
    db_session, monkeypatch
):
    complete = _ipo("complete", date(2026, 7, 10), final_price=10_000)
    band_only = _ipo("band-only", date(2026, 7, 13), band_price=10_000)
    sample = _ipo("sample", date(2026, 7, 14), final_price=10_000, sample=True)
    db_session.add_all((complete, band_only, sample))
    db_session.flush()

    @contextmanager
    def scope():
        yield db_session
        db_session.flush()

    resolved = []

    class Collector:
        def resolve_ticker(self, name, as_of_date):
            resolved.append((name, as_of_date))
            return {"complete": "000001", "band-only": "000002"}[name]

        def collect_listing_prices(self, ticker, listing_date):
            return ListingPricePayload(
                ticker=ticker,
                listing_date=listing_date,
                d0_trade_date=listing_date,
                d1_trade_date=listing_date,
                d0_open_price=12_000,
                d0_close_price=11_500,
                d1_close_price=11_000,
                source="test",
            )

    monkeypatch.setattr(jobs, "init_db", lambda: None)
    monkeypatch.setattr(jobs, "session_scope", scope)
    monkeypatch.setattr(jobs, "PriceCollector", Collector)
    monkeypatch.setattr(jobs, "local_today", lambda: date(2026, 7, 19))

    result = jobs.backfill_historical_outcomes(date(2026, 7, 1), date(2026, 7, 19))

    assert result["processed"] == 2
    assert result["resolved"] == 2
    assert result["prices"] == 2
    assert result["evaluated"] == 1
    assert result["skipped"] == 1
    assert result["failed"] == 0
    assert resolved == [
        ("complete", date(2026, 7, 10)),
        ("band-only", date(2026, 7, 13)),
    ]
    assert db_session.query(ListingPrice).count() == 2
    assert db_session.query(OutcomeEvaluation).count() == 1
    assert "final offer price missing" in result["results"][1]["reason"]


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        (date(2026, 7, 20), date(2026, 7, 19), "after end"),
        (date(2026, 7, 1), date(2026, 7, 20), "future"),
        (date(2015, 12, 1), date(2026, 1, 2), "10 years"),
    ],
)
def test_history_backfill_rejects_unsafe_ranges(monkeypatch, start, end, message):
    monkeypatch.setattr(jobs, "local_today", lambda: date(2026, 7, 19))
    with pytest.raises(ValueError, match=message):
        jobs.backfill_historical_outcomes(start, end)
