from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.models import (
    DemandForecast,
    Ipo,
    IpoStructureMetric,
    MarketConditionSnapshot,
    NewsMetric,
    ScoreSnapshot,
    ValuationMetric,
)
from app.services import scoring_v3
from app.services.scoring_v3 import (
    MODEL_VERSION,
    SimpleIpoScoringService,
    _as_utc,
    _critical_source_issues,
    _demand_is_usable,
    _has_source,
    _input_fingerprint,
    _local_date,
    _market_is_usable,
    _normalize_cutoff,
    _offer_price,
    _optional_float,
    _pricing_is_usable,
    _recommendation_band,
    _review_warnings,
    _row_source,
    _row_values,
    _severe_risk_reasons,
    _supply_is_usable,
    load_simple_scoring_rules,
    resolve_scoring_identity,
    score_price_attractiveness,
    score_supply_pressure,
)

UTC = timezone.utc
CUTOFF = datetime(2026, 7, 18, 3, tzinfo=UTC)


def _base_config():
    return {
        "model_version": MODEL_VERSION,
        "pillar_weights": {"demand": 45, "supply": 30, "pricing": 15, "market": 10},
        "confidence": {"minimum_for_recommendation": 90},
        "recommendation_band": {"strong": 80, "watch": 65, "neutral": 50},
        "market_condition": {
            "neutral_score": 5,
            "kosdaq_momentum_positive": 0.05,
            "kosdaq_momentum_negative": -0.05,
            "recent_ipo_success_high": 0.60,
            "recent_ipo_success_low": 0.35,
        },
        "severe_risk_flags": ["상장철회", "감사의견", "횡령", "배임", "과징금"],
    }


def _ipo(db_session, **values):
    defaults = {
        "corp_name": "단순점수기업",
        "normalized_name": "단순점수기업",
        "status": "scheduled",
        "offer_price_final": 10_000,
        "updated_at": CUTOFF - timedelta(hours=1),
    }
    defaults.update(values)
    row = Ipo(**defaults)
    db_session.add(row)
    db_session.flush()
    return row


def _add_complete_features(
    db_session,
    ipo,
    *,
    at=CUTOFF - timedelta(hours=1),
    as_of=date(2026, 7, 18),
    risk_flags=None,
    news_risk=0,
):
    market = (
        db_session.query(MarketConditionSnapshot)
        .filter(MarketConditionSnapshot.as_of_date == as_of)
        .first()
    )
    if market is None:
        market = MarketConditionSnapshot(
            as_of_date=as_of,
            updated_at=at,
            source="pykrx+outcomes",
            kosdaq_momentum_20d=0.1,
            recent_ipo_success_rate=0.7,
            volatility_20d=0.1,
        )
    rows = [
        DemandForecast(
            ipo_id=ipo.id,
            institutional_competition_ratio=2_000,
            lockup_commitment_ratio=0.4,
            final_price_position="above_band",
            participants_count=2_000,
            above_band_participation_ratio=0.8,
            result_published_at=at,
            updated_at=at,
            source="dart",
            receipt_no=f"D-{at.isoformat()}",
        ),
        IpoStructureMetric(
            ipo_id=ipo.id,
            as_of_date=as_of,
            updated_at=at,
            source="dart",
            free_float_ratio_on_listing=0.1,
            free_float_market_cap=Decimal("10000000000"),
            secondary_share_ratio=0.05,
            vc_overhang_ratio=0.05,
            risk_flags_json=risk_flags or [],
        ),
        ValuationMetric(
            ipo_id=ipo.id,
            as_of_date=as_of,
            updated_at=at,
            source="dart",
            valuation_discount_premium_ratio=-0.3,
            per=10,
            peer_median_per=20,
            psr=1,
            peer_median_psr=2,
            valuation_comment="presence must not add points",
        ),
        market,
        NewsMetric(
            ipo_id=ipo.id,
            source="naver_news",
            window_start=as_of - timedelta(days=6),
            window_end=as_of,
            updated_at=at,
            risk_keyword_count=news_risk,
            positive_keyword_count=100,
        ),
    ]
    db_session.add_all(rows)
    db_session.flush()
    return rows


