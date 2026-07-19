from datetime import date, datetime, timezone

import pytest

from app.models import (
    DemandForecast,
    Ipo,
    IpoStructureMetric,
    ListingPrice,
    OutcomeEvaluation,
    ScoreSnapshot,
)
from app.services.validation import (
    _source_was_available,
    assess_data_readiness,
    build_validation_dataset,
    is_prelisting_score,
    write_validation_csv,
)


def _complete_case(session, *, name="검증기업", identity="kind:1", model="rules-v3-simple"):
    listing_date = date(2026, 7, 20)
    ipo = Ipo(
        identity_key=identity,
        corp_name=name,
        normalized_name=name,
        status="listed",
        ticker="123456",
        offer_price_final=10_000,
        listing_date=listing_date,
    )
    session.add(ipo)
    session.flush()
    session.add_all(
        [
            DemandForecast(
                ipo_id=ipo.id,
                receipt_no=f"R-{ipo.id}",
                institutional_competition_ratio=1_000,
            ),
            IpoStructureMetric(
                ipo_id=ipo.id,
                as_of_date=date(2026, 7, 18),
                source="test",
                free_float_ratio_on_listing=0.2,
            ),
            ListingPrice(
                ipo_id=ipo.id,
                ticker="123456",
                listing_date=listing_date,
                source="test",
                d0_open_price=12_000,
                d1_close_price=11_000,
            ),
        ]
    )
    score = ScoreSnapshot(
        ipo_id=ipo.id,
        score_date=date(2026, 7, 18),
        calculated_at=datetime(2026, 7, 18, tzinfo=timezone.utc),
        total_score=75,
        demand_score=35,
        structure_score=25,
        valuation_score=10,
        market_condition_score=5,
        confidence_score=90,
        recommendation_band="Strong",
        model_version=model,
        rules_hash="hash-1",
        explanation_json={
            "gates": {"status": "pass"},
            "sources": {
                "demand": {"source": "test", "as_of": "2026-07-17T00:00:00+00:00"},
                "supply": {"source": "test", "as_of": "2026-07-18"},
            },
        },
    )
    session.add(score)
    session.flush()
    session.add(
        OutcomeEvaluation(
            ipo_id=ipo.id,
            score_snapshot_id=score.id,
            evaluation_version=f"outcome-{ipo.id}",
            d0_open_return_pct=20,
            d1_close_return_pct=10,
            estimated_profit_per_share_open=2_000,
            estimated_profit_per_share_d1=1_000,
        )
    )
    session.flush()
    return ipo, score


def test_data_readiness_counts_real_rows_and_excludes_sample(db_session):
    _complete_case(db_session)
    future_ipo, _future_score = _complete_case(
        db_session,
        name="기준일이후기업",
        identity="kind:future",
    )
    future_ipo.listing_date = date(2026, 7, 21)
    db_session.add(
        Ipo(
            identity_key="sample:ABC",
            corp_name="ABC",
            normalized_name="abc",
            status="scheduled",
            is_sample=True,
            data_origin="synthetic",
        )
    )
    db_session.flush()

    readiness = assess_data_readiness(
        db_session,
        model_version="rules-v3-simple",
        minimum_samples=1,
        as_of_date=date(2026, 7, 20),
    )

    assert readiness.validation_ready is True
    assert readiness.to_dict()["real_ipo_count"] == 2
    assert readiness.listed_count == 1
    assert readiness.sample_ipo_count == 1
    assert readiness.complete_pair_count == 1
    assert readiness.blockers == ()


def test_data_readiness_surfaces_coverage_blockers(db_session):
    ipo = Ipo(
        identity_key="kind:missing",
        corp_name="미완성",
        normalized_name="미완성",
        status="listed",
        listing_date=date(2026, 7, 20),
    )
    db_session.add(ipo)
    db_session.flush()

    readiness = assess_data_readiness(
        db_session,
        model_version="rules-v3-simple",
        minimum_samples=30,
        as_of_date=date(2026, 7, 20),
    )

    assert readiness.validation_ready is False
    assert readiness.as_of_date == date(2026, 7, 20)
    assert len(readiness.blockers) == 4
    assert readiness.listing_price_count == 0
    assert readiness.eligible_score_count == 0


