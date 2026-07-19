from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import repositories as repositories_module
from app.models import (
    AllocationScenario,
    BrokerTermVersion,
    DataQualityIssue,
    FinancialMetric,
    Ipo,
    IpoFactObservation,
    IpoScheduleRevision,
    ListingPrice,
    MonthlyUniverseSnapshot,
    NotificationContentArtifact,
    NotificationEvent,
    ScoreSnapshot,
    SourceDocument,
    SourceSnapshot,
    UnderwriterAllocation,
)
from app.repositories import (
    append_fact_observation,
    append_schedule_revision,
    build_ipo_identity,
    build_offering_id,
    count_rows,
    derive_status,
    find_ipo,
    get_ipo_or_raise,
    latest_score,
    list_active_ipos,
    list_evaluation_due_ipos,
    list_ipos_between,
    list_monthly_universe,
    list_price_due_ipos,
    refresh_ipo_statuses,
    save_allocation_result,
    save_broker_term_version,
    save_monthly_universe_snapshot,
    save_notification_content_artifact,
    save_source_document,
    save_source_snapshot,
    upsert_data_quality_issue,
    upsert_demand_forecast,
    upsert_diligence_review,
    upsert_financial_metric,
    upsert_financial_statement,
    upsert_listing_price,
    upsert_news_metric,
    upsert_outcome_evaluation,
    upsert_schedule_record,
    upsert_search_trend,
)
from app.schemas import AllocationResult, IpoScheduleRecord

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
            _ipo(corp_name="S", normalized_name="s", is_sample=True),
            _ipo(corp_name="T", normalized_name="t", data_origin="test"),
        ]
    )
    db_session.flush()
    assert [row.corp_name for row in list_active_ipos(db_session)] == ["A"]


@pytest.mark.parametrize(
    "event_field",
    [
        "demand_start_date",
        "demand_end_date",
        "subscription_start_date",
        "subscription_end_date",
        "refund_date",
        "payment_date",
        "listing_date",
    ],
)
def test_schedule_query_includes_every_displayed_event_date(db_session, event_field):
    row = _ipo(**{event_field: TODAY})
    db_session.add(row)
    db_session.flush()

    assert list_ipos_between(db_session, TODAY, TODAY) == [row]


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


def test_find_ipo_prefers_dart_identifier_and_exact_date(db_session):
    dart = _ipo(corp_name="DART", normalized_name="dart", dart_corp_code="D1")
    dated = _ipo(subscription_start_date=TODAY)
    db_session.add_all([dart, dated])
    db_session.flush()

    assert find_ipo(db_session, "다른이름", dart_corp_code="D1") is dart
    assert find_ipo(db_session, "테스트", TODAY) is dated


def test_user_lookup_returns_single_match_and_rejects_missing(db_session):
    row = _ipo()
    db_session.add(row)
    db_session.flush()

    assert get_ipo_or_raise(db_session, "테스트") is row
    with pytest.raises(ValueError, match="not found"):
        get_ipo_or_raise(db_session, "없는기업")
    with pytest.raises(ValueError, match="id=999999"):
        get_ipo_or_raise(db_session, 999999)


def test_price_due_query_uses_local_today_and_filters_ineligible_rows(db_session, monkeypatch):
    due = _ipo(
        corp_name="대상",
        normalized_name="대상",
        status="listed",
        ticker="000001",
        listing_date=TODAY,
    )
    archived = _ipo(
        corp_name="보관",
        normalized_name="보관",
        status="archived",
        ticker="000002",
        listing_date=TODAY,
    )
    old = _ipo(
        corp_name="오래됨",
        normalized_name="오래됨",
        status="listed",
        ticker="000003",
        listing_date=TODAY - timedelta(days=22),
    )
    sample = _ipo(
        corp_name="샘플",
        normalized_name="샘플",
        status="listed",
        ticker="000004",
        listing_date=TODAY,
        is_sample=True,
    )
    db_session.add_all([due, archived, old, sample])
    db_session.flush()
    monkeypatch.setattr(repositories_module, "local_today", lambda: TODAY)

    assert list_price_due_ipos(db_session) == [due]