def test_rules_load_four_pillars_and_stable_hash():
    first = load_simple_scoring_rules()
    second = load_simple_scoring_rules()
    assert first.model_version == MODEL_VERSION
    assert first.weights == {"demand": 45, "supply": 30, "pricing": 15, "market": 10}
    assert sum(first.weights.values()) == 100
    assert first.minimum_confidence == 90
    assert first.rules_hash == second.rules_hash
    assert len(first.rules_hash) == 64


def test_scoring_identity_defaults_to_active_rules_and_requires_an_explicit_pair():
    rules = load_simple_scoring_rules()

    assert resolve_scoring_identity() == (rules.model_version, rules.rules_hash)
    assert resolve_scoring_identity(" legacy-model ", " legacy-hash ") == (
        "legacy-model",
        "legacy-hash",
    )
    for model_version, rules_hash in (
        ("legacy-model", None),
        (None, "legacy-hash"),
        (" ", "legacy-hash"),
        ("legacy-model", " "),
    ):
        with pytest.raises(ValueError, match="provided together"):
            resolve_scoring_identity(model_version, rules_hash)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda config: config.update(pillar_weights={"demand": 100}), "keys must be exactly"),
        (
            lambda config: config.update(
                pillar_weights={"demand": 44, "supply": 30, "pricing": 15, "market": 10}
            ),
            "sum to 100",
        ),
        (
            lambda config: config.update(
                pillar_weights={"demand": -1, "supply": 71, "pricing": 20, "market": 10}
            ),
            "sum to 100",
        ),
        (lambda config: config.update(recommendation_band={"strong": 80}), "requires strong"),
        (
            lambda config: config.update(
                recommendation_band={"strong": 60, "watch": 70, "neutral": 50}
            ),
            "strictly descending",
        ),
        (
            lambda config: config.update(confidence={"minimum_for_recommendation": 101}),
            "between 0 and 100",
        ),
        (lambda config: config.update(severe_risk_flags={"bad": "shape"}), "list of strings"),
        (lambda config: config.update(severe_risk_flags=[1]), "list of strings"),
    ],
)
def test_invalid_rules_are_rejected(monkeypatch, change, message):
    config = _base_config()
    change(config)
    monkeypatch.setattr(scoring_v3, "load_yaml_config", lambda _name: config)
    with pytest.raises(ValueError, match=message):
        load_simple_scoring_rules()


def test_rules_use_defaults_and_ignore_blank_severe_flags(monkeypatch):
    config = _base_config()
    config.pop("model_version")
    config.pop("confidence")
    config.pop("market_condition")
    config["severe_risk_flags"] = ["", " 횡령 "]
    monkeypatch.setattr(scoring_v3, "load_yaml_config", lambda _name: config)
    rules = load_simple_scoring_rules()
    assert rules.model_version == MODEL_VERSION
    assert rules.minimum_confidence == 90
    assert rules.market == {}
    assert rules.severe_risk_flags == ("횡령",)


@pytest.mark.parametrize(
    ("ratio", "expected"),
    [(0.2, 15), (0.35, 11), (0.5, 5), (0.51, 0), (None, 0)],
)
def test_supply_free_float_thresholds(ratio, expected):
    score, detail = score_supply_pressure(IpoStructureMetric(free_float_ratio_on_listing=ratio))
    assert detail["ratio_score"] == expected
    assert score == expected


@pytest.mark.parametrize(
    ("market_cap", "expected"),
    [(50_000_000_000, 10), (150_000_000_000, 5), (150_000_000_001, 0), (None, 0)],
)
def test_supply_market_cap_thresholds(market_cap, expected):
    score, detail = score_supply_pressure(IpoStructureMetric(free_float_market_cap=market_cap))
    assert detail["market_cap_score"] == expected
    assert score == expected


@pytest.mark.parametrize(
    ("overhang", "expected"),
    [(0.1, 5), (0.25, 3), (0.5, 1), (0.51, 0), (None, 0)],
)
def test_supply_overhang_thresholds(overhang, expected):
    metric = IpoStructureMetric(secondary_share_ratio=overhang, vc_overhang_ratio=0.05)
    if overhang is None:
        metric.vc_overhang_ratio = None
    score, detail = score_supply_pressure(metric)
    assert detail["overhang_score"] == expected
    assert score == expected


