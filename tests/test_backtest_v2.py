from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from app.models import OutcomeEvaluation, ScoreSnapshot
from app.services.backtest import (
    _canonical_pairs,
    _ranks,
    _spearman,
    _strategy_metrics,
    _wilson_interval,
    build_backtest_report,
    build_gross_outcome_summary,
    build_operational_backtest,
)


def _add_result(
    session,
    band,
    d1_return,
    correct,
    *,
    model=None,
    rules_hash=None,
    feature_score=0,
    missing_features=None,
    ipo_id=1,
):
    score = ScoreSnapshot(
        ipo_id=ipo_id,
        score_date=date(2026, 1, 1),
        calculated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        total_score=70,
        recommendation_band=band,
        model_version=model or band,
        rules_hash=rules_hash,
        demand_score=feature_score,
        structure_score=feature_score,
        valuation_score=feature_score,
        market_condition_score=0,
        missing_features_json=missing_features,
    )
    session.add(score)
    session.flush()
    session.add(
        OutcomeEvaluation(
            ipo_id=ipo_id,
            score_snapshot_id=score.id,
            evaluation_version=band,
            d0_open_return_pct=d1_return,
            d1_close_return_pct=d1_return,
            prediction_was_correct=correct,
        )
    )


def test_backtest_separates_band_accuracy(db_session):
    from app.models import Ipo

    db_session.add(
        Ipo(
            id=1,
            corp_name="A",
            normalized_name="a",
            status="listed",
            subscription_start_date=date(2026, 1, 2),
        )
    )
    db_session.flush()
    _add_result(db_session, "Strong", 10, True)
    _add_result(db_session, "Avoid", 10, False)
    _add_result(db_session, "Neutral", 2, None)
    db_session.flush()
    report = build_backtest_report(db_session)
    assert report.prediction_accuracy == 0.5
    assert report.strong_watch_accuracy == 1
    assert report.avoid_accuracy == 0
    assert report.neutral_accuracy == 1
    assert report.band_metrics["Neutral"].sample_count == 1


def test_backtest_marks_small_samples(db_session):
    report = build_backtest_report(db_session)
    assert report.band_metrics["Strong"].small_sample_warning is True


def test_backtest_empty_report_has_no_accuracy(db_session):
    report = build_backtest_report(db_session)
    assert report.evaluated_count == 0
    assert report.prediction_accuracy is None
    assert report.validation_status == "insufficient-sample"
    assert report.strategy_metrics["D0_OPEN"].positive_probability_ci_low is None


def test_backtest_filters_model_and_rules_hash(db_session):
    from app.models import Ipo

    db_session.add(
        Ipo(
            id=1,
            corp_name="A",
            normalized_name="a",
            status="listed",
            subscription_start_date=date(2026, 1, 2),
        )
    )
    db_session.flush()
    _add_result(
        db_session,
        "Strong",
        10,
        True,
        model="rules-v3-simple",
        rules_hash="one",
    )
    _add_result(
        db_session,
        "Avoid",
        -10,
        True,
        model="rules-v2",
        rules_hash="two",
    )
    db_session.flush()

    mixed = build_backtest_report(db_session, minimum_samples=1)
    filtered = build_backtest_report(
        db_session,
        model_version="rules-v3-simple",
        rules_hash="one",
        minimum_samples=1,
    )

    assert mixed.validation_status == "mixed-models"
    assert mixed.model_versions == ("rules-v2", "rules-v3-simple")
    assert mixed.rules_hashes == ("one", "two")
    assert filtered.evaluated_count == 1
    assert filtered.validation_status == "ready"
    assert filtered.selected_rules_hash == "one"
    assert build_backtest_report(db_session, rules_hash="missing").evaluated_count == 0


def test_backtest_does_not_validate_mixed_rules_for_one_model(db_session):
    from app.models import Ipo

    db_session.add(
        Ipo(
            id=1,
            corp_name="A",
            normalized_name="a",
            status="listed",
            subscription_start_date=date(2026, 1, 2),
        )
    )
    db_session.flush()
    _add_result(
        db_session,
        "Strong",
        10,
        True,
        model="rules-v3-simple",
        rules_hash="one",
    )
    _add_result(
        db_session,
        "Strong",
        20,
        True,
        model="rules-v3-simple",
        rules_hash="two",
    )
    db_session.flush()

    report = build_backtest_report(
        db_session,
        model_version="rules-v3-simple",
        minimum_samples=2,
    )

    assert report.evaluated_count == 2
    assert report.validation_status == "mixed-rules"
    assert report.rules_hashes == ("one", "two")