def test_evaluation_due_query_requires_d3_price(db_session):
    due = _ipo(corp_name="대상", normalized_name="대상", status="listed", listing_date=TODAY)
    pending = _ipo(
        corp_name="대기",
        normalized_name="대기",
        status="listed",
        listing_date=TODAY,
    )
    synthetic = _ipo(
        corp_name="합성",
        normalized_name="합성",
        status="listed",
        listing_date=TODAY,
        data_origin="synthetic",
    )
    db_session.add_all([due, pending, synthetic])
    db_session.flush()
    db_session.add_all(
        [
            ListingPrice(
                ipo_id=due.id,
                listing_date=TODAY,
                source="test",
                d3_close_price=100,
            ),
            ListingPrice(ipo_id=pending.id, listing_date=TODAY, source="test"),
            ListingPrice(
                ipo_id=synthetic.id,
                listing_date=TODAY,
                source="test",
                d3_close_price=100,
            ),
        ]
    )
    db_session.flush()

    assert list_evaluation_due_ipos(db_session) == [due]


def test_evaluated_status_and_identity_fallbacks_are_preserved():
    assert derive_status(_ipo(status="evaluated"), TODAY) == "evaluated"
    assert (
        build_ipo_identity(_ipo(identity_key="legacy", dart_corp_code="D1", dart_receipt_no="R1"))
        == "dart:D1:R1"
    )
    assert build_ipo_identity(_ipo(identity_key="legacy")) == "name:테스트:unknown:v1"


def test_schedule_change_increments_version_and_ignores_blank_broker(db_session):
    first = upsert_schedule_record(
        db_session,
        IpoScheduleRecord(
            corp_name="변경기업",
            kind_biz_no="K-CHANGE",
            subscription_start_date=TODAY,
            underwriters=[""],
        ),
    )
    db_session.flush()
    second = upsert_schedule_record(
        db_session,
        IpoScheduleRecord(
            corp_name="변경기업",
            kind_biz_no="K-CHANGE",
            subscription_start_date=TODAY + timedelta(days=1),
        ),
    )

    assert second is first
    assert second.schedule_version == 2
    assert second.underwriters == []


def test_existing_source_snapshot_accepts_payload_and_ipo_link(db_session):
    ipo = _ipo()
    db_session.add(ipo)
    db_session.flush()
    first = save_source_snapshot(db_session, "kind", "key", "same")
    db_session.flush()

    second = save_source_snapshot(
        db_session,
        "kind",
        "key",
        "same",
        ipo_id=ipo.id,
        payload_json={"updated": True},
    )

    assert second is first
    assert second.ipo_id == ipo.id
    assert second.payload_json == {"updated": True}


def test_latest_score_returns_newest_snapshot(db_session):
    ipo = _ipo()
    db_session.add(ipo)
    db_session.flush()
    older = ScoreSnapshot(
        ipo_id=ipo.id,
        score_date=TODAY,
        total_score=10,
        recommendation_band="Avoid",
        model_version="old",
    )
    newer = ScoreSnapshot(
        ipo_id=ipo.id,
        score_date=TODAY,
        total_score=20,
        recommendation_band="Avoid",
        model_version="new",
    )
    db_session.add_all([older, newer])
    db_session.flush()

    assert latest_score(db_session, ipo.id) is newer


def test_outcome_without_score_is_upserted(db_session):
    ipo = _ipo()
    db_session.add(ipo)
    db_session.flush()
    first = upsert_outcome_evaluation(
        db_session,
        ipo_id=ipo.id,
        score_snapshot_id=None,
        evaluation_version="test",
        values={"offer_price": 100},
    )
    db_session.flush()
    second = upsert_outcome_evaluation(
        db_session,
        ipo_id=ipo.id,
        score_snapshot_id=None,
        evaluation_version="test",
        values={"offer_price": 200},
    )

    assert second is first
    assert float(second.offer_price) == 200


