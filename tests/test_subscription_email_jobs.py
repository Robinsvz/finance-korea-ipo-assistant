from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.config import DEFAULT_EMAIL_RECIPIENT
from app.models import Ipo, IpoFactObservation, SourceDocument
from app.services import jobs
from app.services.email import EmailContent


def _content(name: str) -> EmailContent:
    return EmailContent(
        subject=f"[Korea IPO] {name} 일반청약 D-1",
        text=f"{name} text",
        html=f"<p>{name}</p>",
    )


def _bundle(
    ipo_id: int,
    name: str,
    *,
    reconciliation_state: str = jobs.SUBSCRIPTION_RECONCILIATION_STATE,
    parts: int = 1,
    rendered_ipo_id: int | None = None,
):
    return SimpleNamespace(
        read_model=SimpleNamespace(
            reconciliation_state=reconciliation_state,
            core=(
                SimpleNamespace(
                    ipo_id=rendered_ipo_id or ipo_id,
                    missing_labels=(),
                ),
            ),
            filing_appendix=(),
        ),
        parts=tuple(_content(name) for _ in range(parts)),
        universe_hash=f"{ipo_id:x}".rjust(64, "a")[-64:],
        evidence_hash=f"{ipo_id:x}".rjust(64, "b")[-64:],
    )


def _candidate(ipo_id: int, name: str, *, warnings: tuple[str, ...] = ()):
    return jobs._SubscriptionEmailCandidate(
        ipo_id=ipo_id,
        offering_id=f"offering:{ipo_id}",
        corp_name=name,
        subscription_start_date=date(2026, 7, 20),
        bundle=_bundle(ipo_id, name),
        evidence_warnings=warnings,
    )


def test_kind_schedule_default_window_is_explicit_and_snapshot_is_covering(monkeypatch):
    captured = {}
    collector = SimpleNamespace(
        collect_with_raw=lambda **kwargs: captured.update(collect=kwargs) or ([], "raw")
    )
    monkeypatch.setattr(jobs, "init_db", lambda: None)
    monkeypatch.setattr(jobs, "local_today", lambda: date(2026, 7, 19))
    monkeypatch.setattr(
        jobs,
        "get_settings",
        lambda: SimpleNamespace(kind_lookback_days=180, kind_lookahead_days=365),
    )
    monkeypatch.setattr(jobs, "KindScheduleCollector", lambda: collector)

    @contextmanager
    def scope():
        yield SimpleNamespace(flush=lambda: None)

    monkeypatch.setattr(jobs, "session_scope", scope)

    def save(_session, source, source_key, raw, **kwargs):
        captured.update(
            source=source,
            source_key=source_key,
            raw=raw,
            payload=kwargs["payload_json"],
        )
        return SimpleNamespace(raw_text_hash="hash")

    monkeypatch.setattr(jobs, "save_source_snapshot", save)

    result = jobs.collect_kind_schedule()

    assert result == {"records": 0, "ipos": 0, "snapshot_hash": "hash"}
    assert captured["collect"] == {
        "start": date(2026, 1, 20),
        "end": date(2027, 7, 19),
    }
    assert captured["source_key"] == "2026-01-20:2027-07-19"
    assert captured["source"] == "kind"
    assert captured["raw"] == "raw"
    assert captured["payload"] == {"record_count": 0}