def test_backtest_feature_diagnostics_and_wilson_interval(db_session):
    from app.models import Ipo

    db_session.add_all(
        [
            Ipo(
                id=ipo_id,
                corp_name=f"A{ipo_id}",
                normalized_name=f"a{ipo_id}",
                status="listed",
                subscription_start_date=date(2026, 1, 2),
            )
            for ipo_id in (1, 2, 3)
        ]
    )
    db_session.flush()
    for ipo_id, (score, value) in enumerate(((10, -5), (20, 5), (30, 15)), start=1):
        _add_result(
            db_session,
            "Strong",
            value,
            value > 0,
            model="rules-v3-simple",
            feature_score=score,
            ipo_id=ipo_id,
        )
    db_session.flush()

    report = build_backtest_report(
        db_session,
        model_version="rules-v3-simple",
        minimum_samples=3,
    )

    demand = report.feature_metrics["demand"]
    market = report.feature_metrics["market"]
    assert demand.spearman_d0_open == pytest.approx(1)
    assert demand.spearman_d1_close == pytest.approx(1)
    assert demand.top_third_d1_positive_probability == 1
    assert demand.bottom_third_d1_positive_probability == 0
    assert market.spearman_d1_close is None
    strategy = report.strategy_metrics["D1_CLOSE"]
    assert strategy.positive_probability == pytest.approx(2 / 3)
    assert 0 < strategy.positive_probability_ci_low < strategy.positive_probability_ci_high < 1


def test_feature_diagnostics_exclude_missing_pillars_instead_of_treating_them_as_zero(
    db_session,
):
    from app.models import Ipo

    db_session.add(
        Ipo(
            id=1,
            corp_name="결측피처",
            normalized_name="결측피처",
            status="listed",
            subscription_start_date=date(2026, 1, 2),
        )
    )
    db_session.flush()
    _add_result(
        db_session,
        "Strong",
        10,
        True,
        model="rules-v3-simple",
        feature_score=0,
        missing_features=["demand_forecast"],
    )
    db_session.flush()

    report = build_backtest_report(db_session, model_version="rules-v3-simple")

    assert report.feature_metrics["demand"].sample_count == 0
    assert report.feature_metrics["supply"].sample_count == 1


def test_band_sample_count_uses_valid_d1_and_zero_is_not_positive(db_session):
    from app.models import Ipo

    db_session.add_all(
        [
            Ipo(
                id=1,
                corp_name="결측",
                normalized_name="결측",
                status="listed",
                subscription_start_date=date(2026, 1, 2),
            ),
            Ipo(
                id=2,
                corp_name="보합",
                normalized_name="보합",
                status="listed",
                subscription_start_date=date(2026, 1, 2),
            ),
        ]
    )
    db_session.flush()
    _add_result(
        db_session,
        "Strong",
        None,
        None,
        model="rules-v3-simple",
        rules_hash="hash",
        ipo_id=1,
    )
    _add_result(
        db_session,
        "Strong",
        0,
        False,
        model="rules-v3-simple",
        rules_hash="hash",
        ipo_id=2,
    )
    db_session.flush()

    report = build_backtest_report(
        db_session,
        model_version="rules-v3-simple",
        rules_hash="hash",
        minimum_samples=2,
    )
    metrics = report.band_metrics["Strong"]

    assert report.evaluated_count == 2
    assert report.validation_status == "insufficient-sample"
    assert metrics.sample_count == 1
    assert metrics.positive_probability == 0
    assert metrics.loss_probability == 0
    assert metrics.positive_probability_ci_low == 0
    assert metrics.positive_probability_ci_high is not None


def test_blocked_band_is_excluded_from_directional_accuracy(db_session):
    from app.models import Ipo

    db_session.add(
        Ipo(
            id=1,
            corp_name="안전차단",
            normalized_name="안전차단",
            status="listed",
            subscription_start_date=date(2026, 1, 2),
        )
    )
    db_session.flush()
    _add_result(
        db_session,
        "Blocked",
        -10,
        True,
        model="rules-v3-simple",
        rules_hash="hash",
    )
    db_session.flush()

    report = build_backtest_report(db_session)

    assert report.prediction_accuracy is None
    assert report.avoid_accuracy is None
    assert report.band_metrics["Blocked"].prediction_accuracy is None
    assert report.band_metrics["Blocked"].sample_count == 1


def test_backtest_helper_edge_cases():
    assert _spearman([(1, 1), (2, 2)]) is None
    assert _spearman([(1, 1), (1, 2), (1, 3)]) is None
    assert _ranks([3, 1, 1]) == [3, 1.5, 1.5]
    assert _wilson_interval(0, 0) == (None, None)
    metrics = _strategy_metrics([-1, 1])
    assert metrics.average_return_pct == 0
    assert metrics.median_return_pct == 0
    assert metrics.small_sample_warning is True