def test_allocation_result_is_saved_and_counted(db_session):
    ipo = _ipo()
    db_session.add(ipo)
    db_session.flush()
    result = AllocationResult(
        ipo_id=ipo.id,
        broker_name="한국투자증권",
        user_cash=1_000_000,
        offer_price=10_000,
        apply_shares=100,
        expected_equal_shares_low=0.1,
        expected_equal_shares_mid=0.2,
        expected_equal_shares_high=0.3,
        expected_proportional_shares_conservative=0.05,
        expected_proportional_shares_base=0.1,
        expected_proportional_shares_optimistic=0.2,
        expected_total_shares_base=0.3,
        estimated_fee=2_000,
        estimated_required_deposit=500_000,
    )

    scenario = save_allocation_result(db_session, result)
    db_session.flush()

    assert scenario.ipo_id == ipo.id
    assert count_rows(db_session, [Ipo, AllocationScenario]) == {
        "ipos": 1,
        "allocation_scenarios": 1,
    }


def test_notification_content_artifact_is_hashed_immutable_and_idempotent(db_session):
    event = NotificationEvent(
        channel="email",
        event_key="monthly:2026-07:1",
        recipient="reader@example.com",
        status="pending",
    )
    db_session.add(event)
    db_session.flush()
    values = {
        "notification_event_id": event.id,
        "subject": "7월 IPO 1/2",
        "text_body": "한글 본문",
        "html_body": "<p>한글 본문</p>",
        "universe_hash": "a" * 64,
        "evidence_hash": "b" * 64,
        "metadata": {"period": "2026-07", "part_number": 1},
    }

    artifact = save_notification_content_artifact(db_session, **values)
    same = save_notification_content_artifact(db_session, **values)

    assert same is artifact
    assert event.content_artifact is artifact
    assert artifact.notification_event is event
    assert artifact.content_redacted is False
    assert artifact.subject_sha256 == (
        "f4359bbfd04d7834340afe0df40cc14b019a0b879419fdcfd74884fd6693f45c"
    )
    assert all(
        len(value) == 64
        for value in (
            artifact.text_sha256,
            artifact.html_sha256,
            artifact.content_sha256,
        )
    )
    assert db_session.query(NotificationContentArtifact).count() == 1

    with pytest.raises(ValueError, match="immutable"):
        save_notification_content_artifact(
            db_session,
            **{**values, "text_body": "변조된 본문"},
        )
    with pytest.raises(ValueError, match="not found"):
        save_notification_content_artifact(
            db_session,
            **{**values, "notification_event_id": event.id + 99},
        )


def test_monthly_universe_uses_overlap_actual_listing_and_filing_appendix(db_session):
    july_start = date(2026, 7, 1)
    july_end = date(2026, 7, 31)
    spanning = _ipo(
        corp_name="기간겹침",
        normalized_name="기간겹침",
        status="withdrawn",
        demand_start_date=date(2026, 6, 30),
        demand_end_date=date(2026, 8, 1),
        listing_date=date(2026, 7, 20),
        actual_listing_date=date(2026, 7, 22),
    )
    open_ended = _ipo(
        corp_name="끝만확인",
        normalized_name="끝만확인",
        subscription_end_date=date(2026, 7, 2),
    )
    filing_only = _ipo(
        corp_name="공시부록",
        normalized_name="공시부록",
        report_filed_date=date(2026, 7, 3),
        listing_date=date(2026, 8, 10),
    )
    sample = _ipo(
        corp_name="샘플",
        normalized_name="샘플",
        is_sample=True,
        subscription_start_date=date(2026, 7, 1),
    )
    synthetic = _ipo(
        corp_name="합성",
        normalized_name="합성",
        data_origin="synthetic",
        subscription_start_date=date(2026, 7, 1),
    )
    db_session.add_all([spanning, open_ended, filing_only, sample, synthetic])
    db_session.flush()

    result = list_monthly_universe(db_session, july_start, july_end)

    assert result.core_count == 2
    assert [entry.ipo for entry in result.core] == [spanning, open_ended]
    assert result.core[0].event_badges == (
        "demand_forecast",
        "actual_listing",
        "planned_listing",
    )
    assert result.core[0].display_listing_date == date(2026, 7, 22)
    assert result.core[0].planned_listing_date == date(2026, 7, 20)
    assert result.core[0].reconciliation_state == "unreconciled"
    assert result.core[1].inclusion_dates == {"subscription": (date(2026, 7, 2),)}
    assert [entry.ipo for entry in result.filing_update_appendix] == [filing_only]
    assert result.filing_update_appendix[0].event_badges == ("filing_update",)