def test_d_minus_one_refresh_jobs_target_tomorrows_calendar_month(monkeypatch):
    monkeypatch.setattr(jobs, "local_today", lambda: date(2026, 7, 31))
    calls = []
    monkeypatch.setattr(
        jobs,
        "collect_kind_schedule",
        lambda start, end: calls.append(("kind_schedule", start, end)) or {"records": 1},
    )
    monkeypatch.setattr(
        jobs,
        "collect_kind_listings_for_current_month",
        lambda target: calls.append(("kind_listing", target)) or {"records": 1},
    )
    monkeypatch.setattr(
        jobs,
        "collect_public_dart_for_current_month",
        lambda target: calls.append(("dart", target)) or {"success": 1},
    )
    monkeypatch.setattr(
        jobs,
        "reconcile_current_month_evidence",
        lambda target: calls.append(("reconcile", target)) or {"ready": True},
    )

    assert jobs.collect_kind_schedule_for_subscription_d_minus_one() == {"records": 1}
    assert jobs.collect_kind_listings_for_subscription_d_minus_one() == {"records": 1}
    assert jobs.collect_public_dart_for_subscription_d_minus_one() == {"success": 1}
    assert jobs.reconcile_subscription_d_minus_one_evidence() == {"ready": True}
    assert calls == [
        ("kind_schedule", date(2026, 8, 1), date(2026, 8, 31)),
        ("kind_listing", date(2026, 8, 1)),
        ("dart", date(2026, 8, 1)),
        ("reconcile", date(2026, 8, 1)),
    ]


def test_build_d_minus_one_batch_selects_only_tomorrow_and_fails_closed(db_session, monkeypatch):
    run_date = date(2026, 7, 19)
    generated = datetime(2026, 7, 19, 18, 30, tzinfo=ZoneInfo("Asia/Seoul"))

    def ipo(name, start, *, data_origin="kind", status="scheduled", is_sample=False):
        return Ipo(
            identity_key=f"identity:{name}",
            offering_id=f"offering:{name}",
            corp_name=name,
            normalized_name=name,
            data_origin=data_origin,
            status=status,
            is_sample=is_sample,
            subscription_start_date=start,
            subscription_end_date=start,
        )

    ready = ipo("가-준비", date(2026, 7, 20))
    bad_render = ipo("나-렌더실패", date(2026, 7, 20))
    missing_kind = ipo("다-KIND없음", date(2026, 7, 20))
    wrong_day = ipo("라-다른날", date(2026, 7, 21))
    sample = ipo("마-샘플", date(2026, 7, 20), data_origin="sample", is_sample=True)
    withdrawn = ipo("바-철회", date(2026, 7, 20), status="withdrawn")
    db_session.add_all((ready, bad_render, missing_kind, wrong_day, sample, withdrawn))
    db_session.flush()

    for row in (ready, bad_render):
        document = SourceDocument(
            ipo_id=row.id,
            source="kind",
            document_key=f"kind:{row.id}",
            document_type="kind_public_offering_schedule",
            lineage_root_key=f"kind:{row.id}",
        )
        db_session.add(document)
        db_session.flush()
        db_session.add(
            IpoFactObservation(
                observation_key=f"observation:{row.id}",
                ipo_id=row.id,
                source_document_id=document.id,
                field_name="subscription_period",
                value_json="2026-07-20",
                quality_state="reported",
                knowledge_phase="pre_subscription",
                observed_at=datetime(2026, 7, 19, 8, 0, tzinfo=timezone.utc),
                fetched_at=datetime(2026, 7, 19, 8, 0, tzinfo=timezone.utc),
            )
        )
    db_session.flush()
    captured = []

    @contextmanager
    def scope():
        yield db_session

    monkeypatch.setattr(jobs, "init_db", lambda: None)
    monkeypatch.setattr(jobs, "session_scope", scope)
    monkeypatch.setattr(
        jobs,
        "get_settings",
        lambda: SimpleNamespace(email_max_html_bytes=45_000),
    )

    def build(session, selected, **kwargs):
        captured.append(("build", session, selected.ipo_id, kwargs))
        return _bundle(
            selected.ipo_id,
            selected.ipo.corp_name,
            rendered_ipo_id=99 if selected.ipo_id == bad_render.id else None,
        )

    monkeypatch.setattr(jobs, "build_single_ipo_email", build)

    batch = jobs.build_subscription_d_minus_one_email_batch(run_date)

    assert batch.run_date == run_date
    assert batch.subscription_date == date(2026, 7, 20)
    assert batch.total_candidates == 3
    assert [item.corp_name for item in batch.ready] == ["가-준비"]
    assert [item["ipo_id"] for item in batch.blocked] == [bad_render.id, missing_kind.id]
    assert batch.blocked[0]["reason"] == jobs.SUBSCRIPTION_REPORT_REQUIRED_REASON
    assert batch.blocked[1] == {
        "ipo_id": missing_kind.id,
        "corp_name": "다-KIND없음",
        "reason": jobs.SUBSCRIPTION_EVIDENCE_REQUIRED_REASON,
        "expected_subscription_period": "2026-07-20",
        "observed_subscription_period": None,
    }
    build_calls = [item for item in captured if item[0] == "build"]
    assert [item[2] for item in build_calls] == [ready.id, bad_render.id]
    assert all(item[3]["generated_at"] == generated for item in build_calls)
    assert build_calls[0][3]["max_html_bytes"] == 45_000


