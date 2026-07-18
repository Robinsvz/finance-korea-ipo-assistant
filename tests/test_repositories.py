from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.models import FinancialMetric, Ipo, SourceSnapshot, UnderwriterAllocation
from app.repositories import (
    derive_status,
    find_ipo,
    get_ipo_or_raise,
    list_active_ipos,
    refresh_ipo_statuses,
    save_source_snapshot,
    upsert_demand_forecast,
    upsert_diligence_review,
    upsert_financial_metric,
    upsert_financial_statement,
    upsert_listing_price,
    upsert_news_metric,
    upsert_schedule_record,
    upsert_search_trend,
)
from app.schemas import IpoScheduleRecord

TODAY = date(2026, 7, 18)


def _ipo(**values):
    defaults = {"corp_name": "테스트", "normalized_name": "테스트", "status": "scheduled"}
    return Ipo(**{**defaults, **values})


def test_status_scheduled():
    assert (
        derive_status(_ipo(subscription_start_date=TODAY + timedelta(days=2)), TODAY) == "scheduled"
    )


def test_status_demand_forecast():
    ipo = _ipo(demand_start_date=TODAY, demand_end_date=TODAY + timedelta(days=1))
    assert derive_status(ipo, TODAY) == "demand_forecast"


def test_status_subscription_open():
    ipo = _ipo(subscription_start_date=TODAY, subscription_end_date=TODAY + timedelta(days=1))
    assert derive_status(ipo, TODAY) == "subscription_open"


def test_status_subscription_closed():
    ipo = _ipo(
        subscription_start_date=TODAY - timedelta(days=2),
        subscription_end_date=TODAY - timedelta(days=1),
    )
    assert derive_status(ipo, TODAY) == "subscription_closed"


def test_status_listed():
    assert derive_status(_ipo(listing_date=TODAY), TODAY) == "listed"


def test_terminal_status_is_preserved():
    assert derive_status(_ipo(status="withdrawn", listing_date=TODAY), TODAY) == "withdrawn"


def test_active_query_uses_allowlist(db_session):
    db_session.add_all(
        [
            _ipo(corp_name="A", normalized_name="a"),
            _ipo(corp_name="B", normalized_name="b", status="listed"),
        ]
    )
    db_session.flush()
    assert [row.corp_name for row in list_active_ipos(db_session)] == ["A"]


def test_refresh_statuses_updates_stale_rows(db_session):
    row = _ipo(listing_date=TODAY - timedelta(days=1))
    db_session.add(row)
    db_session.flush()
    result = refresh_ipo_statuses(db_session, TODAY)
    assert result == {"checked": 1, "changed": 1}
    assert row.status == "listed"


def test_find_ipo_prefers_kind_identifier(db_session):
    row = _ipo(kind_biz_no="K1")
    db_session.add(row)
    db_session.flush()
    assert find_ipo(db_session, "다른이름", kind_biz_no="K1") is row


def test_find_ipo_can_promote_single_placeholder(db_session):
    row = _ipo(subscription_start_date=None)
    db_session.add(row)
    db_session.flush()
    assert find_ipo(db_session, "테스트", TODAY) is row


def test_user_facing_lookup_rejects_ambiguous_same_name(db_session):
    db_session.add_all(
        [
            _ipo(subscription_start_date=TODAY),
            _ipo(subscription_start_date=TODAY + timedelta(days=30)),
        ]
    )
    db_session.flush()

    with pytest.raises(ValueError, match="ambiguous"):
        get_ipo_or_raise(db_session, "테스트")


def test_internal_lookup_by_id_is_unambiguous(db_session):
    first = _ipo(subscription_start_date=TODAY)
    second = _ipo(subscription_start_date=TODAY + timedelta(days=30))
    db_session.add_all([first, second])
    db_session.flush()

    assert get_ipo_or_raise(db_session, second.id) is second


def test_collector_does_not_regress_human_review_status(db_session):
    ipo = _ipo()
    db_session.add(ipo)
    db_session.flush()
    reviewed = upsert_diligence_review(
        db_session,
        ipo_id=ipo.id,
        review_type="articles",
        values={
            "review_status": "reviewed",
            "reviewed_by": "qa-user",
            "evidence_refs_json": ["manual:evidence"],
        },
    )
    db_session.flush()
    collected = upsert_diligence_review(
        db_session,
        ipo_id=ipo.id,
        review_type="articles",
        values={"review_status": "collected", "evidence_refs_json": ["naver_news"]},
    )

    assert collected is reviewed
    assert collected.review_status == "reviewed"
    assert collected.reviewed_by == "qa-user"
    assert collected.evidence_refs_json == ["manual:evidence"]


def test_schedule_upsert_preserves_all_fields_and_not_refund(db_session):
    record = IpoScheduleRecord(
        corp_name="신규",
        kind_biz_no="K2",
        report_filed_date=TODAY - timedelta(days=30),
        demand_start_date=TODAY - timedelta(days=5),
        demand_end_date=TODAY - timedelta(days=4),
        subscription_start_date=TODAY + timedelta(days=1),
        subscription_end_date=TODAY + timedelta(days=2),
        payment_date=TODAY + timedelta(days=4),
        offer_amount_million=12_345,
        underwriters=["한투", "한투"],
    )
    row = upsert_schedule_record(db_session, record)
    db_session.flush()
    assert row.report_filed_date == record.report_filed_date
    assert row.demand_start_date == record.demand_start_date
    assert row.offer_amount_million == 12_345
    assert row.refund_date is None
    assert row.refund_date_source is None
    assert row.payment_date_source == "kind"
    assert row.payment_date_verified_at is not None
    assert len(row.underwriters) == 1
    assert row.underwriters[0].broker_name == "한국투자증권"