def test_supply_uses_worst_overhang_and_caps_at_thirty():
    metric = IpoStructureMetric(
        free_float_ratio_on_listing=0.1,
        free_float_market_cap=Decimal("10000000000"),
        secondary_share_ratio=0.05,
        vc_overhang_ratio=0.2,
    )
    score, detail = score_supply_pressure(metric)
    assert score == 28
    assert detail["overhang_ratio"] == 0.2
    assert score_supply_pressure(None)[0] == 0
    assert _optional_float(Decimal("1.5")) == 1.5
    assert _optional_float(None) is None


@pytest.mark.parametrize(
    ("premium", "expected"),
    [(-0.2, 10), (0, 7), (0.2, 3), (0.21, 0), (None, 0)],
)
def test_price_discount_thresholds(premium, expected):
    score, detail = score_price_attractiveness(
        ValuationMetric(valuation_discount_premium_ratio=premium)
    )
    assert detail["discount_score"] == expected
    assert score == expected


def test_price_relative_multiples_and_comment_do_not_overstate_score():
    metric = ValuationMetric(
        per=10,
        peer_median_per=20,
        psr=3,
        peer_median_psr=2,
        valuation_comment="아무 설명",
    )
    score, detail = score_price_attractiveness(metric)
    assert score == 2.5
    assert detail["relative_comparisons"] == 2
    assert score_price_attractiveness(ValuationMetric(per=10, peer_median_per=20))[0] == 5
    assert score_price_attractiveness(None)[0] == 0


def test_recommendation_band_covers_all_ranges():
    rules = load_simple_scoring_rules()
    assert _recommendation_band(80, rules) == "Strong"
    assert _recommendation_band(65, rules) == "Watch"
    assert _recommendation_band(50, rules) == "Neutral"
    assert _recommendation_band(49.9, rules) == "Avoid"


def test_cutoff_normalization_handles_date_naive_aware_and_default(monkeypatch):
    local_day = date(2026, 7, 18)
    assert _normalize_cutoff(local_day) == datetime(2026, 7, 18, 14, 59, 59, 999999, UTC)
    assert _as_utc(datetime(2026, 7, 18, 3)) == datetime(2026, 7, 18, 3, tzinfo=UTC)
    aware = datetime(2026, 7, 18, 12, tzinfo=timezone(timedelta(hours=9)))
    assert _normalize_cutoff(aware) == datetime(2026, 7, 18, 3, tzinfo=UTC)
    assert _local_date(datetime(2026, 7, 17, 23, tzinfo=UTC)) == local_day

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 18, 3, tzinfo=tz)

    monkeypatch.setattr(scoring_v3, "datetime", FixedDateTime)
    assert _normalize_cutoff(None) == datetime(2026, 7, 18, 3, tzinfo=UTC)


def test_complete_score_is_four_pillar_and_formatter_compatible(db_session):
    ipo = _ipo(db_session)
    _add_complete_features(db_session, ipo, news_risk=4)
    result = SimpleIpoScoringService().score(db_session, ipo, CUTOFF)
    assert result.total_score == 100
    assert result.demand_score == 45
    assert result.structure_score == 30
    assert result.valuation_score == 15
    assert result.market_condition_score == 10
    assert result.financial_score == result.news_trend_score == result.allocation_score == 0
    assert result.risk_penalty_score == 0
    assert result.confidence_score == 100
    assert result.recommendation_band == "Strong"
    assert result.model_version == MODEL_VERSION
    assert result.explanation["gates"]["status"] == "pass"
    assert result.explanation["gates"]["offer_price_kind"] == "offer_price_final"
    assert result.explanation["gates"]["blocked_reasons"] == []
    assert result.explanation["gates"]["review_warnings"]
    assert set(result.explanation["pillars"]) == {"demand", "supply", "pricing", "market"}
    assert result.explanation["excluded_from_total"] == ["financial", "news_trend", "allocation"]
    assert result.missing_features == []