def test_historical_send_date_cutoff_hides_future_kind_and_dart_observations(
    db_session, monkeypatch
):
    ipo = Ipo(
        identity_key="identity:pit",
        offering_id="offering:pit",
        corp_name="PIT기업",
        normalized_name="pit기업",
        data_origin="kind",
        status="scheduled",
        is_sample=False,
        subscription_start_date=date(2026, 7, 20),
        subscription_end_date=date(2026, 7, 20),
    )
    db_session.add(ipo)
    db_session.flush()
    prior_kind_document = SourceDocument(
        ipo_id=ipo.id,
        source="kind",
        document_key="kind:pit:prior",
        document_type="kind_public_offering_schedule",
        lineage_root_key="kind:pit",
        fetched_at=datetime(2026, 7, 19, 8, 0, tzinfo=timezone.utc),
    )
    future_kind_document = SourceDocument(
        ipo_id=ipo.id,
        source="kind",
        document_key="kind:pit:future",
        document_type="kind_public_offering_schedule",
        lineage_root_key="kind:pit",
        fetched_at=datetime(2026, 7, 20, 1, 0, tzinfo=timezone.utc),
    )
    dart_document = SourceDocument(
        ipo_id=ipo.id,
        source="dart_public",
        document_key="dart:pit:future",
        document_type="terms_finalization",
        lineage_root_key="dart:pit",
        fetched_at=datetime(2026, 7, 20, 1, 0, tzinfo=timezone.utc),
    )
    db_session.add_all((prior_kind_document, future_kind_document, dart_document))
    db_session.flush()
    db_session.add_all(
        (
            IpoFactObservation(
                observation_key="observation:pit:kind:prior",
                ipo_id=ipo.id,
                source_document_id=prior_kind_document.id,
                field_name="subscription_period",
                value_json="2026-07-20",
                quality_state="reported",
                knowledge_phase="pre_subscription",
                observed_at=datetime(2026, 7, 19, 8, 0, tzinfo=timezone.utc),
                fetched_at=datetime(2026, 7, 19, 8, 0, tzinfo=timezone.utc),
            ),
            IpoFactObservation(
                observation_key="observation:pit:kind:future",
                ipo_id=ipo.id,
                source_document_id=future_kind_document.id,
                field_name="subscription_period",
                value_json="2026-07-21",
                quality_state="reported",
                knowledge_phase="pre_subscription",
                observed_at=datetime(2026, 7, 20, 1, 0, tzinfo=timezone.utc),
                fetched_at=datetime(2026, 7, 20, 1, 0, tzinfo=timezone.utc),
            ),
            IpoFactObservation(
                observation_key="observation:pit:future-price",
                ipo_id=ipo.id,
                source_document_id=dart_document.id,
                field_name="offer_price_final",
                value_json=99_999_991,
                unit="원",
                quality_state="reported",
                knowledge_phase="during_subscription",
                observed_at=datetime(2026, 7, 20, 1, 0, tzinfo=timezone.utc),
                fetched_at=datetime(2026, 7, 20, 1, 0, tzinfo=timezone.utc),
            ),
        )
    )
    db_session.flush()

    @contextmanager
    def scope():
        yield db_session

    monkeypatch.setattr(jobs, "init_db", lambda: None)
    monkeypatch.setattr(jobs, "session_scope", scope)

    batch = jobs.build_subscription_d_minus_one_email_batch(date(2026, 7, 19))

    assert len(batch.ready) == 1
    assert batch.ready[0].bundle.read_model.generated_at == datetime(
        2026, 7, 19, 18, 30, tzinfo=ZoneInfo("Asia/Seoul")
    )
    rendered = batch.ready[0].bundle.parts[0]
    assert jobs.SUBSCRIPTION_RECONCILIATION_STATE in rendered.text
    assert "99,999,991" not in rendered.text
    assert "99,999,991" not in rendered.html