def test_canonical_pairs_keeps_newer_aware_pair_when_older_pair_follows():
    newer_score = ScoreSnapshot(
        id=2,
        ipo_id=1,
        score_date=date(2026, 1, 1),
        calculated_at=datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
        total_score=80,
        recommendation_band="Strong",
        model_version="rules-v3-simple",
        rules_hash="hash",
    )
    older_score = ScoreSnapshot(
        id=1,
        ipo_id=1,
        score_date=date(2026, 1, 1),
        calculated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        total_score=70,
        recommendation_band="Strong",
        model_version="rules-v3-simple",
        rules_hash="hash",
    )
    newer_evaluation = OutcomeEvaluation(
        id=2,
        ipo_id=1,
        evaluated_at=datetime(2026, 1, 2, 1, tzinfo=timezone.utc),
        evaluation_version="newer",
    )
    older_evaluation = OutcomeEvaluation(
        id=1,
        ipo_id=1,
        evaluated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        evaluation_version="older",
    )

    pairs = _canonical_pairs([(newer_evaluation, newer_score), (older_evaluation, older_score)])

    assert pairs == [(newer_evaluation, newer_score)]


def test_backtest_rejects_invalid_minimum(db_session):
    with pytest.raises(ValueError, match="positive"):
        build_backtest_report(db_session, minimum_samples=0)


def test_backtest_can_include_outcome_without_score(db_session):
    from app.models import Ipo

    db_session.add(Ipo(id=1, corp_name="A", normalized_name="a", status="listed"))
    db_session.add(
        OutcomeEvaluation(
            ipo_id=1,
            evaluation_version="unpaired",
            d0_open_return_pct=3,
            d1_close_return_pct=None,
        )
    )
    db_session.flush()

    report = build_backtest_report(db_session)

    assert report.evaluated_count == 1
    assert report.model_versions == ()
    assert report.feature_metrics["demand"].sample_count == 0


def test_backtest_excludes_samples_late_scores_and_duplicate_revisions(db_session):
    from app.models import Ipo

    db_session.add_all(
        [
            Ipo(
                id=1,
                corp_name="실기업",
                normalized_name="실기업",
                status="listed",
                subscription_start_date=date(2026, 1, 2),
            ),
            Ipo(
                id=2,
                corp_name="샘플",
                normalized_name="샘플",
                status="listed",
                is_sample=True,
                subscription_start_date=date(2026, 1, 2),
            ),
        ]
    )
    db_session.flush()
    _add_result(db_session, "Strong", 10, True, model="rules-v3-simple", ipo_id=1)
    _add_result(db_session, "Strong", 20, True, model="rules-v3-simple", ipo_id=1)
    _add_result(db_session, "Strong", 30, True, model="rules-v3-simple", ipo_id=2)
    late = ScoreSnapshot(
        ipo_id=1,
        score_date=date(2026, 1, 3),
        calculated_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
        total_score=90,
        recommendation_band="Strong",
        model_version="late-model",
    )
    db_session.add(late)
    db_session.flush()
    db_session.add(
        OutcomeEvaluation(
            ipo_id=1,
            score_snapshot_id=late.id,
            evaluation_version="late",
            d0_open_return_pct=40,
            d1_close_return_pct=40,
        )
    )
    db_session.flush()

    report = build_backtest_report(
        db_session,
        model_version="rules-v3-simple",
        minimum_samples=1,
    )

    assert report.evaluated_count == 1
    assert report.average_d1_return_pct == 20
    assert build_backtest_report(db_session, model_version="late-model").evaluated_count == 0


def test_strict_validation_report_requires_eligible_complete_score_pairs(db_session):
    from app.models import Ipo

    db_session.add_all(
        [
            Ipo(
                id=ipo_id,
                corp_name=f"검증{ipo_id}",
                normalized_name=f"검증{ipo_id}",
                status="listed",
                subscription_start_date=date(2026, 1, 2),
            )
            for ipo_id in (1, 2, 3, 4)
        ]
    )
    db_session.flush()
    valid_explanation = {
        "gates": {"status": "pass"},
        "sources": {
            "demand": {"source": "dart", "as_of": "2026-01-01"},
            "supply": {"source": "dart", "as_of": "2026-01-01"},
        },
    }
    scores = []
    for ipo_id, explanation in (
        (1, valid_explanation),
        (2, {"gates": {"status": "insufficient_data"}}),
        (3, valid_explanation),
    ):
        score = ScoreSnapshot(
            ipo_id=ipo_id,
            score_date=date(2026, 1, 1),
            calculated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            total_score=80,
            recommendation_band="Strong",
            model_version="rules-v3-simple",
            rules_hash="hash",
            explanation_json=explanation,
        )
        db_session.add(score)
        scores.append(score)
    db_session.flush()
    db_session.add_all(
        [
            OutcomeEvaluation(
                ipo_id=1,
                score_snapshot_id=scores[0].id,
                evaluation_version="valid",
                d0_open_return_pct=10,
                d1_close_return_pct=20,
            ),
            OutcomeEvaluation(
                ipo_id=2,
                score_snapshot_id=scores[1].id,
                evaluation_version="bad-gate",
                d0_open_return_pct=10,
                d1_close_return_pct=20,
            ),
            OutcomeEvaluation(
                ipo_id=3,
                score_snapshot_id=scores[2].id,
                evaluation_version="incomplete",
                d0_open_return_pct=10,
            ),
            OutcomeEvaluation(
                ipo_id=4,
                evaluation_version="unpaired",
                d0_open_return_pct=10,
                d1_close_return_pct=20,
            ),
        ]
    )
    db_session.flush()

    report = build_backtest_report(
        db_session,
        model_version="rules-v3-simple",
        rules_hash="hash",
        minimum_samples=1,
        validation_eligible_only=True,
    )

    assert report.evaluated_count == 1
    assert report.validation_status == "ready"
    assert report.average_d1_return_pct == 20
    assert (
        build_backtest_report(
            db_session,
            minimum_samples=1,
            validation_eligible_only=True,
        ).evaluated_count
        == 1
    )


