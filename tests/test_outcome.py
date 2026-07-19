from datetime import date, datetime, timezone

import pytest

from app.models import Ipo, ListingPrice, ScoreSnapshot
from app.services import outcome as outcome_module
from app.services.outcome import (
    OutcomeEvaluationService,
    add_business_days,
    latest_actionable_score,
    latest_pre_listing_score,
    pct_return,
    prediction_correct,
    profit_per_share,
    success_label,
)
from app.services.scoring_v3 import load_simple_scoring_rules


class DummyScore:
    recommendation_band = "Watch"


def test_add_business_days_skips_weekend():
    assert add_business_days(date(2026, 6, 19), 1) == date(2026, 6, 22)


def test_add_business_days_skips_krx_substitute_holiday():
    assert add_business_days(date(2026, 2, 27), 1) == date(2026, 3, 3)


def test_add_business_days_rejects_invalid_or_unavailable_ranges():
    with pytest.raises(ValueError, match="non-negative"):
        add_business_days(date(2026, 1, 1), -1)
    assert add_business_days(date(2026, 1, 1), 0) == date(2026, 1, 1)
    last_session = outcome_module.xcals.get_calendar("XKRX").last_session.date()
    with pytest.raises(ValueError, match="does not cover"):
        add_business_days(last_session, 1)


def test_pct_return():
    assert pct_return(150, 100) == 50
    assert pct_return(None, 100) is None
    assert pct_return(100, 0) is None
    assert profit_per_share(None, 100) is None
    assert profit_per_share(120, 100) == 20


def test_prediction_correct():
    assert prediction_correct(DummyScore(), 10) is True
    assert prediction_correct(DummyScore(), -1) is False
    assert prediction_correct(None, 10) is None
    assert prediction_correct(DummyScore(), None) is None
    assert success_label(None) is None
    assert success_label(0) is False
    assert success_label(1) is True


def test_neutral_prediction_is_not_directional_error():
    score = DummyScore()
    score.recommendation_band = "Neutral"
    assert prediction_correct(score, 10) is None


def test_avoid_prediction_is_correct_on_loss():
    score = DummyScore()
    score.recommendation_band = "Avoid"
    assert prediction_correct(score, -1) is True
    score.recommendation_band = "Blocked"
    assert prediction_correct(score, -1) is None


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
    with pytest.raises(ValueError, match="model_version"):
        latest_pre_listing_score(
            db_session,
            ipo.id,
            listing_date,
            rules_hash="hash-without-model",
        )


def test_actionable_score_uses_subscription_open_cutoff(db_session):
    rules = load_simple_scoring_rules()
    subscription_date = date(2026, 7, 10)
    listing_date = date(2026, 7, 20)
    ipo = Ipo(
        corp_name="A",
        normalized_name="a",
        status="listed",
        subscription_start_date=subscription_date,
        listing_date=listing_date,
    )
    db_session.add(ipo)
    db_session.flush()
    actionable = ScoreSnapshot(
        ipo_id=ipo.id,
        score_date=subscription_date,
        calculated_at=datetime(2026, 7, 9, 23, tzinfo=timezone.utc),
        total_score=70,
        recommendation_band="Watch",
        model_version="actionable",
        rules_hash="actionable-hash",
    )
    too_late = ScoreSnapshot(
        ipo_id=ipo.id,
        score_date=date(2026, 7, 15),
        calculated_at=datetime(2026, 7, 15, tzinfo=timezone.utc),
        total_score=90,
        recommendation_band="Strong",
        model_version="late",
        rules_hash="late-hash",
    )
    operating = ScoreSnapshot(
        ipo_id=ipo.id,
        score_date=subscription_date,
        calculated_at=datetime(2026, 7, 8, 23, tzinfo=timezone.utc),
        total_score=65,
        recommendation_band="Watch",
        model_version=rules.model_version,
        rules_hash=rules.rules_hash,
    )
    db_session.add_all([actionable, too_late, operating])
    db_session.flush()

    assert (
        latest_actionable_score(
            db_session,
            ipo.id,
            None,
            listing_date,
            model_version="late",
            rules_hash="late-hash",
        ).id
        == too_late.id
    )
    assert (
        latest_actionable_score(db_session, ipo.id, subscription_date, listing_date).id
        == operating.id
    )
    assert (
        latest_actionable_score(
            db_session,
            ipo.id,
            subscription_date,
            listing_date,
            model_version="actionable",
            rules_hash="actionable-hash",
        ).id
        == actionable.id
    )
    assert (
        latest_actionable_score(
            db_session,
            ipo.id,
            subscription_date,
            listing_date,
            model_version="actionable",
            rules_hash="missing",
        )
        is None
    )
    with pytest.raises(ValueError, match="provided together"):
        latest_actionable_score(
            db_session,
            ipo.id,
            subscription_date,
            listing_date,
            model_version="actionable",
        )
    with pytest.raises(ValueError, match="model_version"):
        latest_actionable_score(
            db_session,
            ipo.id,
            subscription_date,
            listing_date,
            rules_hash="actionable-hash",
        )


