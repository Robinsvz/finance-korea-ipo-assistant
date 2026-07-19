from datetime import date, datetime, timezone
from types import SimpleNamespace

from app.models import Ipo, ScoreSnapshot
from app.services import decision_email
from app.services.decision import DecisionAssessment
from app.services.decision_email import (
    _readiness_note,
    _validation_summary,
    build_period_decision_email,
    build_schedule_email_items,
    decision_email_item,
)
from app.services.scoring_v3 import load_simple_scoring_rules


def _assessment(**overrides):
    values = {
        "action": "CANDIDATE",
        "score": 82,
        "confidence": 91,
        "broker_name": "한국투자증권",
        "expected_shares": 1.2,
        "break_even_return_pct": 1.5,
        "expected_net_profit": 20_000,
        "positive_probability": 0.7,
        "benchmark_median_return_pct": 20,
        "validation_sample_count": 31,
        "validation_status": "validated",
        "reasons": ("근거",),
        "risks": ("위험",),
    }
    values.update(overrides)
    return DecisionAssessment(**values)


def test_decision_email_item_maps_execution_and_validation_values():
    ipo = Ipo(
        corp_name="판단기업",
        normalized_name="판단기업",
        offer_price_max=10_000,
        subscription_start_date=date(2026, 7, 20),
        subscription_end_date=date(2026, 7, 21),
        refund_date=date(2026, 7, 23),
        actual_listing_date=date(2026, 7, 30),
    )
    score = ScoreSnapshot(
        ipo_id=1,
        score_date=date(2026, 7, 19),
        total_score=82,
        confidence_score=91,
        recommendation_band="Strong",
    )

    item = decision_email_item(ipo, score, _assessment(), 30)

    assert item.decision == "청약 후보"
    assert item.next_action.startswith("증권사별")
    assert item.offer_price == 10_000
    assert item.listing_date == date(2026, 7, 30)
    assert item.expected_net_profit == 20_000
    assert item.validation_summary == "동일 등급 n=31 / D1 양수 70.0% / D1 중앙값 20.0%"
    unknown = decision_email_item(
        ipo,
        None,
        _assessment(
            action="CUSTOM",
            positive_probability=None,
            validation_status="custom",
        ),
        30,
    )
    assert unknown.decision == "CUSTOM"
    assert unknown.next_action == "공모 일정과 공시 원문을 직접 확인하세요."
    assert unknown.total_score is None


def test_validation_summary_covers_unvalidated_states():
    insufficient = _assessment(
        positive_probability=None,
        benchmark_median_return_pct=None,
        validation_sample_count=2,
        validation_status="insufficient-sample",
    )
    incomplete = _assessment(
        positive_probability=None,
        benchmark_median_return_pct=None,
        validation_status="incomplete-outcomes",
    )
    no_median = _assessment(benchmark_median_return_pct=None)

    assert "2/30" in _validation_summary(insufficient, 30)
    assert "성과값 불완전" in _validation_summary(incomplete, 30)
    assert _validation_summary(no_median, 30).endswith("D1 양수 70.0%")


def test_readiness_note_covers_ready_and_blocked_states():
    ready = SimpleNamespace(validation_ready=True, complete_pair_count=30)
    blocked = SimpleNamespace(
        validation_ready=False,
        complete_pair_count=0,
        minimum_samples=30,
        blockers=("피처 부족",),
    )
    assert "30건" in _readiness_note(ready)
    assert "0/30" in _readiness_note(blocked)
    assert "피처 부족" not in _readiness_note(blocked)


def test_period_email_builds_missing_score_fail_closed(db_session):
    ipo = Ipo(
        corp_name="미수집기업",
        normalized_name="미수집기업",
        status="scheduled",
        subscription_start_date=date(2026, 7, 20),
    )
    db_session.add(ipo)
    db_session.flush()

    content = build_period_decision_email(
        db_session,
        [ipo],
        user_cash=10_000_000,
        title="오늘 IPO 판단",
        period_label="2026-07-20",
        generated_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
    )

    assert "데이터 부족" in content.text
    assert "검증 표본 0/30 확보 중" in content.text
    assert "확률·기대수익 미제공" in content.text


def test_period_email_reuses_model_report(monkeypatch, db_session):
    ipos = [
        Ipo(id=1, corp_name="A", normalized_name="a"),
        Ipo(id=2, corp_name="B", normalized_name="b"),
    ]
    score = ScoreSnapshot(
        ipo_id=1,
        score_date=date(2026, 7, 19),
        total_score=80,
        confidence_score=90,
        recommendation_band="Strong",
        model_version="rules-v3-simple",
        rules_hash="same",
    )
    report = SimpleNamespace(marker="report")
    calls = []
    monkeypatch.setattr(
        decision_email,
        "assess_data_readiness",
        lambda *_args, **_kwargs: SimpleNamespace(
            validation_ready=True,
            complete_pair_count=30,
        ),
    )
    monkeypatch.setattr(
        decision_email,
        "latest_actionable_score",
        lambda *_args, **_kwargs: score,
    )

    def build_report(*_args, **kwargs):
        assert kwargs["validation_eligible_only"] is True
        calls.append("build")
        return report

    monkeypatch.setattr(decision_email, "build_backtest_report", build_report)

    def assess(*_args, **kwargs):
        assert kwargs["report"] is report
        return _assessment()

    monkeypatch.setattr(decision_email, "assess_ipo_decision", assess)

    content = build_period_decision_email(
        db_session,
        ipos,
        user_cash=10_000_000,
        title="주간 판단",
        period_label="이번 주",
        generated_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
        model_version="rules-v3-simple",
        rules_hash="same",
    )

    assert calls == ["build"]
    assert content.text.count("청약 후보") == 2