def test_readiness_never_mixes_rule_hashes(db_session):
    _complete_case(db_session, name="첫규칙", identity="kind:rule-1")
    _ipo, second = _complete_case(db_session, name="둘째규칙", identity="kind:rule-2")
    second.rules_hash = "hash-2"
    db_session.flush()

    mixed = assess_data_readiness(
        db_session,
        model_version="rules-v3-simple",
        minimum_samples=2,
        as_of_date=date(2026, 7, 20),
    )
    selected = assess_data_readiness(
        db_session,
        model_version="rules-v3-simple",
        rules_hash="hash-1",
        minimum_samples=1,
        as_of_date=date(2026, 7, 20),
    )

    assert mixed.validation_ready is False
    assert mixed.complete_pair_count == 1
    assert mixed.eligible_rules_hash_count == 2
    assert any("rules_hash" in blocker for blocker in mixed.blockers)
    assert selected.validation_ready is True
    assert selected.complete_pair_count == 1
    assert selected.selected_rules_hash == "hash-1"


def test_readiness_requires_passed_point_in_time_gate_and_all_coverage(db_session):
    _ipo, score = _complete_case(db_session)
    score.rules_hash = None
    db_session.flush()
    missing_hash = assess_data_readiness(
        db_session,
        model_version="rules-v3-simple",
        minimum_samples=1,
        as_of_date=date(2026, 7, 20),
    )
    assert missing_hash.complete_pair_count == 0

    score.rules_hash = "hash-1"
    score.explanation_json = {"gates": {"status": "insufficient_data"}}
    db_session.flush()

    gated = assess_data_readiness(
        db_session,
        model_version="rules-v3-simple",
        minimum_samples=1,
        as_of_date=date(2026, 7, 20),
    )
    assert gated.validation_ready is False
    assert gated.complete_pair_count == 0

    score.explanation_json = {
        "gates": {"status": "pass"},
        "sources": {
            "demand": {"source": "test", "as_of": "not-a-date"},
            "supply": {"source": "test", "as_of": "2026-07-18"},
        },
    }
    db_session.flush()
    malformed = assess_data_readiness(
        db_session,
        model_version="rules-v3-simple",
        minimum_samples=1,
        as_of_date=date(2026, 7, 20),
    )
    assert malformed.validation_ready is False
    assert malformed.complete_pair_count == 0

    score.explanation_json = {
        "gates": {"status": "pass"},
        "sources": {
            "demand": {"source": "test", "as_of": "2026-07-17T00:00:00Z"},
            "supply": {"source": "test", "as_of": "2026-07-18"},
        },
    }
    db_session.query(DemandForecast).delete()
    db_session.flush()
    incomplete = assess_data_readiness(
        db_session,
        model_version="rules-v3-simple",
        minimum_samples=1,
        as_of_date=date(2026, 7, 20),
    )
    assert incomplete.complete_pair_count == 1
    assert incomplete.validation_ready is False
    assert any("수요예측" in blocker for blocker in incomplete.blockers)


