from datetime import date

from app.models import OutcomeEvaluation, ScoreSnapshot
from app.services.backtest import build_backtest_report


def _add_result(session, band, d1_return, correct):
    score = ScoreSnapshot(
        ipo_id=1,
        score_date=date(2026, 1, 1),
        total_score=70,
        recommendation_band=band,
        model_version=band,
    )
    session.add(score)
    session.flush()
    session.add(
        OutcomeEvaluation(
            ipo_id=1,
            score_snapshot_id=score.id,
            evaluation_version=band,
            d0_open_return_pct=d1_return,
            d1_close_return_pct=d1_return,
            prediction_was_correct=correct,
        )
    )


def test_backtest_separates_band_accuracy(db_session):
    from app.models import Ipo

    db_session.add(Ipo(id=1, corp_name="A", normalized_name="a", status="listed"))
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