def test_period_email_blocks_value_outputs_until_data_readiness(monkeypatch, db_session):
    ipo = Ipo(id=1, corp_name="준비중", normalized_name="준비중")
    score = ScoreSnapshot(
        ipo_id=1,
        score_date=date(2026, 7, 19),
        total_score=95,
        confidence_score=99,
        recommendation_band="Strong",
        model_version="rules-v3-simple",
        rules_hash="same",
    )
    captured = {}
    monkeypatch.setattr(
        decision_email,
        "assess_data_readiness",
        lambda *_args, **_kwargs: SimpleNamespace(
            validation_ready=False,
            complete_pair_count=0,
            minimum_samples=30,
        ),
    )
    monkeypatch.setattr(decision_email, "latest_actionable_score", lambda *_a, **_k: score)
    monkeypatch.setattr(
        decision_email,
        "build_backtest_report",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must stay fail-closed")),
    )

    def assess(*_args, **kwargs):
        captured["report"] = kwargs["report"]
        return _assessment(
            action="RESEARCH_ONLY",
            positive_probability=None,
            expected_net_profit=None,
            validation_status="data-readiness-blocked",
            validation_sample_count=0,
        )

    monkeypatch.setattr(decision_email, "assess_ipo_decision", assess)

    content = build_period_decision_email(
        db_session,
        [ipo],
        user_cash=10_000_000,
        title="판단",
        period_label="오늘",
        generated_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
        model_version="rules-v3-simple",
        rules_hash="same",
    )

    assert captured["report"].validation_status == "data-readiness-blocked"
    assert "검증 대기" in content.text
    assert "D1 양수확률" not in content.text
    assert "중앙값 기준 순이익" not in content.text


def test_schedule_items_keep_non_decision_events_and_sort_them():
    first = Ipo(
        corp_name="상장기업",
        normalized_name="상장기업",
        status="listed",
        demand_start_date=date(2026, 7, 19),
        refund_date=date(2026, 7, 22),
        listing_date=date(2026, 7, 24),
    )
    second = Ipo(
        corp_name="청약기업",
        normalized_name="청약기업",
        subscription_start_date=date(2026, 7, 20),
        subscription_end_date=date(2026, 7, 21),
        payment_date=date(2026, 7, 23),
    )

    items = build_schedule_email_items([first, second], date(2026, 7, 20), date(2026, 7, 24))

    assert [(item.event_label, item.corp_name) for item in items] == [
        ("청약 시작", "청약기업"),
        ("청약 마감", "청약기업"),
        ("환불", "상장기업"),
        ("납입", "청약기업"),
        ("상장", "상장기업"),
    ]


def test_period_email_uses_only_the_active_v3_lineage(monkeypatch, db_session):
    rules = load_simple_scoring_rules()
    ipo = Ipo(
        corp_name="계보기업",
        normalized_name="계보기업",
        status="scheduled",
        subscription_start_date=date(2026, 7, 20),
    )
    db_session.add(ipo)
    db_session.flush()
    active = ScoreSnapshot(
        ipo_id=ipo.id,
        score_date=date(2026, 7, 19),
        calculated_at=datetime(2026, 7, 18, tzinfo=timezone.utc),
        total_score=71,
        confidence_score=90,
        recommendation_band="Watch",
        model_version=rules.model_version,
        rules_hash=rules.rules_hash,
    )
    wrong_hash = ScoreSnapshot(
        ipo_id=ipo.id,
        score_date=date(2026, 7, 19),
        calculated_at=datetime(2026, 7, 19, 22, tzinfo=timezone.utc),
        total_score=98,
        confidence_score=100,
        recommendation_band="Strong",
        model_version=rules.model_version,
        rules_hash="superseded-hash",
    )
    legacy = ScoreSnapshot(
        ipo_id=ipo.id,
        score_date=date(2026, 7, 19),
        calculated_at=datetime(2026, 7, 19, 23, tzinfo=timezone.utc),
        total_score=99,
        confidence_score=100,
        recommendation_band="Strong",
        model_version="rules-v2",
        rules_hash="legacy-hash",
    )
    db_session.add_all([active, wrong_hash, legacy])
    db_session.flush()
    captured = {}

    def readiness(_session, **kwargs):
        captured["readiness"] = kwargs
        return SimpleNamespace(validation_ready=True, complete_pair_count=30)

    def backtest(_session, **kwargs):
        captured["backtest"] = kwargs
        return SimpleNamespace(marker="active-report")

    def assess(_session, _ipo, score, **kwargs):
        captured["score"] = score
        captured["report"] = kwargs["report"]
        return _assessment()

    monkeypatch.setattr(decision_email, "assess_data_readiness", readiness)
    monkeypatch.setattr(decision_email, "build_backtest_report", backtest)
    monkeypatch.setattr(decision_email, "assess_ipo_decision", assess)

    build_period_decision_email(
        db_session,
        [ipo],
        user_cash=10_000_000,
        title="계보 판단",
        period_label="2026-07-20",
        generated_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
    )

    assert captured["score"].id == active.id
    assert captured["score"].id not in {wrong_hash.id, legacy.id}
    assert captured["readiness"]["model_version"] == rules.model_version
    assert captured["readiness"]["rules_hash"] == rules.rules_hash
    assert captured["backtest"]["model_version"] == rules.model_version
    assert captured["backtest"]["rules_hash"] == rules.rules_hash
    assert captured["report"].marker == "active-report"