def test_outcome_evaluation_is_upserted(db_session):
    rules = load_simple_scoring_rules()
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
        model_version=rules.model_version,
        rules_hash=rules.rules_hash,
    )
    newer_legacy = ScoreSnapshot(
        ipo_id=ipo.id,
        score_date=date(2026, 7, 19),
        calculated_at=datetime(2026, 7, 19, 23, tzinfo=timezone.utc),
        total_score=99,
        recommendation_band="Strong",
        model_version="rules-v2",
        rules_hash="legacy-hash",
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
    db_session.add_all([score, newer_legacy, price])
    db_session.flush()
    service = OutcomeEvaluationService()
    first = service.evaluate_and_save(db_session, ipo)
    db_session.flush()
    second = service.evaluate_and_save(db_session, ipo)
    db_session.flush()
    assert first.id == second.id
    assert first.score_snapshot_id == score.id
    assert first.score_snapshot_id != newer_legacy.id
    assert ipo.status == "evaluated"


def test_outcome_service_accepts_only_a_complete_explicit_legacy_identity():
    service = OutcomeEvaluationService(model_version="rules-v2", rules_hash="legacy-hash")
    assert service.model_version == "rules-v2"
    assert service.rules_hash == "legacy-hash"
    with pytest.raises(ValueError, match="provided together"):
        OutcomeEvaluationService(model_version="rules-v2")


def test_outcome_evaluation_requires_listing_and_offer_prices(db_session):
    missing_listing = Ipo(corp_name="가격없음", normalized_name="가격없음", status="listed")
    missing_offer = Ipo(
        corp_name="공모가없음",
        normalized_name="공모가없음",
        status="listed",
        offer_price_max=100,
    )
    db_session.add_all([missing_listing, missing_offer])
    db_session.flush()
    db_session.add(
        ListingPrice(
            ipo_id=missing_offer.id,
            listing_date=date(2026, 7, 20),
            source="test",
            d0_open_price=100,
        )
    )
    db_session.flush()
    service = OutcomeEvaluationService()

    with pytest.raises(ValueError, match="Listing price is missing"):
        service.evaluate_and_save(db_session, missing_listing)
    with pytest.raises(ValueError, match="Final offer price is missing"):
        service.evaluate_and_save(db_session, missing_offer)


def test_missing_return_is_not_labeled_as_loss(db_session):
    ipo = Ipo(
        corp_name="부분가격",
        normalized_name="부분가격",
        status="listed",
        listing_date=date(2026, 7, 20),
        offer_price_final=100,
    )
    db_session.add(ipo)
    db_session.flush()
    db_session.add(
        ListingPrice(
            ipo_id=ipo.id,
            listing_date=ipo.listing_date,
            source="test",
            d0_open_price=120,
        )
    )
    db_session.flush()

    evaluation = OutcomeEvaluationService().evaluate_and_save(db_session, ipo)

    assert evaluation.label_open_success is True
    assert evaluation.label_d1_success is None
    assert evaluation.label_d3_success is None
