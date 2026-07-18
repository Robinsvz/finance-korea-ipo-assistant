from datetime import date, datetime, timezone

from app.models import Ipo, ListingPrice, ScoreSnapshot
from app.services.outcome import (
    OutcomeEvaluationService,
    add_business_days,
    latest_pre_listing_score,
    pct_return,
    prediction_correct,
)


class DummyScore:
    recommendation_band = "Watch"


def test_add_business_days_skips_weekend():
    assert add_business_days(date(2026, 6, 19), 1) == date(2026, 6, 22)


def test_add_business_days_skips_krx_substitute_holiday():
    assert add_business_days(date(2026, 2, 27), 1) == date(2026, 3, 3)


def test_pct_return():
    assert pct_return(150, 100) == 50


def test_prediction_correct():
    assert prediction_correct(DummyScore(), 10) is True
    assert prediction_correct(DummyScore(), -1) is False


def test_neutral_prediction_is_not_directional_error():
    score = DummyScore()
    score.recommendation_band = "Neutral"
    assert prediction_correct(score, 10) is None


def test_avoid_prediction_is_correct_on_loss():
    score = DummyScore()
    score.recommendation_band = "Avoid"
    assert prediction_correct(score, -1) is True


def test_pre_listing_score_excludes_post_open_snapshot(db_session):
    listing_date = date(2026, 7, 20)
    ipo = Ipo(corp_name="A", normalized_name="a", status="listed", listing_date=listing_date)
    db_session.add(ipo)
    db_session.flush()
    before = ScoreSnapshot(
        ipo_id=ipo.id,
        score_date=listing_date,
        calculated_at=datetime(2026, 7, 19, 23, 0, tzinfo=timezone.utc),
        total_score=70,
        recommendation_band="Watch",
        model_version="before",
    )
    after = ScoreSnapshot(
        ipo_id=ipo.id,
        score_date=listing_date,
        calculated_at=datetime(2026, 7, 20, 1, 0, tzinfo=timezone.utc),
        total_score=80,
        recommendation_band="Strong",
        model_version="after",
    )
    db_session.add_all([before, after])
    db_session.flush()
    assert latest_pre_listing_score(db_session, ipo.id, listing_date).id == before.id


def test_same_model_pre_open_prediction_survives_post_open_revision(db_session):
    listing_date = date(2026, 7, 20)
    ipo = Ipo(corp_name="A", normalized_name="a", status="listed", listing_date=listing_date)
    db_session.add(ipo)
    db_session.flush()
    before = ScoreSnapshot(
        ipo_id=ipo.id,
        score_date=listing_date,
        calculated_at=datetime(2026, 7, 19, 23, 0, tzinfo=timezone.utc),
        total_score=70,
        recommendation_band="Watch",
        model_version="rules-v2",
        input_fingerprint="before-open",
    )
    after = ScoreSnapshot(
        ipo_id=ipo.id,
        score_date=listing_date,
        calculated_at=datetime(2026, 7, 20, 1, 0, tzinfo=timezone.utc),
        total_score=80,
        recommendation_band="Strong",
        model_version="rules-v2",
        input_fingerprint="after-open",
    )
    db_session.add_all([before, after])
    db_session.flush()

    assert latest_pre_listing_score(db_session, ipo.id, listing_date).id == before.id


def test_outcome_evaluation_is_upserted(db_session):
    listing_date = date(2026, 7, 20)
    ipo = Ipo(
        corp_name="A",
        normalized_name="a",
        status="listed",
        listing_date=listing_date,
        offer_price_final=100,
    )
    db_session.add(ipo)
    db_session.flush()
    score = ScoreSnapshot(
        ipo_id=ipo.id,
        score_date=date(2026, 7, 19),
        calculated_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
        total_score=70,
        recommendation_band="Watch",
        model_version="rules-v2",
    )
    price = ListingPrice(
        ipo_id=ipo.id,
        listing_date=listing_date,
        source="test",
        d0_open_price=120,
        d0_low_price=110,
        d0_close_price=130,
        d1_close_price=140,
        d3_close_price=150,
    )
    db_session.add_all([score, price])
    db_session.flush()
    service = OutcomeEvaluationService()
    first = service.evaluate_and_save(db_session, ipo)
    db_session.flush()
    second = service.evaluate_and_save(db_session, ipo)
    db_session.flush()
    assert first.id == second.id
    assert ipo.status == "evaluated"
