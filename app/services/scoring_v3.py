from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any, Optional, Union
from zoneinfo import ZoneInfo

from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.orm import Session

from app.config import load_yaml_config
from app.models import (
    DemandForecast,
    Ipo,
    IpoStructureMetric,
    MarketConditionSnapshot,
    NewsMetric,
    ScoreSnapshot,
    ValuationMetric,
)
from app.services.clock import local_today
from app.services.scoring import _scale, score_demand, score_market_condition

MODEL_VERSION = "rules-v3-simple"
PILLAR_NATIVE_MAX = {"demand": 25.0, "supply": 30.0, "pricing": 15.0, "market": 10.0}


@dataclass(frozen=True)
class SimpleScoringRules:
    model_version: str
    weights: dict[str, float]
    bands: dict[str, float]
    minimum_confidence: float
    market: dict[str, float]
    severe_risk_flags: tuple[str, ...]
    rules_hash: str


@dataclass
class SimpleScoreResult:
    total_score: float
    demand_score: float
    financial_score: float
    valuation_score: float
    structure_score: float
    news_trend_score: float
    allocation_score: float
    market_condition_score: float
    risk_penalty_score: float
    recommendation_band: str
    confidence_score: float
    missing_features: list[str]
    explanation: dict[str, Any]
    model_version: str
    rules_hash: str