def test_monthly_universe_single_dates_and_latest_reconciliation(db_session):
    july_start = date(2026, 7, 1)
    july_end = date(2026, 7, 31)
    ipo = _ipo(
        demand_start_date=date(2026, 7, 2),
        refund_date=date(2026, 7, 4),
        payment_date=date(2026, 7, 5),
    )
    db_session.add(ipo)
    db_session.flush()
    db_session.add_all(
        [
            MonthlyUniverseSnapshot(
                month_start=july_start,
                ipo_id=ipo.id,
                offering_id_snapshot=ipo.offering_id,
                cohort="core",
                event_badges_json=["demand_forecast"],
                inclusion_dates_json={"demand_forecast": ["2026-07-02"]},
                reconciliation_state="partial",
                snapshot_hash="a",
                captured_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            ),
            MonthlyUniverseSnapshot(
                month_start=july_start,
                ipo_id=ipo.id,
                offering_id_snapshot=ipo.offering_id,
                cohort="core",
                event_badges_json=["demand_forecast"],
                inclusion_dates_json={"demand_forecast": ["2026-07-02"]},
                reconciliation_state="verified",
                snapshot_hash="b",
                captured_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
            ),
        ]
    )
    db_session.flush()

    entry = list_monthly_universe(db_session, july_start, july_end).core[0]

    assert entry.event_badges == ("demand_forecast", "refund", "payment")
    assert entry.reconciliation_state == "verified"
    assert list_ipos_between(db_session, july_start, july_end) == [ipo]


def test_monthly_universe_empty_and_invalid_range(db_session):
    empty = list_monthly_universe(db_session, date(2026, 7, 1), date(2026, 7, 31))
    assert empty.core == ()
    assert empty.filing_update_appendix == ()
    with pytest.raises(ValueError, match="month_start"):
        list_monthly_universe(db_session, date(2026, 8, 1), date(2026, 7, 31))


def test_filing_appendix_requires_a_date():
    with pytest.raises(ValueError, match="report_filed_date"):
        repositories_module._filing_appendix_entry(_ipo(), "unreconciled")


def test_offering_identity_is_stable_across_document_revisions():
    ipo = _ipo(
        identity_key="legacy",
        dart_corp_code="D1",
        dart_receipt_no="R1",
        subscription_start_date=TODAY,
    )
    first = build_offering_id(ipo)
    ipo.dart_receipt_no = "R2"
    assert build_offering_id(ipo) == first == "dart:D1:equity:2026-07-18"
    assert build_offering_id(_ipo(kind_biz_no="K1")) == "kind:K1"
    assert build_offering_id(_ipo(ticker="000001", listing_date=TODAY)) == "krx:000001:2026-07-18"


def test_offering_identity_preserves_existing_and_has_unknown_fallback():
    existing = _ipo(offering_id="canonical:fixed", subscription_start_date=TODAY)
    unknown = _ipo(identity_key="unknown-source", offering_id=None)

    assert build_offering_id(existing) == "canonical:fixed"
    assert build_offering_id(unknown).startswith("offering:temp:")