def test_score_rejects_offer_price_mutated_after_feature_cutoff(db_session):
    ipo = _ipo(db_session, updated_at=CUTOFF + timedelta(seconds=1))
    _add_complete_features(db_session, ipo)

    result = SimpleIpoScoringService().score(db_session, ipo, CUTOFF)

    assert result.recommendation_band == "Insufficient Data"
    assert "offer_price" in result.missing_features
    assert result.explanation["gates"]["offer_price_kind"] == "unavailable_at_cutoff"


@pytest.mark.parametrize(
    ("values", "expected_kind"),
    [
        ({"offer_price_final": None, "offer_price_max": 9_000}, "offer_price_max"),
        (
            {"offer_price_final": None, "offer_price_max": None, "offer_price_min": 8_000},
            "offer_price_min",
        ),
    ],
)
def test_offer_price_fallbacks_pass_gate(db_session, values, expected_kind):
    ipo = _ipo(db_session, **values)
    _add_complete_features(db_session, ipo)
    result = SimpleIpoScoringService().score(db_session, ipo, CUTOFF)
    assert result.explanation["gates"]["offer_price_kind"] == expected_kind
    assert result.recommendation_band == "Strong"


def test_empty_and_partial_data_are_insufficient(db_session):
    empty = _ipo(
        db_session,
        corp_name="빈기업",
        normalized_name="빈기업",
        offer_price_final=None,
    )
    result = SimpleIpoScoringService().score(db_session, empty, CUTOFF)
    assert result.total_score == 0
    assert result.confidence_score == 0
    assert result.recommendation_band == "Insufficient Data"
    assert result.explanation["gates"]["status"] == "insufficient_data"
    assert {"offer_price", "demand_forecast", "supply_core"}.issubset(
        result.explanation["gates"]["critical_missing"]
    )
    assert set(result.missing_features) == {
        "offer_price",
        "demand_forecast",
        "ipo_structure_metrics",
        "valuation_metrics",
        "market_condition",
    }
    assert all(value is None for value in result.explanation["sources"].values())

    partial = _ipo(db_session, corp_name="부분기업", normalized_name="부분기업")
    _add_complete_features(db_session, partial)
    db_session.query(ValuationMetric).filter(ValuationMetric.ipo_id == partial.id).delete()
    db_session.flush()
    partial_result = SimpleIpoScoringService().score(db_session, partial, CUTOFF)
    assert partial_result.confidence_score == 85
    assert partial_result.recommendation_band == "Insufficient Data"
    assert "valuation_metrics" in partial_result.missing_features


def test_missing_critical_sources_are_explicit_gates(db_session):
    ipo = _ipo(db_session)
    demand, structure, *_ = _add_complete_features(db_session, ipo)
    db_session.query(DemandForecast).filter(DemandForecast.id == demand.id).update(
        {"source": None, "updated_at": CUTOFF - timedelta(hours=1)},
        synchronize_session=False,
    )
    db_session.query(IpoStructureMetric).filter(IpoStructureMetric.id == structure.id).update(
        {"source": " ", "updated_at": CUTOFF - timedelta(hours=1)},
        synchronize_session=False,
    )
    db_session.expire_all()
    result = SimpleIpoScoringService().score(db_session, ipo, CUTOFF)
    assert result.recommendation_band == "Insufficient Data"
    assert result.confidence_score == 25
    assert set(result.explanation["gates"]["critical_missing"]) == {
        "demand_forecast",
        "supply_core",
        "as_of_source:demand",
        "as_of_source:supply",
    }


def test_structured_severe_flag_blocks_but_news_keyword_only_warns(db_session):
    blocked = _ipo(db_session, corp_name="차단기업", normalized_name="차단기업")
    _add_complete_features(
        db_session,
        blocked,
        risk_flags=["감사의견 거절", "일반 경고", "감사의견 거절"],
        news_risk=9,
    )
    blocked_result = SimpleIpoScoringService().score(db_session, blocked, CUTOFF)
    assert blocked_result.recommendation_band == "Blocked"
    assert blocked_result.explanation["gates"]["status"] == "blocked"
    assert blocked_result.explanation["gates"]["blocked_reasons"] == ["감사의견 거절"]

    review = _ipo(db_session, corp_name="검토기업", normalized_name="검토기업")
    _add_complete_features(db_session, review, risk_flags=["일반 경고"], news_risk=5)
    review_result = SimpleIpoScoringService().score(db_session, review, CUTOFF)
    assert review_result.recommendation_band == "Strong"
    assert review_result.explanation["gates"]["blocked_reasons"] == []
    assert review_result.explanation["gates"]["review_warnings"] == [
        "뉴스 위험 키워드 5건: 사람 검토 필요"
    ]