@pytest.mark.parametrize(
    ("run_date", "subscription_date"),
    [
        (date(2026, 7, 31), date(2026, 8, 1)),
        (date(2026, 12, 31), date(2027, 1, 1)),
        (date(2028, 2, 28), date(2028, 2, 29)),
    ],
)
def test_d_minus_one_date_boundaries(db_session, monkeypatch, run_date, subscription_date):
    @contextmanager
    def scope():
        yield db_session

    monkeypatch.setattr(jobs, "init_db", lambda: None)
    monkeypatch.setattr(jobs, "session_scope", scope)

    batch = jobs.build_subscription_d_minus_one_email_batch(run_date)

    assert batch.run_date == run_date
    assert batch.subscription_date == subscription_date
    assert batch.total_candidates == 0


def test_one_report_build_failure_does_not_block_next_ipo(db_session, monkeypatch):
    ipos = []
    for index, name in enumerate(("가-렌더실패", "나-정상"), 1):
        ipo = Ipo(
            identity_key=f"identity:render:{index}",
            offering_id=f"offering:render:{index}",
            corp_name=name,
            normalized_name=name,
            data_origin="kind",
            status="scheduled",
            is_sample=False,
            subscription_start_date=date(2026, 7, 20),
            subscription_end_date=date(2026, 7, 20),
        )
        db_session.add(ipo)
        db_session.flush()
        document = SourceDocument(
            ipo_id=ipo.id,
            source="kind",
            document_key=f"kind:render:{index}",
            document_type="kind_public_offering_schedule",
            lineage_root_key=f"kind:render:{index}",
        )
        db_session.add(document)
        db_session.flush()
        db_session.add(
            IpoFactObservation(
                observation_key=f"observation:render:{index}",
                ipo_id=ipo.id,
                source_document_id=document.id,
                field_name="subscription_period",
                value_json="2026-07-20",
                quality_state="reported",
                knowledge_phase="pre_subscription",
                observed_at=datetime(2026, 7, 19, 8, 0, tzinfo=timezone.utc),
                fetched_at=datetime(2026, 7, 19, 8, 0, tzinfo=timezone.utc),
            )
        )
        ipos.append(ipo)
    db_session.flush()

    @contextmanager
    def scope():
        yield db_session

    monkeypatch.setattr(jobs, "init_db", lambda: None)
    monkeypatch.setattr(jobs, "session_scope", scope)

    def build(_session, entry, **_kwargs):
        if entry.ipo_id == ipos[0].id:
            raise RuntimeError("password=must-not-leak")
        return _bundle(entry.ipo_id, entry.ipo.corp_name)

    monkeypatch.setattr(jobs, "build_single_ipo_email", build)

    batch = jobs.build_subscription_d_minus_one_email_batch(date(2026, 7, 19))

    assert [candidate.ipo_id for candidate in batch.ready] == [ipos[1].id]
    assert batch.blocked == (
        {
            "ipo_id": ipos[0].id,
            "corp_name": "가-렌더실패",
            "reason": "종목별 리포트 생성 실패",
            "error": "RuntimeError: report build failed",
        },
    )
    assert "must-not-leak" not in str(batch)


@pytest.mark.parametrize(
    ("bundle", "ipo_id", "expected"),
    [
        (_bundle(1, "정상"), 1, True),
        (_bundle(1, "상태", reconciliation_state="reported"), 1, False),
        (_bundle(1, "다중", parts=2), 1, False),
        (_bundle(1, "식별", rendered_ipo_id=2), 1, False),
    ],
)
def test_single_ipo_bundle_send_gate(bundle, ipo_id, expected):
    assert jobs._single_ipo_bundle_is_sendable(bundle, ipo_id) is expected

    no_core = _bundle(ipo_id, "없음")
    no_core.read_model.core = ()
    assert jobs._single_ipo_bundle_is_sendable(no_core, ipo_id) is False

    appendix = _bundle(ipo_id, "부록")
    appendix.read_model.filing_appendix = (object(),)
    assert jobs._single_ipo_bundle_is_sendable(appendix, ipo_id) is False


