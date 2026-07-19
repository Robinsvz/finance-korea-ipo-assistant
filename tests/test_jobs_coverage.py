from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.exceptions import MissingCredentialError
from app.models import (
    Ipo,
    IpoFactObservation,
    IpoScheduleRevision,
    IpoStructureMetric,
    MarketConditionSnapshot,
    OutcomeEvaluation,
    SourceDocument,
    SourceSnapshot,
    UnderwriterAllocation,
    ValuationMetric,
)
from app.schemas import IpoScheduleRecord
from app.services import jobs


def _patch_scope(monkeypatch, db_session):
    @contextmanager
    def scope():
        yield db_session
        db_session.flush()

    monkeypatch.setattr(jobs, "init_db", lambda: None)
    monkeypatch.setattr(jobs, "session_scope", scope)
    return scope


def _ipo(name="테스트", **kwargs):
    values = {
        "identity_key": f"test:{name}",
        "corp_name": name,
        "normalized_name": name,
        "is_sample": False,
        "data_origin": "test",
        "status": "scheduled",
    }
    values.update(kwargs)
    return Ipo(**values)


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _Connection:
    def __init__(self, revision=None, *, revision_error=False):
        self.revision = revision
        self.revision_error = revision_error
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, _statement):
        self.calls += 1
        if self.calls == 2 and self.revision_error:
            raise RuntimeError("version table missing")
        return _ScalarResult(self.revision if self.calls == 2 else None)


class _Engine:
    def __init__(self, connection=None, *, error=False):
        self.connection = connection
        self.error = error

    def connect(self):
        if self.error:
            raise RuntimeError("database down")
        return self.connection


def _settings(**kwargs):
    values = {
        "project_root": Path("/tmp/project"),
        "telegram_configured": True,
        "email_configured": True,
        "dart_configured": True,
        "naver_configured": True,
        "database_url": "postgresql://user:secret@db.example/ipo",
        "timezone": "Asia/Seoul",
        "default_user_cash": 10_000_000,
    }
    values.update(kwargs)
    return SimpleNamespace(**values)


def test_healthcheck_covers_schema_database_and_redaction_states(monkeypatch):
    from alembic.script import ScriptDirectory

    monkeypatch.setattr(jobs, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        ScriptDirectory,
        "from_config",
        staticmethod(lambda _config: SimpleNamespace(get_current_head=lambda: "head")),
    )
    monkeypatch.setattr(jobs, "engine", _Engine(_Connection("head")))
    healthy = jobs.healthcheck()
    assert healthy.database_ok and healthy.schema_current
    assert healthy.database_url == "postgresql://user:***@db.example/ipo"

    monkeypatch.setattr(
        ScriptDirectory,
        "from_config",
        staticmethod(lambda _config: (_ for _ in ()).throw(RuntimeError("bad config"))),
    )
    monkeypatch.setattr(jobs, "engine", _Engine(_Connection(revision_error=True)))
    missing_schema = jobs.healthcheck()
    assert missing_schema.database_ok is True
    assert missing_schema.schema_current is False
    assert missing_schema.schema_revision is None
    assert missing_schema.expected_schema_revision is None

    monkeypatch.setattr(jobs, "engine", _Engine(error=True))
    unavailable = jobs.healthcheck()
    assert unavailable.database_ok is False
    assert jobs._safe_database_url("sqlite:///data/ipo.db") == "sqlite:///data/ipo.db"