def test_as_of_queries_exclude_every_future_feature(db_session):
    ipo = _ipo(db_session)
    old = _add_complete_features(db_session, ipo)
    future_at = CUTOFF + timedelta(days=1)
    future_day = date(2026, 7, 19)
    future = _add_complete_features(
        db_session,
        ipo,
        at=future_at,
        as_of=future_day,
        risk_flags=["상장철회"],
        news_risk=7,
    )
    future[0].institutional_competition_ratio = 1
    future[1].free_float_ratio_on_listing = 0.9
    future[2].valuation_discount_premium_ratio = 1
    future[3].kosdaq_momentum_20d = -1
    db_session.flush()

    result = SimpleIpoScoringService().score(db_session, ipo, CUTOFF)
    assert result.total_score == 100
    assert result.recommendation_band == "Strong"
    assert result.explanation["gates"]["review_warnings"] == []
    assert result.explanation["gates"]["blocked_reasons"] == []
    assert (
        result.explanation["sources"]["demand"]["as_of"] == old[0].result_published_at.isoformat()
    )
    assert result.explanation["sources"]["supply"]["as_of"] == "2026-07-18"
    assert result.explanation["sources"]["news_review"]["as_of"] == "2026-07-18"


def test_demand_without_publication_time_uses_updated_at(db_session):
    ipo = _ipo(db_session)
    demand = DemandForecast(
        ipo_id=ipo.id,
        institutional_competition_ratio=1_000,
        source="dart",
        receipt_no="fallback-time",
        result_published_at=None,
        updated_at=CUTOFF - timedelta(minutes=5),
    )
    db_session.add(demand)
    db_session.flush()
    result = SimpleIpoScoringService().score(db_session, ipo, CUTOFF)
    assert result.demand_score > 0
    assert result.explanation["sources"]["demand"]["as_of"] == demand.updated_at.isoformat()


def test_news_without_window_end_is_as_of_filtered_and_only_warns(db_session):
    ipo = _ipo(db_session)
    _add_complete_features(db_session, ipo)
    db_session.query(NewsMetric).filter(NewsMetric.ipo_id == ipo.id).delete()
    db_session.add(
        NewsMetric(
            ipo_id=ipo.id,
            source="naver_news",
            window_end=None,
            updated_at=CUTOFF - timedelta(minutes=1),
            risk_keyword_count=2,
        )
    )
    db_session.flush()
    result = SimpleIpoScoringService().score(db_session, ipo, CUTOFF)
    assert result.total_score == 100
    assert result.explanation["gates"]["review_warnings"] == [
        "뉴스 위험 키워드 2건: 사람 검토 필요"
    ]


def test_score_and_save_is_compatible_idempotent_and_preserves_explanation(db_session):
    ipo = _ipo(db_session)
    _add_complete_features(db_session, ipo)
    service = SimpleIpoScoringService()
    first = service.score_and_save(
        db_session,
        ipo,
        score_date=date(2026, 7, 18),
        calculated_at=CUTOFF,
    )
    db_session.flush()
    second = service.score_and_save(
        db_session,
        ipo,
        score_date=date(2026, 7, 18),
        calculated_at=CUTOFF + timedelta(minutes=1),
    )
    db_session.flush()
    assert first.id == second.id
    assert first.model_version == MODEL_VERSION
    assert first.total_score == 100
    assert first.financial_score == first.news_trend_score == first.allocation_score == 0
    assert first.explanation_json["feature_cutoff"] == CUTOFF.isoformat()
    assert set(first.explanation_json["pillars"]) == {"demand", "supply", "pricing", "market"}
    assert first.rules_hash == service.rules.rules_hash
    assert len(first.input_fingerprint) == 64
    assert db_session.query(ScoreSnapshot).filter_by(ipo_id=ipo.id).count() == 1