def load_simple_scoring_rules() -> SimpleScoringRules:
    config = load_yaml_config("scoring_rules_v3.yaml")
    raw_weights = dict(config.get("pillar_weights") or {})
    required = set(PILLAR_NATIVE_MAX)
    if set(raw_weights) != required:
        raise ValueError(f"pillar_weights keys must be exactly {sorted(required)}")
    weights = {key: float(value) for key, value in raw_weights.items()}
    if any(value < 0 for value in weights.values()) or round(sum(weights.values()), 6) != 100:
        raise ValueError("pillar_weights must be non-negative and sum to 100")

    bands = {key: float(value) for key, value in (config.get("recommendation_band") or {}).items()}
    if not {"strong", "watch", "neutral"}.issubset(bands):
        raise ValueError("recommendation_band requires strong, watch, and neutral")
    if not bands["strong"] > bands["watch"] > bands["neutral"]:
        raise ValueError("recommendation bands must be strictly descending")

    confidence = config.get("confidence") or {}
    minimum_confidence = float(confidence.get("minimum_for_recommendation", 90))
    if not 0 <= minimum_confidence <= 100:
        raise ValueError("minimum confidence must be between 0 and 100")

    severe_flags = config.get("severe_risk_flags") or []
    if not isinstance(severe_flags, list) or any(
        not isinstance(flag, str) for flag in severe_flags
    ):
        raise ValueError("severe_risk_flags must be a list of strings")

    canonical = json.dumps(config, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return SimpleScoringRules(
        model_version=str(config.get("model_version") or MODEL_VERSION),
        weights=weights,
        bands=bands,
        minimum_confidence=minimum_confidence,
        market={key: float(value) for key, value in (config.get("market_condition") or {}).items()},
        severe_risk_flags=tuple(flag.strip() for flag in severe_flags if flag.strip()),
        rules_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def resolve_scoring_identity(
    model_version: Optional[str] = None,
    rules_hash: Optional[str] = None,
) -> tuple[str, str]:
    """Resolve one exact scoring lineage, defaulting to the active v3 configuration."""
    if model_version is None and rules_hash is None:
        rules = load_simple_scoring_rules()
        return rules.model_version, rules.rules_hash
    if not model_version or not model_version.strip() or not rules_hash or not rules_hash.strip():
        raise ValueError("model_version and rules_hash must be provided together")
    return model_version.strip(), rules_hash.strip()


class SimpleIpoScoringService:
    def __init__(self, rules: Optional[SimpleScoringRules] = None) -> None:
        self.rules = rules or load_simple_scoring_rules()

    def score_and_save(
        self,
        session: Session,
        ipo: Ipo,
        score_date: Optional[date] = None,
        calculated_at: Optional[datetime] = None,
    ) -> ScoreSnapshot:
        score_date = score_date or local_today()
        # Materialize SQLAlchemy on-update timestamps before taking the score cutoff.
        session.flush()
        calculated_at = _as_utc(calculated_at or datetime.now(timezone.utc))
        result = self.score(session, ipo, as_of_date=calculated_at)
        fingerprint = result.explanation["input_fingerprint"]
        snapshot = (
            session.execute(
                select(ScoreSnapshot).where(
                    ScoreSnapshot.ipo_id == ipo.id,
                    ScoreSnapshot.score_date == score_date,
                    ScoreSnapshot.model_version == result.model_version,
                    ScoreSnapshot.input_fingerprint == fingerprint,
                )
            )
            .scalars()
            .first()
        )
        if snapshot is not None:
            return snapshot

        snapshot = ScoreSnapshot(
            ipo_id=ipo.id,
            score_date=score_date,
            calculated_at=calculated_at,
            model_version=result.model_version,
            input_fingerprint=fingerprint,
        )
        session.add(snapshot)
        snapshot.total_score = result.total_score
        snapshot.demand_score = result.demand_score
        snapshot.financial_score = result.financial_score
        snapshot.valuation_score = result.valuation_score
        snapshot.structure_score = result.structure_score
        snapshot.news_trend_score = result.news_trend_score
        snapshot.allocation_score = result.allocation_score
        snapshot.market_condition_score = result.market_condition_score
        snapshot.risk_penalty_score = result.risk_penalty_score
        snapshot.recommendation_band = result.recommendation_band
        snapshot.confidence_score = result.confidence_score
        snapshot.missing_features_json = result.missing_features
        snapshot.explanation_json = result.explanation
        snapshot.rules_hash = result.rules_hash
        return snapshot

    def score(
        self,
        session: Session,
        ipo: Ipo,
        as_of_date: Optional[Union[date, datetime]] = None,
    ) -> SimpleScoreResult:
        cutoff = _normalize_cutoff(as_of_date)
        demand = _latest_demand(session, ipo.id, cutoff)
        structure = _latest_dated(session, IpoStructureMetric, ipo.id, cutoff)
        valuation = _latest_dated(session, ValuationMetric, ipo.id, cutoff)
        market = _latest_market(session, cutoff)
        news = _latest_news(session, ipo.id, cutoff)

        offer_price, offer_price_kind = _offer_price(ipo, cutoff)
        demand_valid = _demand_is_usable(demand)
        supply_valid = _supply_is_usable(structure)
        pricing_valid = _pricing_is_usable(valuation)
        market_valid = _market_is_usable(market)

        raw_demand, demand_detail = score_demand(demand if demand_valid else None)
        raw_supply, supply_detail = score_supply_pressure(structure if supply_valid else None)
        raw_pricing, pricing_detail = score_price_attractiveness(
            valuation if pricing_valid else None
        )
        raw_market, market_detail = score_market_condition(
            market if market_valid else None, self.rules.market
        )

        demand_score = _scale(raw_demand, PILLAR_NATIVE_MAX["demand"], self.rules.weights["demand"])
        supply_score = _scale(raw_supply, PILLAR_NATIVE_MAX["supply"], self.rules.weights["supply"])
        pricing_score = _scale(
            raw_pricing, PILLAR_NATIVE_MAX["pricing"], self.rules.weights["pricing"]
        )
        market_score = _scale(raw_market, PILLAR_NATIVE_MAX["market"], self.rules.weights["market"])
        total = round(
            max(min(demand_score + supply_score + pricing_score + market_score, 100), 0), 1
        )

        confidence = sum(
            (
                self.rules.weights["demand"] if demand_valid else 0,
                self.rules.weights["supply"] if supply_valid else 0,
                self.rules.weights["pricing"] if pricing_valid else 0,
                self.rules.weights["market"] if market_valid else 0,
            )
        )
        missing = _missing_features(
            offer_price,
            demand,
            demand_valid,
            structure,
            supply_valid,
            valuation,
            pricing_valid,
            market,
            market_valid,
        )
        source_issues = _critical_source_issues(demand, structure)
        severe_reasons = _severe_risk_reasons(structure, self.rules.severe_risk_flags)
        review_warnings = _review_warnings(news)

        critical_missing = [
            name
            for name, present in (
                ("offer_price", offer_price is not None),
                ("demand_forecast", demand_valid),
                ("supply_core", supply_valid),
            )
            if not present
        ]
        critical_missing.extend(source_issues)
        if severe_reasons:
            gate_status = "blocked"
            band = "Blocked"
        elif critical_missing or confidence < self.rules.minimum_confidence:
            gate_status = "insufficient_data"
            band = "Insufficient Data"
        else:
            gate_status = "pass"
            band = _recommendation_band(total, self.rules)

        cutoff_iso = cutoff.isoformat()
        pillars = {
            "demand": _pillar_payload(demand_score, self.rules.weights["demand"], demand_detail),
            "supply": _pillar_payload(supply_score, self.rules.weights["supply"], supply_detail),
            "pricing": _pillar_payload(
                pricing_score, self.rules.weights["pricing"], pricing_detail
            ),
            "market": _pillar_payload(market_score, self.rules.weights["market"], market_detail),
        }
        gates = {
            "status": gate_status,
            "critical_missing": critical_missing,
            "blocked_reasons": severe_reasons,
            "review_warnings": review_warnings,
            "offer_price_kind": offer_price_kind,
            "minimum_confidence": self.rules.minimum_confidence,
        }
        explanation: dict[str, Any] = {
            "pillars": pillars,
            "gates": gates,
            "feature_cutoff": cutoff_iso,
            "sources": _source_payload(demand, structure, valuation, market, news),
            "excluded_from_total": ["financial", "news_trend", "allocation"],
        }
        explanation["input_fingerprint"] = _input_fingerprint(
            ipo,
            cutoff,
            self.rules.rules_hash,
            demand,
            structure,
            valuation,
            market,
            news,
        )
        return SimpleScoreResult(
            total_score=total,
            demand_score=round(demand_score, 1),
            financial_score=0.0,
            valuation_score=round(pricing_score, 1),
            structure_score=round(supply_score, 1),
            news_trend_score=0.0,
            allocation_score=0.0,
            market_condition_score=round(market_score, 1),
            risk_penalty_score=0.0,
            recommendation_band=band,
            confidence_score=round(confidence, 1),
            missing_features=missing,
            explanation=explanation,
            model_version=self.rules.model_version,
            rules_hash=self.rules.rules_hash,
        )


def score_supply_pressure(
    metric: Optional[IpoStructureMetric],
) -> tuple[float, dict[str, Any]]:
    if metric is None:
        return 0.0, {"score": 0.0, "reason": "공급 구조 데이터 없음"}

    free_float = metric.free_float_ratio_on_listing
    ratio_score = 0.0
    if free_float is not None:
        ratio_score = (
            15
            if free_float <= 0.20
            else 11
            if free_float <= 0.35
            else 5
            if free_float <= 0.50
            else 0
        )

    market_cap = _optional_float(metric.free_float_market_cap)
    market_cap_score = 0.0
    if market_cap is not None:
        market_cap_score = (
            10 if market_cap <= 50_000_000_000 else 5 if market_cap <= 150_000_000_000 else 0
        )

    overhang_values = [
        float(value)
        for value in (metric.secondary_share_ratio, metric.vc_overhang_ratio)
        if value is not None
    ]
    overhang = max(overhang_values) if overhang_values else None
    overhang_score = 0.0
    if overhang is not None:
        overhang_score = (
            5 if overhang <= 0.10 else 3 if overhang <= 0.25 else 1 if overhang <= 0.50 else 0
        )

    score = min(ratio_score + market_cap_score + overhang_score, 30)
    return score, {
        "score": score,
        "free_float_ratio": free_float,
        "free_float_market_cap": market_cap,
        "overhang_ratio": overhang,
        "ratio_score": ratio_score,
        "market_cap_score": market_cap_score,
        "overhang_score": overhang_score,
    }


def score_price_attractiveness(
    metric: Optional[ValuationMetric],
) -> tuple[float, dict[str, Any]]:
    if metric is None:
        return 0.0, {"score": 0.0, "reason": "가격 매력 데이터 없음"}

    premium = metric.valuation_discount_premium_ratio
    discount_score = 0.0
    if premium is not None:
        discount_score = (
            10 if premium <= -0.20 else 7 if premium <= 0 else 3 if premium <= 0.20 else 0
        )

    comparisons: list[bool] = []
    if metric.per is not None and metric.peer_median_per is not None:
        comparisons.append(metric.per <= metric.peer_median_per)
    if metric.psr is not None and metric.peer_median_psr is not None:
        comparisons.append(metric.psr <= metric.peer_median_psr)
    relative_score = 5 * sum(comparisons) / len(comparisons) if comparisons else 0.0
    score = min(discount_score + relative_score, 15)
    return score, {
        "score": score,
        "valuation_discount_premium_ratio": premium,
        "relative_comparisons": len(comparisons),
        "relative_score": relative_score,
        "discount_score": discount_score,
    }


def _recommendation_band(total_score: float, rules: SimpleScoringRules) -> str:
    if total_score >= rules.bands["strong"]:
        return "Strong"
    if total_score >= rules.bands["watch"]:
        return "Watch"
    if total_score >= rules.bands["neutral"]:
        return "Neutral"
    return "Avoid"


def _normalize_cutoff(value: Optional[Union[date, datetime]]) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return _as_utc(value)
    local_end = datetime.combine(value, time.max, tzinfo=ZoneInfo("Asia/Seoul"))
    return local_end.astimezone(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _latest_demand(session: Session, ipo_id: int, cutoff: datetime) -> Optional[DemandForecast]:
    available = or_(
        and_(
            DemandForecast.result_published_at.is_not(None),
            DemandForecast.result_published_at <= cutoff,
        ),
        and_(
            DemandForecast.result_published_at.is_(None),
            DemandForecast.updated_at <= cutoff,
        ),
    )
    stmt = (
        select(DemandForecast)
        .where(
            DemandForecast.ipo_id == ipo_id,
            available,
            DemandForecast.updated_at <= cutoff,
        )
        .order_by(
            desc(func.coalesce(DemandForecast.result_published_at, DemandForecast.updated_at)),
            desc(DemandForecast.updated_at),
            desc(DemandForecast.id),
        )
    )
    return session.execute(stmt).scalars().first()


def _latest_dated(session: Session, model, ipo_id: int, cutoff: datetime):
    cutoff_date = _local_date(cutoff)
    stmt = (
        select(model)
        .where(
            model.ipo_id == ipo_id,
            model.as_of_date <= cutoff_date,
            model.updated_at <= cutoff,
        )
        .order_by(desc(model.as_of_date), desc(model.updated_at), desc(model.id))
    )
    return session.execute(stmt).scalars().first()


def _latest_market(session: Session, cutoff: datetime) -> Optional[MarketConditionSnapshot]:
    cutoff_date = _local_date(cutoff)
    stmt = (
        select(MarketConditionSnapshot)
        .where(
            MarketConditionSnapshot.as_of_date <= cutoff_date,
            MarketConditionSnapshot.updated_at <= cutoff,
        )
        .order_by(
            desc(MarketConditionSnapshot.as_of_date),
            desc(MarketConditionSnapshot.updated_at),
            desc(MarketConditionSnapshot.id),
        )
    )
    return session.execute(stmt).scalars().first()


def _latest_news(session: Session, ipo_id: int, cutoff: datetime) -> Optional[NewsMetric]:
    cutoff_date = _local_date(cutoff)
    stmt = (
        select(NewsMetric)
        .where(
            NewsMetric.ipo_id == ipo_id,
            or_(NewsMetric.window_end.is_(None), NewsMetric.window_end <= cutoff_date),
            NewsMetric.updated_at <= cutoff,
        )
        .order_by(desc(NewsMetric.window_end), desc(NewsMetric.updated_at), desc(NewsMetric.id))
    )
    return session.execute(stmt).scalars().first()


def _offer_price(
    ipo: Ipo,
    cutoff: Optional[datetime] = None,
) -> tuple[Optional[int], Optional[str]]:
    if cutoff is not None:
        updated_at = getattr(ipo, "updated_at", None)
        if updated_at is None or _as_utc(updated_at) > cutoff:
            return None, "unavailable_at_cutoff"
    for name in ("offer_price_final", "offer_price_max", "offer_price_min"):
        value = getattr(ipo, name)
        if value:
            return int(value), name
    return None, None


def _demand_is_usable(metric: Optional[DemandForecast]) -> bool:
    return bool(
        metric
        and metric.institutional_competition_ratio is not None
        and _has_source(metric)
        and (metric.result_published_at is not None or metric.updated_at is not None)
    )


def _supply_is_usable(metric: Optional[IpoStructureMetric]) -> bool:
    return bool(
        metric
        and (
            metric.free_float_ratio_on_listing is not None
            or metric.free_float_market_cap is not None
        )
        and _has_source(metric)
        and metric.as_of_date is not None
    )


def _pricing_is_usable(metric: Optional[ValuationMetric]) -> bool:
    return bool(
        metric
        and _has_source(metric)
        and metric.as_of_date is not None
        and (
            metric.valuation_discount_premium_ratio is not None
            or (metric.per is not None and metric.peer_median_per is not None)
            or (metric.psr is not None and metric.peer_median_psr is not None)
        )
    )


def _market_is_usable(metric: Optional[MarketConditionSnapshot]) -> bool:
    return bool(
        metric
        and _has_source(metric)
        and metric.as_of_date is not None
        and any(
            value is not None
            for value in (
                metric.kosdaq_momentum_20d,
                metric.recent_ipo_success_rate,
                metric.volatility_20d,
            )
        )
    )


def _has_source(row: Any) -> bool:
    return bool(str(getattr(row, "source", "") or "").strip())


def _missing_features(
    offer_price: Optional[int],
    demand: Optional[DemandForecast],
    demand_valid: bool,
    structure: Optional[IpoStructureMetric],
    supply_valid: bool,
    valuation: Optional[ValuationMetric],
    pricing_valid: bool,
    market: Optional[MarketConditionSnapshot],
    market_valid: bool,
) -> list[str]:
    missing: list[str] = []
    if offer_price is None:
        missing.append("offer_price")
    for name, row, valid in (
        ("demand_forecast", demand, demand_valid),
        ("ipo_structure_metrics", structure, supply_valid),
        ("valuation_metrics", valuation, pricing_valid),
        ("market_condition", market, market_valid),
    ):
        if row is None or not valid:
            missing.append(name)
    return missing


def _critical_source_issues(
    demand: Optional[DemandForecast], structure: Optional[IpoStructureMetric]
) -> list[str]:
    issues: list[str] = []
    if demand is not None and not _has_source(demand):
        issues.append("as_of_source:demand")
    if structure is not None and not _has_source(structure):
        issues.append("as_of_source:supply")
    return issues


def _severe_risk_reasons(
    structure: Optional[IpoStructureMetric], severe_flags: tuple[str, ...]
) -> list[str]:
    if structure is None:
        return []
    reasons: list[str] = []
    for raw_flag in structure.risk_flags_json or []:
        flag = str(raw_flag).strip()
        if flag and any(severe in flag for severe in severe_flags) and flag not in reasons:
            reasons.append(flag)
    return reasons


def _review_warnings(news: Optional[NewsMetric]) -> list[str]:
    if news is None or not (news.risk_keyword_count or 0):
        return []
    return [f"뉴스 위험 키워드 {news.risk_keyword_count}건: 사람 검토 필요"]


def _pillar_payload(score: float, maximum: float, detail: dict[str, Any]) -> dict[str, Any]:
    return {"score": round(score, 1), "max_score": maximum, "detail": detail}


def _source_payload(
    demand: Optional[DemandForecast],
    structure: Optional[IpoStructureMetric],
    valuation: Optional[ValuationMetric],
    market: Optional[MarketConditionSnapshot],
    news: Optional[NewsMetric],
) -> dict[str, Any]:
    return {
        "demand": _row_source(demand, "result_published_at"),
        "supply": _row_source(structure, "as_of_date"),
        "pricing": _row_source(valuation, "as_of_date"),
        "market": _row_source(market, "as_of_date"),
        "news_review": _row_source(news, "window_end"),
    }


def _row_source(row: Any, as_of_field: str) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    value = getattr(row, as_of_field, None) or getattr(row, "updated_at", None)
    return {
        "source": getattr(row, "source", None),
        "as_of": value.isoformat() if value is not None else None,
    }


def _input_fingerprint(
    ipo: Ipo,
    cutoff: datetime,
    rules_hash: str,
    *rows: Any,
) -> str:
    payload = {
        "rules_hash": rules_hash,
        "feature_cutoff_date": _local_date(cutoff).isoformat(),
        "ipo": {
            "offer_price_min": ipo.offer_price_min,
            "offer_price_max": ipo.offer_price_max,
            "offer_price_final": ipo.offer_price_final,
            "offer_price_row_updated_at": ipo.updated_at,
        },
        "rows": [_row_values(row) for row in rows],
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _row_values(row: Any) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    return {
        "table": row.__tablename__,
        "values": {
            column.name: getattr(row, column.name)
            for column in row.__table__.columns
            if column.name not in {"id", "ipo_id", "created_at"}
        },
    }


def _optional_float(value: Optional[Union[float, Decimal]]) -> Optional[float]:
    return float(value) if value is not None else None


def _local_date(value: datetime) -> date:
    return value.astimezone(ZoneInfo("Asia/Seoul")).date()