def test_two_d_minus_one_ipos_send_two_stable_independent_messages(monkeypatch):
    first_candidate = _candidate(11, "첫째", warnings=("기관 가격분포",))
    second_candidate = _candidate(12, "둘째")
    batch = jobs._SubscriptionEmailBatch(
        run_date=date(2026, 7, 19),
        subscription_date=date(2026, 7, 20),
        total_candidates=2,
        ready=(first_candidate, second_candidate),
        blocked=(),
    )
    changed_first = _candidate(11, "첫째", warnings=("기관 가격분포",))
    changed_second = _candidate(12, "둘째")
    changed_first.bundle.universe_hash = "c" * 64
    changed_first.bundle.evidence_hash = "d" * 64
    changed_second.bundle.universe_hash = "e" * 64
    changed_second.bundle.evidence_hash = "f" * 64
    changed_batch = jobs._SubscriptionEmailBatch(
        run_date=batch.run_date,
        subscription_date=batch.subscription_date,
        total_candidates=2,
        ready=(changed_first, changed_second),
        blocked=(),
    )
    batches = iter((batch, changed_batch))
    monkeypatch.setattr(
        jobs,
        "build_subscription_d_minus_one_email_batch",
        lambda _target: next(batches),
    )
    seen: set[str] = set()
    calls = []

    def send(content, *, event_key, recipients, audit_context):
        calls.append((content, event_key, recipients, audit_context))
        if event_key in seen:
            return {"processed": 1, "duplicate": 1, "failures": []}
        seen.add(event_key)
        return {"processed": 1, "sent": 1, "failures": []}

    monkeypatch.setattr(jobs, "send_email_message", send)

    first = jobs.send_subscription_d_minus_one_emails(date(2026, 7, 19))
    second = jobs.send_subscription_d_minus_one_emails(date(2026, 7, 19))

    assert (first["processed"], first["sent"], first["items"]) == (2, 2, 2)
    assert (second["processed"], second["duplicate"]) == (2, 2)
    first_keys = [call[1] for call in calls[:2]]
    assert first_keys == [call[1] for call in calls[2:]]
    assert first_keys[0] != first_keys[1]
    assert first_keys == [
        "subscription-d1-email:v3-facts-d1:2026-07-20:"
        + jobs.hashlib.sha256(b"offering:11").hexdigest()[:20],
        "subscription-d1-email:v3-facts-d1:2026-07-20:"
        + jobs.hashlib.sha256(b"offering:12").hexdigest()[:20],
    ]
    assert all(len(key) < 255 for key in first_keys)
    assert all(call[2] == (DEFAULT_EMAIL_RECIPIENT,) for call in calls)
    assert first["evidence_warnings"] == [
        {"ipo_id": 11, "corp_name": "첫째", "missing_labels": ["기관 가격분포"]}
    ]
    assert calls[0][3].metadata == {
        "product": jobs.SUBSCRIPTION_EMAIL_EVENT_VERSION,
        "trigger": "subscription_d_minus_one",
        "ipo_id": 11,
        "offering_id": "offering:11",
        "corp_name": "첫째",
        "run_date": "2026-07-19",
        "send_date": "2026-07-19",
        "subscription_start_date": "2026-07-20",
        "universe_hash": first_candidate.bundle.universe_hash,
        "evidence_hash": first_candidate.bundle.evidence_hash,
        "missing_labels": ["기관 가격분포"],
    }