def test_source_document_revision_and_fact_observation_are_idempotent(db_session):
    first_ipo = _ipo(corp_name="A", normalized_name="a")
    second_ipo = _ipo(corp_name="B", normalized_name="b")
    db_session.add_all([first_ipo, second_ipo])
    db_session.flush()
    document = save_source_document(
        db_session,
        ipo_id=first_ipo.id,
        source="dart",
        document_key="R1",
        document_type="registration",
        lineage_root_key="ROOT",
        values={"parser_version": "v1", "unknown": "ignored"},
    )
    db_session.flush()

    same_document = save_source_document(
        db_session,
        ipo_id=first_ipo.id,
        source="dart",
        document_key="R1",
        document_type="registration",
    )
    assert same_document is document
    assert document.lineage_root_key == "ROOT"
    assert document.parser_version == "v1"
    with pytest.raises(ValueError, match="another offering"):
        save_source_document(
            db_session,
            ipo_id=second_ipo.id,
            source="dart",
            document_key="R1",
            document_type="registration",
        )

    published_at = datetime(2026, 7, 1, tzinfo=timezone.utc)
    fetched_at = datetime(2026, 7, 1, 1, tzinfo=timezone.utc)
    fact = append_fact_observation(
        db_session,
        ipo_id=first_ipo.id,
        source_document_id=document.id,
        field_name="institutional_competition_ratio",
        value=1000.5,
        unit="ratio",
        quality_state="reported",
        knowledge_phase="pre_subscription",
        values={
            "published_at": published_at,
            "fetched_at": fetched_at,
            "parser_version": "v1",
        },
    )
    db_session.flush()
    same_fact = append_fact_observation(
        db_session,
        ipo_id=first_ipo.id,
        source_document_id=document.id,
        field_name="institutional_competition_ratio",
        value=1000.5,
        unit="ratio",
        quality_state="reported",
        knowledge_phase="pre_subscription",
        values={"published_at": published_at, "fetched_at": fetched_at},
    )
    changed_fact = append_fact_observation(
        db_session,
        ipo_id=first_ipo.id,
        source_document_id=document.id,
        field_name="institutional_competition_ratio",
        value=1001.0,
        unit="ratio",
        quality_state="reported",
        knowledge_phase="pre_subscription",
    )

    assert same_fact is fact
    assert fact.fetched_at == fetched_at
    assert changed_fact is not fact
    assert db_session.query(SourceDocument).count() == 1
    assert db_session.query(IpoFactObservation).count() == 2


def test_schedule_revision_broker_terms_and_quality_issue_versions(db_session):
    ipo = _ipo()
    db_session.add(ipo)
    db_session.flush()
    schedule_values = {
        "subscription_start_date": TODAY,
        "planned_listing_date": TODAY + timedelta(days=10),
    }
    revision = append_schedule_revision(
        db_session,
        ipo_id=ipo.id,
        event_status="scheduled",
        values=schedule_values,
    )
    db_session.flush()
    assert (
        append_schedule_revision(
            db_session,
            ipo_id=ipo.id,
            event_status="scheduled",
            values=schedule_values,
        )
        is revision
    )
    assert (
        append_schedule_revision(
            db_session,
            ipo_id=ipo.id,
            event_status="withdrawn",
            values=schedule_values,
        )
        is not revision
    )

    term = save_broker_term_version(
        db_session,
        broker_name="KB증권",
        effective_from=TODAY,
        values={"subscription_fee": 1500},
    )
    db_session.flush()
    assert (
        save_broker_term_version(
            db_session,
            broker_name="KB증권",
            effective_from=TODAY,
            values={"subscription_fee": 1500},
        )
        is term
    )

    issue = upsert_data_quality_issue(
        db_session,
        ipo_id=ipo.id,
        issue_type="missing",
        severity="high",
        status="open",
        field_name="refund_date",
        details={"source": "kind"},
    )
    db_session.flush()
    same_issue = upsert_data_quality_issue(
        db_session,
        ipo_id=ipo.id,
        issue_type="missing",
        severity="low",
        status="resolved",
        field_name="refund_date",
        details={"source": "kind"},
        values={"resolution_note": "verified"},
    )
    assert same_issue is issue
    assert issue.status == "resolved"
    assert issue.severity == "low"
    assert issue.resolution_note == "verified"
    assert db_session.query(IpoScheduleRevision).count() == 2
    assert db_session.query(BrokerTermVersion).count() == 1
    assert db_session.query(DataQualityIssue).count() == 1