def test_readiness_ignores_incomplete_or_unpaired_evaluations(db_session):
    ipo, score = _complete_case(db_session)
    db_session.add_all(
        [
            OutcomeEvaluation(
                ipo_id=ipo.id,
                score_snapshot_id=None,
                evaluation_version="unpaired",
                d0_open_return_pct=1,
                d1_close_return_pct=1,
            ),
            OutcomeEvaluation(
                ipo_id=ipo.id,
                score_snapshot_id=score.id,
                evaluation_version="missing-d0",
                d1_close_return_pct=1,
            ),
            OutcomeEvaluation(
                ipo_id=ipo.id,
                score_snapshot_id=score.id,
                evaluation_version="missing-d1",
                d0_open_return_pct=1,
            ),
        ]
    )
    db_session.flush()

    readiness = assess_data_readiness(
        db_session,
        model_version="rules-v3-simple",
        minimum_samples=1,
        as_of_date=date(2026, 7, 20),
    )
    assert readiness.complete_pair_count == 1


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (None, False),
        ({"source": "", "as_of": "2026-07-17"}, False),
        ({"source": "test"}, False),
        ({"source": "test", "as_of": "invalid"}, False),
        ({"source": "test", "as_of": "2026-07-19"}, False),
        ({"source": "test", "as_of": "2026-07-18"}, True),
        ({"source": "test", "as_of": "2026-07-18T00:00:00Z"}, True),
        ({"source": "test", "as_of": "2026-07-19T00:00:00Z"}, False),
    ],
)
def test_validation_source_availability_boundaries(payload, expected):
    assert _source_was_available(payload, datetime(2026, 7, 18, 1, tzinfo=timezone.utc)) is expected


def test_readiness_rejects_invalid_minimum(db_session):
    with pytest.raises(ValueError, match="positive"):
        assess_data_readiness(db_session, model_version="x", minimum_samples=0)


def test_validation_dataset_filters_model_hash_samples_and_late_scores(db_session):
    _complete_case(db_session, name="운영스팩", identity="kind:spac")
    sample_ipo, _sample_score = _complete_case(
        db_session,
        name="샘플회사",
        identity="sample:historical",
        model="rules-v3-simple",
    )
    sample_ipo.is_sample = True
    sample_ipo.data_origin = "synthetic"
    late_ipo, late_score = _complete_case(
        db_session,
        name="늦은점수",
        identity="kind:late",
        model="rules-v3-simple",
    )
    late_score.calculated_at = datetime(2026, 7, 21, tzinfo=timezone.utc)
    late_ipo.actual_listing_date = date(2026, 7, 20)
    _invalid_ipo, invalid_score = _complete_case(
        db_session,
        name="게이트미통과",
        identity="kind:invalid-gate",
    )
    invalid_score.explanation_json = {"gates": {"status": "insufficient_data"}}
    _incomplete_ipo, incomplete_score = _complete_case(
        db_session,
        name="성과미완성",
        identity="kind:incomplete-outcome",
    )
    db_session.query(OutcomeEvaluation).filter_by(
        score_snapshot_id=incomplete_score.id
    ).one().d0_open_return_pct = None
    _incomplete_d1_ipo, incomplete_d1_score = _complete_case(
        db_session,
        name="D1성과미완성",
        identity="kind:incomplete-d1-outcome",
    )
    db_session.query(OutcomeEvaluation).filter_by(
        score_snapshot_id=incomplete_d1_score.id
    ).one().d1_close_return_pct = None
    db_session.flush()

    rows = build_validation_dataset(
        db_session,
        model_version="rules-v3-simple",
        rules_hash="hash-1",
    )

    assert len(rows) == 1
    assert rows[0].cohort == "spac"
    assert rows[0].decision_policy == "subscription-open-09-kst"
    assert rows[0].decision_date == date(2026, 7, 20)
    assert rows[0].to_dict()["listing_date"] == "2026-07-20"
    assert (
        build_validation_dataset(
            db_session,
            model_version="rules-v3-simple",
            rules_hash="other",
        )
        == []
    )


def test_validation_csv_writes_header_and_rows(tmp_path, db_session):
    _complete_case(db_session)
    rows = build_validation_dataset(db_session, model_version="rules-v3-simple")
    output = tmp_path / "nested" / "validation.csv"

    assert write_validation_csv(rows, output) == output
    content = output.read_text(encoding="utf-8-sig")
    assert "corp_name" in content
    assert "검증기업" in content
    assert "decision_policy" in content
    with pytest.raises(ValueError, match=".csv"):
        write_validation_csv([], tmp_path / "bad.txt")