def test_gross_outcomes_are_canonical_per_real_ipo_and_allow_incomplete_rows(db_session):
    from app.models import Ipo

    db_session.add_all(
        [
            Ipo(id=1, corp_name="실기업1", normalized_name="실기업1", status="listed"),
            Ipo(id=2, corp_name="실기업2", normalized_name="실기업2", status="listed"),
            Ipo(
                id=3,
                corp_name="샘플",
                normalized_name="샘플",
                status="listed",
                is_sample=True,
            ),
        ]
    )
    db_session.flush()
    db_session.add_all(
        [
            OutcomeEvaluation(
                ipo_id=1,
                evaluation_version="middle",
                evaluated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
                d0_open_return_pct=5,
                d1_close_return_pct=5,
            ),
            OutcomeEvaluation(
                ipo_id=1,
                evaluation_version="newest",
                evaluated_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
                d0_open_return_pct=20,
                d1_close_return_pct=30,
            ),
            OutcomeEvaluation(
                ipo_id=1,
                evaluation_version="oldest",
                evaluated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                d0_open_return_pct=-100,
                d1_close_return_pct=-100,
            ),
            OutcomeEvaluation(
                ipo_id=2,
                evaluation_version="partial",
                evaluated_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
                d0_open_return_pct=-10,
            ),
            OutcomeEvaluation(
                ipo_id=3,
                evaluation_version="sample",
                evaluated_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
                d0_open_return_pct=999,
                d1_close_return_pct=999,
            ),
        ]
    )
    db_session.flush()

    summary = build_gross_outcome_summary(db_session)

    assert summary.ipo_count == 2
    assert summary.d0_open.sample_count == 2
    assert summary.d0_open.average_return_pct == 5
    assert summary.d0_open.median_return_pct == 5
    assert summary.d0_open.positive_probability == 0.5
    assert summary.d1_close.sample_count == 1
    assert summary.d1_close.average_return_pct == 30


def test_operational_backtest_pins_deployed_model_rules_and_strict_filter(monkeypatch):
    from app.services import backtest as backtest_module

    readiness = SimpleNamespace(marker="readiness")
    validation = SimpleNamespace(marker="validation")
    gross = SimpleNamespace(marker="gross")
    calls = []
    monkeypatch.setattr(
        backtest_module,
        "resolve_scoring_identity",
        lambda: ("rules-v3-simple", "deployed-hash"),
    )

    def assess(_session, **kwargs):
        calls.append(("readiness", kwargs))
        return readiness

    def build(_session, **kwargs):
        calls.append(("validation", kwargs))
        return validation

    monkeypatch.setattr(backtest_module, "assess_data_readiness", assess)
    monkeypatch.setattr(backtest_module, "build_backtest_report", build)
    monkeypatch.setattr(backtest_module, "build_gross_outcome_summary", lambda _session: gross)

    bundle = build_operational_backtest("session", minimum_samples=40)

    assert bundle.model_version == "rules-v3-simple"
    assert bundle.rules_hash == "deployed-hash"
    assert bundle.readiness is readiness
    assert bundle.validation is validation
    assert bundle.gross_outcomes is gross
    assert calls == [
        (
            "readiness",
            {
                "model_version": "rules-v3-simple",
                "rules_hash": "deployed-hash",
                "minimum_samples": 40,
            },
        ),
        (
            "validation",
            {
                "model_version": "rules-v3-simple",
                "rules_hash": "deployed-hash",
                "minimum_samples": 40,
                "validation_eligible_only": True,
            },
        ),
    ]