def test_monthly_snapshot_persistence_is_idempotent_and_drives_read_state(db_session):
    july_start = date(2026, 7, 1)
    july_end = date(2026, 7, 31)
    core = _ipo(subscription_start_date=TODAY)
    appendix = _ipo(
        corp_name="공시",
        normalized_name="공시",
        report_filed_date=TODAY,
    )
    db_session.add_all([core, appendix])
    db_session.flush()
    universe = list_monthly_universe(db_session, july_start, july_end)

    first = save_monthly_universe_snapshot(
        db_session,
        universe,
        reconciliation_state="verified",
        reconciliation_sources=["kind", "dart", "kind"],
    )
    db_session.flush()
    second = save_monthly_universe_snapshot(
        db_session,
        universe,
        reconciliation_state="verified",
        reconciliation_sources=["dart", "kind"],
    )

    assert second == first
    assert len(first) == 2
    assert {row.cohort for row in first} == {"core", "filing_update_appendix"}
    assert first[0].reconciliation_sources_json == ["dart", "kind"]
    assert db_session.query(MonthlyUniverseSnapshot).count() == 2
    refreshed = list_monthly_universe(db_session, july_start, july_end)
    assert refreshed.core[0].reconciliation_state == "verified"


def test_remaining_repository_identity_and_update_paths(db_session):
    row = _ipo(subscription_start_date=TODAY + timedelta(days=2))
    db_session.add(row)
    db_session.flush()
    assert find_ipo(db_session, "테스트", dart_corp_code="missing") is row
    assert (
        derive_status(
            _ipo(
                subscription_start_date=TODAY + timedelta(days=1),
                subscription_end_date=TODAY + timedelta(days=2),
            ),
            TODAY,
        )
        == "scheduled"
    )
    assert (
        build_offering_id(
            _ipo(
                offering_id="offering:temp:old",
                subscription_start_date=TODAY,
            )
        )
        == "name:테스트:2026-07-18"
    )
    schedule = upsert_schedule_record(
        db_session,
        IpoScheduleRecord(
            corp_name="날짜식별",
            subscription_start_date=TODAY,
            raw={"source_key": "date-id"},
        ),
    )
    db_session.flush()
    assert schedule.offering_id == "name:날짜식별:2026-07-18"
    assert refresh_ipo_statuses(db_session, TODAY)["checked"] == 2

    score = ScoreSnapshot(
        ipo_id=row.id,
        score_date=TODAY,
        total_score=1,
        recommendation_band="Avoid",
    )
    db_session.add(score)
    db_session.flush()
    outcome = upsert_outcome_evaluation(
        db_session,
        ipo_id=row.id,
        score_snapshot_id=score.id,
        evaluation_version="scored",
        values={"offer_price": 100},
    )
    assert outcome.score_snapshot_id == score.id

    review = upsert_diligence_review(
        db_session,
        ipo_id=row.id,
        review_type="source",
        values={"review_status": "collected"},
    )
    db_session.flush()
    updated = upsert_diligence_review(
        db_session,
        ipo_id=row.id,
        review_type="source",
        values={"review_status": "reviewed"},
    )
    assert updated is review
    assert updated.review_status == "reviewed"