def test_prelisting_score_requires_date_order_and_handles_naive_utc():
    score = ScoreSnapshot(
        ipo_id=1,
        score_date=date(2026, 7, 19),
        calculated_at=datetime(2026, 7, 18, 23),
        total_score=1,
        recommendation_band="Avoid",
    )
    assert is_prelisting_score(score, None) is False
    assert is_prelisting_score(score, date(2026, 7, 18)) is False
    score.score_date = date(2026, 7, 18)
    assert is_prelisting_score(score, date(2026, 7, 20)) is True
    score.calculated_at = datetime(2026, 7, 20, tzinfo=timezone.utc)
    assert is_prelisting_score(score, date(2026, 7, 20)) is False


def test_validation_dataset_keeps_one_latest_score_per_ipo_model_and_rules(db_session):
    ipo, first = _complete_case(db_session)
    second = ScoreSnapshot(
        ipo_id=ipo.id,
        score_date=date(2026, 7, 19),
        calculated_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
        total_score=81,
        recommendation_band="Strong",
        confidence_score=92,
        model_version="rules-v3-simple",
        rules_hash="hash-1",
        explanation_json=first.explanation_json,
    )
    db_session.add(second)
    db_session.flush()
    db_session.add(
        OutcomeEvaluation(
            ipo_id=ipo.id,
            score_snapshot_id=second.id,
            evaluation_version="second",
            d0_open_return_pct=21,
            d1_close_return_pct=11,
        )
    )
    db_session.flush()

    rows = build_validation_dataset(db_session, model_version="rules-v3-simple")

    assert len(rows) == 1
    assert rows[0].total_score == 81
    assert rows[0].d1_close_return_pct == 11
    assert first.id != second.id


def test_validation_dataset_ignores_older_candidate_seen_after_newer():
    ipo = Ipo(
        id=1,
        corp_name="역순후보",
        normalized_name="역순후보",
        status="listed",
        listing_date=date(2026, 7, 20),
    )
    newer_score = ScoreSnapshot(
        id=2,
        ipo_id=1,
        score_date=date(2026, 7, 19),
        calculated_at=datetime(2026, 7, 19, 1, tzinfo=timezone.utc),
        total_score=80,
        recommendation_band="Strong",
        model_version="rules-v3-simple",
        rules_hash="hash",
        explanation_json={
            "gates": {"status": "pass"},
            "sources": {
                "demand": {"source": "test", "as_of": "2026-07-18"},
                "supply": {"source": "test", "as_of": "2026-07-18"},
            },
        },
    )
    older_score = ScoreSnapshot(
        id=1,
        ipo_id=1,
        score_date=date(2026, 7, 19),
        calculated_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
        total_score=70,
        recommendation_band="Strong",
        model_version="rules-v3-simple",
        rules_hash="hash",
        explanation_json={
            "gates": {"status": "pass"},
            "sources": {
                "demand": {"source": "test", "as_of": "2026-07-18"},
                "supply": {"source": "test", "as_of": "2026-07-18"},
            },
        },
    )
    newer_evaluation = OutcomeEvaluation(
        id=2,
        ipo_id=1,
        evaluated_at=datetime(2026, 7, 21, 1, tzinfo=timezone.utc),
        evaluation_version="newer",
        d0_open_return_pct=30,
        d1_close_return_pct=20,
    )
    older_evaluation = OutcomeEvaluation(
        id=1,
        ipo_id=1,
        evaluated_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
        evaluation_version="older",
        d0_open_return_pct=20,
        d1_close_return_pct=10,
    )

    class FakeResult:
        def all(self):
            return [
                (newer_evaluation, newer_score, ipo),
                (older_evaluation, older_score, ipo),
            ]

    class FakeSession:
        def execute(self, _statement):
            return FakeResult()

    rows = build_validation_dataset(FakeSession(), model_version="rules-v3-simple")

    assert len(rows) == 1
    assert rows[0].total_score == 80