def test_fingerprint_changes_with_rules_cutoff_date_and_input(db_session):
    ipo = _ipo(db_session)
    rows = _add_complete_features(db_session, ipo)
    service = SimpleIpoScoringService()
    first = service.score(db_session, ipo, CUTOFF)
    next_day = service.score(db_session, ipo, CUTOFF + timedelta(days=1))
    changed_rules = replace(service.rules, rules_hash="f" * 64)
    changed = SimpleIpoScoringService(changed_rules).score(db_session, ipo, CUTOFF)
    ipo.offer_price_final = 11_000
    changed_input = service.score(db_session, ipo, CUTOFF)
    assert (
        len(
            {
                first.explanation["input_fingerprint"],
                next_day.explanation["input_fingerprint"],
                changed.explanation["input_fingerprint"],
                changed_input.explanation["input_fingerprint"],
            }
        )
        == 4
    )
    assert _row_values(rows[0])["table"] == "demand_forecasts"
    assert _row_values(None) is None


def test_helper_validity_and_provenance_branches():
    demand = DemandForecast(institutional_competition_ratio=1_000, source="dart")
    structure = IpoStructureMetric(source="dart", as_of_date=date(2026, 7, 18))
    valuation = ValuationMetric(source="dart", as_of_date=date(2026, 7, 18))
    market = MarketConditionSnapshot(source="test", as_of_date=date(2026, 7, 18))
    assert not _demand_is_usable(demand)
    demand.updated_at = CUTOFF
    assert _demand_is_usable(demand)
    assert not _supply_is_usable(structure)
    structure.free_float_market_cap = 1
    assert _supply_is_usable(structure)
    assert not _pricing_is_usable(valuation)
    valuation.per, valuation.peer_median_per = 1, 2
    assert _pricing_is_usable(valuation)
    valuation.per = valuation.peer_median_per = None
    valuation.psr, valuation.peer_median_psr = 1, 2
    assert _pricing_is_usable(valuation)
    valuation.psr = valuation.peer_median_psr = None
    valuation.valuation_discount_premium_ratio = -0.1
    assert _pricing_is_usable(valuation)
    assert not _market_is_usable(market)
    market.volatility_20d = 0.2
    assert _market_is_usable(market)
    assert _has_source(SimpleNamespace(source=" source "))
    assert not _has_source(SimpleNamespace())

    no_source_demand = DemandForecast(source=None)
    no_source_structure = IpoStructureMetric(source="")
    assert _critical_source_issues(no_source_demand, no_source_structure) == [
        "as_of_source:demand",
        "as_of_source:supply",
    ]
    assert _critical_source_issues(None, None) == []
    assert _severe_risk_reasons(None, ("횡령",)) == []
    assert _review_warnings(None) == []
    assert _review_warnings(NewsMetric(risk_keyword_count=0)) == []
    assert _row_source(None, "as_of_date") is None
    transient = SimpleNamespace(source="test", as_of_date=None, updated_at=None)
    assert _row_source(transient, "as_of_date") == {"source": "test", "as_of": None}


def test_offer_price_helper_and_direct_fingerprint():
    final = Ipo(offer_price_final=3, offer_price_max=2, offer_price_min=1)
    maximum = Ipo(offer_price_max=2, offer_price_min=1)
    minimum = Ipo(offer_price_min=1)
    missing = Ipo()
    assert _offer_price(final) == (3, "offer_price_final")
    assert _offer_price(maximum) == (2, "offer_price_max")
    assert _offer_price(minimum) == (1, "offer_price_min")
    assert _offer_price(missing) == (None, None)
    assert len(_input_fingerprint(final, CUTOFF, "rules", None)) == 64


def test_score_and_save_defaults_execute_without_optional_arguments(db_session):
    ipo = _ipo(
        db_session,
        corp_name="기본인자기업",
        normalized_name="기본인자기업",
        offer_price_final=None,
    )
    snapshot = SimpleIpoScoringService().score_and_save(db_session, ipo)
    db_session.flush()
    assert snapshot.id is not None
    assert snapshot.score_date == scoring_v3.local_today()
    assert snapshot.calculated_at.tzinfo is not None
    assert snapshot.recommendation_band == "Insufficient Data"