def test_schedule_records_refund_source_separately(db_session):
    record = IpoScheduleRecord(
        corp_name="환불확인",
        kind_biz_no="K-REFUND",
        refund_date=TODAY,
        payment_date=TODAY + timedelta(days=1),
        source="verified-fixture",
    )
    row = upsert_schedule_record(db_session, record)
    assert row.refund_date_source == "verified-fixture"
    assert row.payment_date_source == "verified-fixture"
    assert row.refund_date_verified_at is not None


def test_schedule_upsert_is_idempotent(db_session):
    record = IpoScheduleRecord(corp_name="신규", kind_biz_no="K3", underwriters=["NH"])
    first = upsert_schedule_record(db_session, record)
    db_session.flush()
    second = upsert_schedule_record(db_session, record)
    db_session.flush()
    assert first.id == second.id
    assert db_session.query(UnderwriterAllocation).count() == 1


def test_unknown_schedule_source_keys_are_not_merged(db_session):
    first = upsert_schedule_record(
        db_session,
        IpoScheduleRecord(corp_name="동일기업", raw={"source_key": "draft-1"}),
    )
    db_session.flush()
    second = upsert_schedule_record(
        db_session,
        IpoScheduleRecord(corp_name="동일기업", raw={"source_key": "draft-2"}),
    )
    db_session.flush()
    assert first.id != second.id
    assert second.needs_review is True


def test_unknown_schedule_same_source_key_is_idempotent(db_session):
    record = IpoScheduleRecord(corp_name="동일기업", raw={"source_key": "draft-1"})
    first = upsert_schedule_record(db_session, record)
    db_session.flush()
    second = upsert_schedule_record(db_session, record)
    assert first.id == second.id


def test_source_snapshot_is_content_idempotent(db_session):
    first = save_source_snapshot(db_session, "kind", "key", "same")
    db_session.flush()
    second = save_source_snapshot(db_session, "kind", "key", "same")
    db_session.flush()
    assert first.id == second.id
    assert db_session.query(SourceSnapshot).count() == 1


def test_financial_metric_upsert_updates_existing(db_session):
    ipo = _ipo()
    db_session.add(ipo)
    db_session.flush()
    first = upsert_financial_metric(
        db_session,
        ipo_id=ipo.id,
        values={"fiscal_year": 2025, "source": "dart", "revenue": 10},
    )
    db_session.flush()
    second = upsert_financial_metric(
        db_session,
        ipo_id=ipo.id,
        values={"fiscal_year": 2025, "source": "dart", "revenue": 20},
    )
    db_session.flush()
    assert first.id == second.id
    assert float(db_session.execute(select(FinancialMetric.revenue)).scalar_one()) == 20


def test_all_collector_natural_key_upserts_are_idempotent(db_session):
    ipo = _ipo(corp_name="UP", normalized_name="up")
    db_session.add(ipo)
    db_session.flush()
    calls = [
        lambda value: upsert_demand_forecast(
            db_session,
            ipo_id=ipo.id,
            receipt_no="R1",
            values={"institutional_competition_ratio": value},
        ),
        lambda value: upsert_financial_statement(
            db_session,
            ipo_id=ipo.id,
            values={
                "fiscal_year": 2025,
                "statement_type": "CFS",
                "account_code": "Revenue",
                "amount": value,
            },
        ),
        lambda value: upsert_news_metric(
            db_session,
            ipo_id=ipo.id,
            values={
                "window_start": TODAY,
                "window_end": TODAY,
                "source": "test",
                "deduped_article_count": value,
            },
        ),
        lambda value: upsert_search_trend(
            db_session,
            ipo_id=ipo.id,
            values={
                "keyword_group": "UP",
                "window_start": TODAY,
                "window_end": TODAY,
                "source": "test",
                "peak_index": value,
            },
        ),
        lambda value: upsert_listing_price(
            db_session,
            ipo_id=ipo.id,
            values={"listing_date": TODAY, "source": "test", "d0_close_price": value},
        ),
    ]
    for call in calls:
        first = call(1)
        db_session.flush()
        second = call(2)
        db_session.flush()
        assert first.id == second.id


def test_upsert_is_idempotent_without_manual_flush(db_session):
    ipo = _ipo(corp_name="NOFLUSH", normalized_name="noflush")
    db_session.add(ipo)
    db_session.flush()
    first = upsert_financial_metric(
        db_session,
        ipo_id=ipo.id,
        values={"fiscal_year": 2025, "source": "test", "revenue": 1},
    )
    second = upsert_financial_metric(
        db_session,
        ipo_id=ipo.id,
        values={"fiscal_year": 2025, "source": "test", "revenue": 2},
    )
    db_session.commit()
    assert first.id == second.id
    assert db_session.query(FinancialMetric).filter_by(ipo_id=ipo.id).count() == 1
    assert float(second.revenue) == 2


def test_partial_observation_does_not_erase_last_known_good(db_session):
    ipo = _ipo(corp_name="PATCH", normalized_name="patch")
    db_session.add(ipo)
    db_session.flush()
    row = upsert_listing_price(
        db_session,
        ipo_id=ipo.id,
        values={
            "listing_date": TODAY,
            "source": "test",
            "d0_close_price": 100,
            "d3_close_price": 130,
        },
    )
    db_session.flush()
    same = upsert_listing_price(
        db_session,
        ipo_id=ipo.id,
        values={
            "listing_date": TODAY,
            "source": "test",
            "d0_close_price": 110,
            "d3_close_price": None,
        },
    )
    assert same.id == row.id
    assert float(same.d0_close_price) == 110
    assert float(same.d3_close_price) == 130