def test_basic_schedule_scoring_and_diligence_jobs(db_session, monkeypatch):
    _patch_scope(monkeypatch, db_session)
    first = _ipo("첫째", subscription_start_date=None)
    second = _ipo("둘째", subscription_start_date=date(2026, 7, 19))
    db_session.add_all((first, second))
    db_session.flush()

    refreshed = []
    monkeypatch.setattr(
        jobs,
        "refresh_ipo_statuses",
        lambda session, today=None: refreshed.append((session, today)) or {"updated": 2},
    )
    assert jobs.refresh_statuses(date(2026, 7, 19)) == {"updated": 2}

    records = [SimpleNamespace(name="r1"), SimpleNamespace(name="r2")]
    monkeypatch.setattr(
        jobs,
        "KindScheduleCollector",
        lambda: SimpleNamespace(collect_with_raw=lambda **_kwargs: (records, "raw")),
    )
    monkeypatch.setattr(
        jobs,
        "save_source_snapshot",
        lambda *_args, **_kwargs: SimpleNamespace(raw_text_hash="hash"),
    )
    monkeypatch.setattr(
        jobs,
        "upsert_schedule_record",
        lambda _session, record: SimpleNamespace(id=1 if record is records[0] else 2),
    )
    monkeypatch.setattr(jobs, "_persist_kind_record_evidence", lambda *_args: None)
    schedule = jobs.collect_kind_schedule(date(2026, 7, 1), date(2026, 7, 31))
    assert schedule == {"records": 2, "ipos": 2, "snapshot_hash": "hash"}
    assert jobs.collect_kind_schedule()["records"] == 2

    listing_records = [
        SimpleNamespace(
            corp_name="신규기업",
            listing_date=date(2026, 7, 13),
            listing_type="신규상장",
            market="코스닥",
            isur_cd="12345",
            bz_procs_no="20260101000001",
        )
    ]
    monkeypatch.setattr(
        jobs,
        "KindListingCollector",
        lambda: SimpleNamespace(
            collect_with_raw=lambda _start, _end: (listing_records, "listing raw")
        ),
    )
    listing = jobs.collect_kind_listings_for_current_month(date(2026, 7, 19))
    assert listing == {
        "month_start": "2026-07-01",
        "month_end": "2026-07-31",
        "records": 1,
        "names": ["신규기업"],
        "snapshot_hash": "hash",
    }

    monkeypatch.setattr(jobs, "list_active_ipos", lambda _session: [first, second])
    monkeypatch.setattr(jobs, "_before_subscription_cutoff", lambda ipo: ipo is first)
    scored = []

    class Scorer:
        def score_and_save(self, _session, ipo):
            scored.append(ipo.corp_name)
            return SimpleNamespace(
                total_score=88,
                recommendation_band="Strong",
                confidence_score=0.9,
                model_version="v3",
            )

    monkeypatch.setattr(jobs, "IpoScoringService", Scorer)
    assert jobs.score_all_active() == {"processed": 2, "scored": 1, "skipped": 1}
    one = jobs.score_one(first.id)
    assert one == {
        "ipo": "첫째",
        "score": 88,
        "band": "Strong",
        "confidence": 0.9,
        "model_version": "v3",
    }

    monkeypatch.setattr(
        jobs,
        "build_diligence_report",
        lambda _session, ipos, criteria: {
            "names": [ipo.corp_name for ipo in ipos],
            "min": criteria.min_news_articles,
            "age": criteria.max_age_days,
        },
    )
    assert jobs.review_diligence(first.id, 2, 7)["names"] == ["첫째"]
    assert jobs.review_diligence()["names"] == ["첫째", "둘째"]

    with pytest.raises(ValueError, match="review_type"):
        jobs.mark_diligence_review("첫째", "bad", "reviewer")
    with pytest.raises(ValueError, match="reviewed_by"):
        jobs.mark_diligence_review("첫째", "articles", " ")
    monkeypatch.setattr(
        jobs,
        "upsert_diligence_review",
        lambda *_args, **kwargs: SimpleNamespace(
            review_type=kwargs["review_type"], review_status=kwargs["values"]["review_status"]
        ),
    )
    marked = jobs.mark_diligence_review("첫째", "articles", " reviewer ")
    assert marked == {"ipo": "첫째", "review_type": "articles", "status": "reviewed"}
    jobs.mark_diligence_review("첫째", "disclosure", "reviewer", evidence_refs=["ref"])


def test_kind_record_evidence_links_core_facts_and_handles_missing_dates(db_session):
    observed_at = datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc)
    snapshot = SourceSnapshot(
        source="kind",
        source_key="2026-07",
        fetched_at=observed_at,
        raw_text_hash="a" * 64,
        raw_text="official raw",
    )
    ipo = _ipo("근거사", data_origin="kind")
    ipo.offering_id = "kind:offer-1"
    db_session.add_all((snapshot, ipo))
    db_session.flush()
    record = IpoScheduleRecord(
        corp_name="근거사",
        market="코스닥",
        kind_biz_no="biz-1",
        report_filed_date=date(2026, 6, 1),
        demand_start_date=date(2026, 6, 10),
        demand_end_date=date(2026, 6, 12),
        subscription_start_date=date(2026, 6, 15),
        subscription_end_date=date(2026, 6, 16),
        payment_date=date(2026, 6, 18),
        listing_date=date(2026, 7, 1),
        offer_price_final=10_000,
        offer_amount_million=20_000,
        underwriters=["KB증권", "IBK투자증권"],
        raw={"row": 1},
    )

    jobs._persist_kind_record_evidence(db_session, ipo, record, snapshot)
    jobs._persist_kind_record_evidence(db_session, ipo, record, snapshot)
    db_session.flush()

    documents = db_session.scalars(
        select(SourceDocument).where(SourceDocument.ipo_id == ipo.id)
    ).all()
    observations = db_session.scalars(
        select(IpoFactObservation).where(IpoFactObservation.ipo_id == ipo.id)
    ).all()
    revisions = db_session.scalars(
        select(IpoScheduleRevision).where(IpoScheduleRevision.ipo_id == ipo.id)
    ).all()
    assert len(documents) == 1
    assert len(observations) == 10
    assert len(revisions) == 1
    assert {row.field_name for row in observations} >= {
        "demand_period",
        "subscription_period",
        "offer_price_final",
        "underwriter_names",
    }
    assert documents[0].official_url.startswith("https://kind.krx.co.kr/")

    assert jobs._kind_report_datetime(None, observed_at) is observed_at
    assert jobs._kind_date(None) is None
    assert jobs._kind_period(None, None) is None
    assert jobs._kind_period(date(2026, 7, 1), None) == "2026-07-01"
    assert jobs._kind_period(None, date(2026, 7, 2)) == "2026-07-02"
    assert jobs._kind_period(date(2026, 7, 3), date(2026, 7, 3)) == "2026-07-03"