def test_one_transport_exception_does_not_block_the_next_ipo(monkeypatch):
    batch = jobs._SubscriptionEmailBatch(
        run_date=date(2026, 7, 19),
        subscription_date=date(2026, 7, 20),
        total_candidates=2,
        ready=(_candidate(21, "실패"), _candidate(22, "성공")),
        blocked=(),
    )
    monkeypatch.setattr(
        jobs,
        "build_subscription_d_minus_one_email_batch",
        lambda _target: batch,
    )
    calls = []

    def send(content, **_kwargs):
        calls.append(content.subject)
        if "실패" in content.subject:
            raise RuntimeError("SMTP password=must-not-leak")
        return {"processed": 1, "sent": 1, "failures": []}

    monkeypatch.setattr(jobs, "send_email_message", send)

    result = jobs.send_subscription_d_minus_one_emails()

    assert calls == [
        "[Korea IPO] 실패 일반청약 D-1",
        "[Korea IPO] 성공 일반청약 D-1",
    ]
    assert (result["processed"], result["failed"], result["sent"]) == (2, 1, 1)
    assert result["failures"] == [
        {
            "ipo_id": 21,
            "corp_name": "실패",
            "error": "RuntimeError: email send failed",
        }
    ]
    assert "must-not-leak" not in str(result)


def test_transport_level_skip_is_preserved_in_d_minus_one_aggregate(monkeypatch):
    batch = jobs._SubscriptionEmailBatch(
        run_date=date(2026, 7, 19),
        subscription_date=date(2026, 7, 20),
        total_candidates=1,
        ready=(_candidate(31, "설정대기"),),
        blocked=(),
    )
    monkeypatch.setattr(
        jobs,
        "build_subscription_d_minus_one_email_batch",
        lambda _target: batch,
    )
    monkeypatch.setattr(
        jobs,
        "send_email_message",
        lambda *_args, **_kwargs: {
            "processed": 0,
            "sent": 0,
            "skipped": True,
            "reason": "Email is not configured",
        },
    )

    result = jobs.send_subscription_d_minus_one_emails()

    assert result["skipped"] is True
    assert result["reason"] == "Email is not configured"


def test_d_minus_one_empty_and_all_blocked_skip_before_smtp(monkeypatch):
    monkeypatch.setattr(
        jobs,
        "send_email_message",
        lambda *_args, **_kwargs: pytest.fail("blocked batch must not send"),
    )
    empty = jobs._SubscriptionEmailBatch(
        run_date=date(2026, 7, 19),
        subscription_date=date(2026, 7, 20),
        total_candidates=0,
        ready=(),
        blocked=(),
    )
    monkeypatch.setattr(
        jobs,
        "build_subscription_d_minus_one_email_batch",
        lambda _target: empty,
    )
    result = jobs.send_subscription_d_minus_one_emails()
    assert result["skipped"] is True
    assert result["reason"] == jobs.NO_D_MINUS_ONE_ITEMS_REASON

    blocked = jobs._SubscriptionEmailBatch(
        run_date=date(2026, 7, 19),
        subscription_date=date(2026, 7, 20),
        total_candidates=1,
        ready=(),
        blocked=({"ipo_id": 1, "reason": "unreconciled"},),
    )
    monkeypatch.setattr(
        jobs,
        "build_subscription_d_minus_one_email_batch",
        lambda _target: blocked,
    )
    result = jobs.send_subscription_d_minus_one_emails()
    assert result["skipped"] is True
    assert result["blocked"] == 1
    assert result["reason"] == jobs.D_MINUS_ONE_ALL_BLOCKED_REASON


def test_v3_daily_routes_to_d_minus_one_and_weekly_is_noop(monkeypatch):
    monkeypatch.setattr(
        jobs,
        "get_settings",
        lambda: SimpleNamespace(email_product_mode="v3-facts"),
    )
    calls = []
    monkeypatch.setattr(
        jobs,
        "send_subscription_d_minus_one_emails",
        lambda: calls.append("d1") or {"sent": 2},
    )

    assert jobs.send_daily_email_summary() == {"sent": 2}
    weekly = jobs.send_weekly_email_summary()

    assert calls == ["d1"]
    assert weekly["skipped"] is True
    assert weekly["reason"] == jobs.WEEKLY_V3_SKIP_REASON
