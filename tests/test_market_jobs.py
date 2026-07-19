from contextlib import contextmanager
from datetime import date, datetime, timezone
from types import SimpleNamespace

from app.models import Ipo, MarketConditionSnapshot, OutcomeEvaluation
from app.services import jobs


def _ipo(name, listing_date, *, sample=False):
    return Ipo(
        identity_key=f"test:{name}",
        corp_name=name,
        normalized_name=name,
        listing_date=listing_date,
        actual_listing_date=listing_date,
        is_sample=sample,
        data_origin="synthetic" if sample else "kind",
    )


def test_market_outcomes_are_point_in_time_real_and_provenanced(db_session, monkeypatch):
    as_of = date(2026, 7, 20)
    eligible = _ipo("eligible", date(2026, 7, 10))
    other = _ipo("other", date(2026, 7, 11))
    future = _ipo("future", date(2026, 7, 21))
    sample = _ipo("sample", date(2026, 7, 10), sample=True)
    late = _ipo("late-evaluation", date(2026, 7, 10))
    db_session.add_all((eligible, other, future, sample, late))
    db_session.flush()
    db_session.add_all(
        (
            OutcomeEvaluation(
                ipo_id=eligible.id,
                evaluation_version="older-complete",
                d1_close_return_pct=-100,
                evaluated_at=datetime(2026, 7, 20, 13, tzinfo=timezone.utc),
            ),
            OutcomeEvaluation(
                ipo_id=eligible.id,
                evaluation_version="latest-complete",
                d1_close_return_pct=10,
                evaluated_at=datetime(2026, 7, 20, 14, tzinfo=timezone.utc),
            ),
            OutcomeEvaluation(
                ipo_id=eligible.id,
                evaluation_version="newer-incomplete",
                d1_close_return_pct=None,
                evaluated_at=datetime(2026, 7, 20, 14, 30, tzinfo=timezone.utc),
            ),
            OutcomeEvaluation(
                ipo_id=other.id,
                evaluation_version="test",
                d1_close_return_pct=-5,
                evaluated_at=datetime(2026, 7, 20, 14, 15, tzinfo=timezone.utc),
            ),
            OutcomeEvaluation(
                ipo_id=future.id,
                evaluation_version="test",
                d1_close_return_pct=-100,
                evaluated_at=datetime(2026, 7, 20, 14, tzinfo=timezone.utc),
            ),
            OutcomeEvaluation(
                ipo_id=sample.id,
                evaluation_version="test",
                d1_close_return_pct=-100,
                evaluated_at=datetime(2026, 7, 20, 14, tzinfo=timezone.utc),
            ),
            OutcomeEvaluation(
                ipo_id=late.id,
                evaluation_version="test",
                d1_close_return_pct=-100,
                evaluated_at=datetime(2026, 7, 20, 15, tzinfo=timezone.utc),
            ),
        )
    )
    db_session.flush()

    @contextmanager
    def scope():
        yield db_session
        db_session.flush()

    class UnavailableCollector:
        def collect_market_condition(self, _as_of):
            raise RuntimeError("market source unavailable")

    monkeypatch.setattr(jobs, "init_db", lambda: None)
    monkeypatch.setattr(jobs, "session_scope", scope)
    monkeypatch.setattr(jobs, "PriceCollector", UnavailableCollector)
    monkeypatch.setattr(jobs, "get_settings", lambda: SimpleNamespace(timezone="Asia/Seoul"))

    result = jobs.collect_market_condition(as_of)

    row = db_session.query(MarketConditionSnapshot).one()
    assert result["updated"] is True
    assert result["recent_ipo_success_rate"] == 0.5
    assert result["warning"] == "RuntimeError: market source unavailable"
    assert row.source == "outcomes-only"


def test_market_success_rate_limits_after_canonicalizing_twenty_ipos(db_session, monkeypatch):
    as_of = date(2026, 7, 20)
    ipos = [_ipo(f"ipo-{index:02d}", date(2026, 7, 1)) for index in range(21)]
    db_session.add_all(ipos)
    db_session.flush()
    base_hour = 10
    for index, ipo in enumerate(ipos):
        db_session.add(
            OutcomeEvaluation(
                ipo_id=ipo.id,
                evaluation_version="latest",
                d1_close_return_pct=-1 if index == 0 else 1,
                evaluated_at=datetime(
                    2026,
                    7,
                    20,
                    base_hour,
                    index,
                    tzinfo=timezone.utc,
                ),
            )
        )
    # This older revision for the newest IPO would consume one of a row-level
    # LIMIT 20 and incorrectly reduce the rate if revisions were not canonicalized first.
    db_session.add(
        OutcomeEvaluation(
            ipo_id=ipos[-1].id,
            evaluation_version="older",
            d1_close_return_pct=-1,
            evaluated_at=datetime(2026, 7, 20, 10, 19, 30, tzinfo=timezone.utc),
        )
    )
    db_session.flush()

    @contextmanager
    def scope():
        yield db_session
        db_session.flush()

    class UnavailableCollector:
        def collect_market_condition(self, _as_of):
            raise RuntimeError("market source unavailable")

    monkeypatch.setattr(jobs, "init_db", lambda: None)
    monkeypatch.setattr(jobs, "session_scope", scope)
    monkeypatch.setattr(jobs, "PriceCollector", UnavailableCollector)
    monkeypatch.setattr(jobs, "get_settings", lambda: SimpleNamespace(timezone="Asia/Seoul"))

    result = jobs.collect_market_condition(as_of)

    assert result["recent_ipo_success_rate"] == 1.0