def test_dart_collection_all_states(monkeypatch):
    @contextmanager
    def scope():
        yield object()

    monkeypatch.setattr(jobs, "init_db", lambda: None)
    monkeypatch.setattr(jobs, "session_scope", scope)

    class NoCredential:
        def __init__(self):
            raise MissingCredentialError("DART missing")

    monkeypatch.setattr(jobs, "DartCollector", NoCredential)
    assert jobs.collect_dart_for_ipo("x")["skipped"] is True

    state = {"ipo": SimpleNamespace(id=1, corp_name="x", dart_corp_code=None, ticker=None)}

    class Collector:
        find_result = None
        filings = []
        body = "document"

        def find_corp(self, *_args, **_kwargs):
            return self.find_result

        def search_ipo_related_filings(self, _code):
            return self.filings

        def fetch_document_text(self, _receipt):
            return self.body

    monkeypatch.setattr(jobs, "DartCollector", Collector)
    monkeypatch.setattr(jobs, "_ipo_or_raise", lambda *_args: state["ipo"])
    missing = jobs.collect_dart_for_ipo("x")
    assert missing["updated"] is False and state["ipo"].needs_review is True

    state["ipo"] = SimpleNamespace(
        corp_name="x",
        dart_corp_code="corp",
        ticker="000001",
        offer_price_min=9_000,
        offer_price_max=10_000,
    )
    assert jobs.collect_dart_for_ipo("x")["reason"] == "IPO related filing not found"

    Collector.find_result = {"corp_code": "corp", "stock_code": ""}
    Collector.filings = [{}]
    state["ipo"] = SimpleNamespace(
        corp_name="x",
        dart_corp_code=None,
        ticker=None,
        offer_price_min=9_000,
        offer_price_max=10_000,
    )
    assert jobs.collect_dart_for_ipo("x")["reason"] == "receipt_no missing"

    Collector.find_result = {"corp_code": "corp2", "stock_code": "000002"}
    Collector.filings = [{"rcept_no": "receipt", "rcept_dt": "20260719"}]
    state["ipo"] = SimpleNamespace(
        id=7,
        corp_name="x",
        dart_corp_code=None,
        ticker="000002",
        offer_price_min=9_000,
        offer_price_max=10_000,
    )
    fields = {
        "offer_price_final": 11_000,
        "institutional_competition_ratio": 1200,
        "lockup_commitment_ratio": 0.2,
        "above_band_participation_ratio": 0.5,
        "participants_count": 1000,
        "evidence": {"x": 1},
        "parser_version": "test",
    }
    monkeypatch.setattr(jobs, "extract_demand_forecast_fields", lambda _body: fields)
    monkeypatch.setattr(jobs, "build_ipo_identity", lambda _ipo: "identity")
    monkeypatch.setattr(
        jobs,
        "upsert_demand_forecast",
        lambda *_args, **_kwargs: SimpleNamespace(receipt_no="receipt"),
    )
    monkeypatch.setattr(jobs, "save_source_snapshot", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(jobs, "upsert_diligence_review", lambda *_args, **_kwargs: None)
    success = jobs.collect_dart_for_ipo("x")
    assert success["updated"] is True
    assert state["ipo"].offer_price_final == 11_000

    fields.clear()
    Collector.filings = [{"rcept_no": "receipt2", "rcept_dt": "invalid"}]
    state["ipo"] = SimpleNamespace(
        id=8,
        corp_name="y",
        dart_corp_code="corp",
        ticker="000003",
        offer_price_min=9_000,
        offer_price_max=10_000,
    )
    assert jobs.collect_dart_for_ipo("y")["updated"] is True


def test_financial_collection_all_states(monkeypatch):
    @contextmanager
    def scope():
        yield object()

    monkeypatch.setattr(jobs, "init_db", lambda: None)
    monkeypatch.setattr(jobs, "session_scope", scope)
    with pytest.raises(ValueError, match="between"):
        jobs.collect_financials_for_ipo("x", years=0)
    with pytest.raises(ValueError, match="between"):
        jobs.collect_financials_for_ipo("x", years=11)

    class NoCredential:
        def __init__(self):
            raise MissingCredentialError("DART missing")

    monkeypatch.setattr(jobs, "DartFinancialCollector", NoCredential)
    assert jobs.collect_financials_for_ipo("x")["skipped"] is True

    state = {"ipo": SimpleNamespace(id=1, corp_name="x", dart_corp_code=None, ticker=None)}

    class Collector:
        find_result = None

        def find_corp(self, *_args, **_kwargs):
            return self.find_result

        def fetch_single_company_financials(self, corp_code, years):
            assert corp_code == "corp"
            assert list(years) == [2024, 2025]
            return [
                {
                    "fiscal_year": 2025,
                    "fs_div": "CFS",
                    "account_nm": "Revenue",
                    "account_id": "ifrs_Revenue",
                    "thstrm_amount": "1,000",
                    "currency": "KRW",
                },
                {
                    "fiscal_year": 2024,
                    "fs_div": "",
                    "sj_div": "BS",
                    "account_nm": "Assets",
                    "account_id": "",
                    "thstrm_amount": "(200)",
                },
            ]

    monkeypatch.setattr(jobs, "DartFinancialCollector", Collector)
    monkeypatch.setattr(jobs, "_ipo_or_raise", lambda *_args: state["ipo"])
    assert jobs.collect_financials_for_ipo("x")["updated"] is False

    Collector.find_result = {"corp_code": "corp", "stock_code": "000001"}
    statements = []
    metrics = []
    monkeypatch.setattr(jobs, "local_today", lambda: date(2026, 7, 19))
    monkeypatch.setattr(
        jobs,
        "upsert_financial_statement",
        lambda *_args, **kwargs: statements.append(kwargs["values"]),
    )
    monkeypatch.setattr(
        jobs,
        "summarize_financial_rows",
        lambda _rows: [{"fiscal_year": 2025, "revenue": 1000, "ignored": 1}],
    )
    monkeypatch.setattr(
        jobs, "upsert_financial_metric", lambda *_args, **kwargs: metrics.append(kwargs["values"])
    )
    result = jobs.collect_financials_for_ipo("x", years=2)
    assert result == {"ipo": "x", "statements": 2, "metrics": 1}
    assert statements[1]["statement_type"] == "BS"
    assert statements[1]["account_code"] == "Assets"
    assert statements[1]["amount"] == -200
    assert statements[1]["currency"] == "KRW"
    assert "ignored" not in metrics[0]
    # Existing identifiers must skip the DART corporation lookup entirely.
    assert jobs.collect_financials_for_ipo("x", years=2)["metrics"] == 1


@pytest.mark.parametrize(
    ("news_error", "trend_error", "expected_updated"),
    [(False, False, True), (True, True, False), (True, False, True)],
)
def test_news_trend_collection_success_and_isolated_failures(
    monkeypatch, news_error, trend_error, expected_updated
):
    @contextmanager
    def scope():
        yield object()

    monkeypatch.setattr(jobs, "init_db", lambda: None)
    monkeypatch.setattr(jobs, "session_scope", scope)
    ipo = SimpleNamespace(id=1, corp_name="테스트")
    monkeypatch.setattr(jobs, "_ipo_or_raise", lambda *_args: ipo)

    class News:
        def collect(self, _name):
            if news_error:
                raise RuntimeError("news down")
            return SimpleNamespace(deduped_article_count=3)

    class Trend:
        def collect(self, _name, _keywords):
            if trend_error:
                raise RuntimeError("trend down")
            return SimpleNamespace(trend_growth_rate=0.5)

    monkeypatch.setattr(jobs, "NaverNewsCollector", News)
    monkeypatch.setattr(jobs, "NaverDataLabCollector", Trend)
    monkeypatch.setattr(jobs, "upsert_news_metric", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(jobs, "upsert_diligence_review", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(jobs, "upsert_search_trend", lambda *_args, **_kwargs: None)
    result = jobs.collect_news_trend_for_ipo("테스트")
    assert result["updated"] is expected_updated
    assert len(result["errors"]) == int(news_error) + int(trend_error)


def test_news_trend_collection_missing_credentials(monkeypatch):
    monkeypatch.setattr(jobs, "init_db", lambda: None)

    class NoCredential:
        def __init__(self):
            raise MissingCredentialError("Naver missing")

    monkeypatch.setattr(jobs, "NaverNewsCollector", NoCredential)
    assert jobs.collect_news_trend_for_ipo("x")["skipped"] is True


def test_price_collection_and_ticker_resolution_states(monkeypatch):
    @contextmanager
    def scope():
        yield object()

    monkeypatch.setattr(jobs, "init_db", lambda: None)
    monkeypatch.setattr(jobs, "session_scope", scope)
    state = {"ipo": SimpleNamespace(id=1, corp_name="x", listing_date=None, ticker=None)}
    monkeypatch.setattr(jobs, "_ipo_or_raise", lambda *_args: state["ipo"])

    class Collector:
        def resolve_ticker(self, name, as_of):
            assert name == "x"
            assert as_of in {date(2026, 7, 18), date(2026, 7, 19)}
            return "000001"

        def collect_listing_prices(self, ticker, listing_date):
            assert ticker == "000001"
            return SimpleNamespace(
                d0_trade_date=listing_date,
                source="test",
                __dict__={"source": "test", "d0_trade_date": listing_date},
            )

    monkeypatch.setattr(jobs, "PriceCollector", Collector)
    assert jobs.collect_prices_for_ipo("x")["reason"] == "listing_date missing"

    state["ipo"] = SimpleNamespace(id=2, corp_name="x", listing_date=date(2026, 7, 19), ticker=None)
    monkeypatch.setattr(
        jobs,
        "upsert_listing_price",
        lambda *_args, **_kwargs: SimpleNamespace(source="test", d0_trade_date=date(2026, 7, 19)),
    )
    result = jobs.collect_prices_for_ipo("x")
    assert result["updated"] is True
    assert state["ipo"].actual_listing_date == date(2026, 7, 19)

    class NoD0Collector(Collector):
        def collect_listing_prices(self, _ticker, _listing_date):
            return SimpleNamespace(d0_trade_date=None, __dict__={})

    monkeypatch.setattr(jobs, "PriceCollector", NoD0Collector)
    state["ipo"] = SimpleNamespace(
        id=3, corp_name="x", listing_date=date(2026, 7, 19), ticker="000001"
    )
    monkeypatch.setattr(
        jobs,
        "upsert_listing_price",
        lambda *_args, **_kwargs: SimpleNamespace(source="test", d0_trade_date=None),
    )
    assert jobs.collect_prices_for_ipo("x")["d0_trade_date"] is None
    assert not hasattr(state["ipo"], "actual_listing_date")

    state["ipo"] = SimpleNamespace(
        corp_name="x",
        ticker="000001",
        listing_date=date(2026, 7, 19),
        actual_listing_date=None,
    )
    existing = jobs.resolve_ticker_for_ipo("x")
    assert existing["updated"] is False
    state["ipo"].ticker = None
    state["ipo"].actual_listing_date = date(2026, 7, 18)
    assert jobs.resolve_ticker_for_ipo("x")["ticker"] == "000001"
    state["ipo"].ticker = None
    state["ipo"].actual_listing_date = None
    assert jobs.resolve_ticker_for_ipo("x")["updated"] is True


def test_market_condition_empty_payload_and_payload_upsert(db_session, monkeypatch):
    _patch_scope(monkeypatch, db_session)
    monkeypatch.setattr(jobs, "get_settings", lambda: _settings())
    target = date(2026, 7, 19)

    class FailingCollector:
        def collect_market_condition(self, _target):
            raise RuntimeError("unavailable")

    monkeypatch.setattr(jobs, "PriceCollector", FailingCollector)
    empty = jobs.collect_market_condition(target)
    assert empty == {"updated": False, "reason": "RuntimeError: unavailable"}

    class EmptyPayloadCollector:
        def collect_market_condition(self, _target):
            return SimpleNamespace(
                kosdaq_momentum_20d=None,
                volatility_20d=None,
                source="prices",
            )

    monkeypatch.setattr(jobs, "PriceCollector", EmptyPayloadCollector)
    created = jobs.collect_market_condition(target)
    assert created["updated"] is True
    db_session.flush()
    row = db_session.query(MarketConditionSnapshot).one()
    assert row.source == "prices"
    assert created["recent_ipo_success_rate"] is None

    row.source = "existing"
    db_session.flush()
    monkeypatch.setattr(jobs, "PriceCollector", FailingCollector)
    # A point-in-time eligible outcome keeps the existing source and updates the rate.
    ipo = _ipo(
        "listed",
        listing_date=date(2026, 7, 10),
        actual_listing_date=None,
        status="listed",
    )
    db_session.add(ipo)
    db_session.flush()
    db_session.add(
        OutcomeEvaluation(
            ipo_id=ipo.id,
            evaluation_version="test",
            d1_close_return_pct=-1,
            evaluated_at=datetime(2026, 7, 19, 1, tzinfo=timezone.utc),
        )
    )
    db_session.flush()
    updated = jobs.collect_market_condition(target)
    assert updated["recent_ipo_success_rate"] == 0.0
    assert row.source == "existing"

    class CompletePayloadCollector:
        def collect_market_condition(self, _target):
            return SimpleNamespace(
                kosdaq_momentum_20d=0.1,
                volatility_20d=0.2,
                source="prices",
            )

    monkeypatch.setattr(jobs, "PriceCollector", CompletePayloadCollector)
    final = jobs.collect_market_condition(target)
    assert final["kosdaq_momentum_20d"] == pytest.approx(0.1)
    assert row.volatility_20d == pytest.approx(0.2)


def test_outcome_and_allocation_actual_jobs(db_session, monkeypatch):
    _patch_scope(monkeypatch, db_session)
    ipo = _ipo("allocation")
    db_session.add(ipo)
    db_session.flush()
    broker = UnderwriterAllocation(ipo_id=ipo.id, broker_name="한국투자증권")
    db_session.add(broker)
    db_session.flush()

    class OutcomeService:
        def evaluate_and_save(self, _session, selected):
            assert selected.id == ipo.id
            return SimpleNamespace(prediction_was_correct=True, evaluation_version="test-v1")

    monkeypatch.setattr(jobs, "OutcomeEvaluationService", OutcomeService)
    assert jobs.evaluate_outcome_for_ipo(ipo.id) == {
        "ipo": "allocation",
        "prediction_was_correct": True,
        "evaluation_version": "test-v1",
    }

    with pytest.raises(ValueError, match="positive"):
        jobs.record_allocation_actual("allocation", "한국투자증권", 0, 1)
    with pytest.raises(ValueError, match="positive"):
        jobs.record_allocation_actual("allocation", "한국투자증권", 1, 0)
    with pytest.raises(ValueError, match="Broker not found"):
        jobs.record_allocation_actual("allocation", "없는증권", 1, 1)
    result = jobs.record_allocation_actual("allocation", "한국투자", 100, 25.5, "manual")
    assert result["broker"] == "한국투자증권"
    assert broker.actual_applicants_count == 100
    assert broker.actual_proportional_competition == 25.5


def test_selected_batch_wrappers_and_operation_status(monkeypatch):
    selected = [SimpleNamespace(id=1), SimpleNamespace(id=2)]

    @contextmanager
    def scope():
        yield "session"

    monkeypatch.setattr(jobs, "init_db", lambda: None)
    monkeypatch.setattr(jobs, "session_scope", scope)
    monkeypatch.setattr(jobs, "refresh_ipo_statuses", lambda _session: None)
    monkeypatch.setattr(jobs, "list_active_ipos", lambda _session: selected)
    monkeypatch.setattr(jobs, "list_price_due_ipos", lambda _session: selected)
    monkeypatch.setattr(jobs, "list_evaluation_due_ipos", lambda _session: selected)
    monkeypatch.setattr(jobs, "collect_dart_for_ipo", lambda name: {"ipo": name})
    monkeypatch.setattr(jobs, "collect_financials_for_ipo", lambda name: {"ipo": name})
    monkeypatch.setattr(jobs, "collect_news_trend_for_ipo", lambda name: {"ipo": name})
    monkeypatch.setattr(jobs, "collect_prices_for_ipo", lambda name: {"ipo": name})
    monkeypatch.setattr(jobs, "evaluate_outcome_for_ipo", lambda name: {"ipo": name})
    assert jobs.collect_dart_for_active()["success"] == 2
    assert jobs.collect_financials_for_active()["success"] == 2
    assert jobs.collect_news_for_active()["success"] == 2
    assert jobs.collect_prices_due()["success"] == 2
    assert jobs.evaluate_outcomes_due()["success"] == 2

    with pytest.raises(ValueError, match="positive"):
        jobs._run_operations([], lambda _name: {}, max_attempts=0)

    cases = [
        ({"skipped": True}, "skipped"),
        ({"updated": True, "errors": ["x"]}, "partial"),
        ({"updated": False, "error": "x"}, "failed"),
        ({"warning": "x"}, "partial"),
        ({"updated": False, "warning": "x"}, "failed"),
        ({"updated": False, "reason": "not due"}, "skipped"),
        ({"updated": False}, "failed"),
        ({"updated": True}, "success"),
    ]
    assert [(jobs.operation_status(value), expected) for value, expected in cases] == [
        (expected, expected) for _value, expected in cases
    ]


@pytest.mark.parametrize("weekly", [False, True])
def test_telegram_schedule_summaries_enabled_and_disabled(monkeypatch, weekly):
    function = jobs.send_weekly_summary if weekly else jobs.send_daily_summary
    monkeypatch.setattr(jobs, "init_db", lambda: None)
    monkeypatch.setattr(jobs, "get_settings", lambda: _settings(telegram_configured=False))
    assert function()["skipped"] is True

    @contextmanager
    def scope():
        yield "session"

    rows = [SimpleNamespace(underwriters=["broker"]), SimpleNamespace(underwriters=[])]
    monkeypatch.setattr(jobs, "session_scope", scope)
    monkeypatch.setattr(jobs, "get_settings", lambda: _settings(telegram_configured=True))
    monkeypatch.setattr(jobs, "local_today", lambda: date(2026, 7, 19))
    monkeypatch.setattr(jobs, "list_ipos_between", lambda *_args: rows)
    monkeypatch.setattr(jobs, "format_schedule", lambda *_args: "summary")
    monkeypatch.setattr(
        jobs,
        "send_telegram_message",
        lambda _text, *, event_key: {} if weekly else {"ok": False, "key": event_key},
    )
    result = function()
    assert result["items"] == 2
    assert result["sent"] is weekly


def test_email_defaults_monthly_disabled_and_decision_eligibility(monkeypatch):
    today = date(2026, 7, 19)
    monkeypatch.setattr(jobs, "local_today", lambda: today)
    calls = []
    monkeypatch.setattr(
        jobs,
        "_build_email_content",
        lambda start, end, *, title: calls.append((start, end, title)) or ("content", 0),
    )
    assert jobs.build_daily_email_content() == ("content", 0)
    assert jobs.build_weekly_email_content() == ("content", 0)
    assert calls[0][0] == calls[0][1] == today
    assert calls[1][1] == date(2026, 7, 25)

    monkeypatch.setattr(jobs, "init_db", lambda: None)
    monkeypatch.setattr(jobs, "get_settings", lambda: _settings(telegram_configured=False))
    assert jobs.send_monthly_backtest()["skipped"] is True

    for status in ("subscription_closed", "listed", "withdrawn", "archived"):
        assert not jobs._eligible_for_decision_email(
            SimpleNamespace(is_sample=False, status=status, subscription_end_date=None), today
        )
    assert not jobs._eligible_for_decision_email(
        SimpleNamespace(is_sample=True, status="scheduled", subscription_end_date=None), today
    )
    assert jobs._eligible_for_decision_email(
        SimpleNamespace(is_sample=False, status="scheduled", subscription_end_date=None), today
    )
    assert jobs._eligible_for_decision_email(
        SimpleNamespace(is_sample=False, status="scheduled", subscription_end_date=today), today
    )
    assert not jobs._eligible_for_decision_email(
        SimpleNamespace(
            is_sample=False, status="scheduled", subscription_end_date=date(2026, 7, 18)
        ),
        today,
    )


def test_job_scalar_helpers():
    assert jobs._dart_receipt_datetime(None) is None
    assert jobs._dart_receipt_datetime("invalid") is None
    assert jobs._dart_receipt_datetime("20260719") == datetime(2026, 7, 18, 15, tzinfo=timezone.utc)

    assert jobs._final_price_position(None, 1, 2) is None
    assert jobs._final_price_position(1, None, 2) is None
    assert jobs._final_price_position(1, 0, None) is None
    assert jobs._final_price_position(3, 1, 2) == "above_band"
    assert jobs._final_price_position(2, 1, 2) == "top_band"
    assert jobs._final_price_position(0, 1, 2) == "below_band"
    assert jobs._final_price_position(1, 1, 2) == "in_band"

    assert jobs._amount(None) is None
    assert jobs._amount("") is None
    assert jobs._amount("-") is None
    assert jobs._amount("1,000") == 1000
    assert jobs._amount("(1,000)") == -1000
    assert jobs._financial_metric_fields({"fiscal_year": 2025, "revenue": 1, "x": 2}) == {
        "fiscal_year": 2025,
        "revenue": 1,
    }


def test_history_backfill_missing_listing_and_item_failure(db_session, monkeypatch):
    scope = _patch_scope(monkeypatch, db_session)
    missing = _ipo(
        "missing",
        listing_date=date(2026, 7, 10),
        ticker="000001",
        offer_price_final=10_000,
        status="listed",
    )
    failed = _ipo(
        "failed",
        listing_date=date(2026, 7, 11),
        ticker="000002",
        offer_price_final=10_000,
        status="listed",
    )
    db_session.add_all((missing, failed))
    db_session.flush()

    class Collector:
        def __init__(self):
            # Simulate the schedule being withdrawn between selection and processing.
            missing.listing_date = None
            db_session.flush()

        def collect_listing_prices(self, _ticker, _listing_date):
            raise RuntimeError("price down")

    monkeypatch.setattr(jobs, "PriceCollector", Collector)
    monkeypatch.setattr(jobs, "local_today", lambda: date(2026, 7, 19))
    # The scope remains explicitly isolated to the in-memory fixture.
    monkeypatch.setattr(jobs, "session_scope", scope)
    result = jobs.backfill_historical_outcomes(date(2026, 7, 1), date(2026, 7, 19))
    assert result["processed"] == 2
    assert result["skipped"] == 1
    assert result["failed"] == 1
    assert result["results"][0]["reason"] == "listing_date missing"
    assert result["results"][1]["status"] == "failed"


def test_history_backfill_handles_payload_without_d0_date(db_session, monkeypatch):
    _patch_scope(monkeypatch, db_session)
    ipo = _ipo(
        "no-d0",
        listing_date=date(2026, 7, 10),
        ticker="000001",
        offer_price_final=10_000,
        status="listed",
    )
    db_session.add(ipo)
    db_session.flush()

    class Collector:
        def collect_listing_prices(self, _ticker, listing_date):
            return SimpleNamespace(d0_trade_date=None, __dict__={"listing_date": listing_date})

    class EvaluationService:
        def evaluate_and_save(self, _session, _ipo):
            return SimpleNamespace(d0_open_return_pct=None, d1_close_return_pct=None)

    monkeypatch.setattr(jobs, "PriceCollector", Collector)
    monkeypatch.setattr(jobs, "OutcomeEvaluationService", EvaluationService)
    monkeypatch.setattr(jobs, "upsert_listing_price", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(jobs, "local_today", lambda: date(2026, 7, 19))
    result = jobs.backfill_historical_outcomes(date(2026, 7, 1), date(2026, 7, 19))
    assert result["evaluated"] == 1
    assert ipo.actual_listing_date is None


def test_seed_sample_data_idempotently_populates_helpers(db_session, monkeypatch):
    _patch_scope(monkeypatch, db_session)
    today = date(2026, 7, 19)
    monkeypatch.setattr(jobs, "local_today", lambda: today)

    class Scorer:
        def score_and_save(self, _session, ipo):
            assert ipo.is_sample is True
            return SimpleNamespace(total_score=77)

    monkeypatch.setattr(jobs, "IpoScoringService", Scorer)
    first = jobs.seed_sample_data()
    second = jobs.seed_sample_data()
    assert first == {"before": 0, "ipo": "ABC테크", "score": 77}
    assert second == {"before": 1, "ipo": "ABC테크", "score": 77}

    ipo = db_session.query(Ipo).filter(Ipo.corp_name == "ABC테크").one()
    assert db_session.query(UnderwriterAllocation).filter_by(ipo_id=ipo.id).count() == 2
    assert db_session.query(ValuationMetric).filter_by(ipo_id=ipo.id).count() == 1
    assert db_session.query(IpoStructureMetric).filter_by(ipo_id=ipo.id).count() == 1
    assert db_session.query(MarketConditionSnapshot).count() == 1

    financial = jobs.latest_financial_metric(db_session, ipo.id)
    news = jobs.latest_news_metric(db_session, ipo.id)
    trend = jobs.latest_search_trend(db_session, ipo.id)
    assert financial.fiscal_year == 2025
    assert news.deduped_article_count == 29
    assert trend.trend_growth_rate == pytest.approx(1.88)

    db_session.add(
        OutcomeEvaluation(
            ipo_id=ipo.id,
            evaluation_version="test",
            d1_close_return_pct=1,
            evaluated_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
        )
    )
    db_session.flush()
    assert jobs.latest_outcome(db_session, ipo.id).evaluation_version == "test"
