from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import get_settings, load_yaml_config
from app.models import (
    DemandForecast,
    FinancialMetric,
    Ipo,
    IpoStructureMetric,
    MarketConditionSnapshot,
    NewsMetric,
    ScoreSnapshot,
    SearchTrend,
    ValuationMetric,
)
from app.services.allocation import build_allocation_scenario, derive_allocation_assumptions
from app.services.clock import local_today

MODEL_VERSION = "rules-v2"


@dataclass(frozen=True)
class ScoringRules:
    model_version: str
    weights: dict[str, float]
    max_risk_penalty: float
    bands: dict[str, float]
    minimum_confidence: float
    critical_features: tuple[str, ...]
    market: dict[str, float]
    rules_hash: str


@dataclass
class ScoreResult:
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


def load_scoring_rules() -> ScoringRules:
    config = load_yaml_config("scoring_rules.yaml")
    weights_config = dict(config.get("score_weights") or {})
    max_risk = float(weights_config.pop("max_risk_penalty", 20))
    required = {
        "demand",
        "financial",
        "valuation",
        "structure",
        "news_trend",
        "allocation",
        "market_condition",
    }
    if set(weights_config) != required:
        raise ValueError(f"score_weights keys must be exactly {sorted(required)}")
    weights = {key: float(value) for key, value in weights_config.items()}
    if any(value < 0 for value in weights.values()) or round(sum(weights.values()), 6) != 100:
        raise ValueError("score_weights must be non-negative and sum to 100")
    bands = {key: float(value) for key, value in (config.get("recommendation_band") or {}).items()}
    if not {"strong", "watch", "neutral"}.issubset(bands):
        raise ValueError("recommendation_band requires strong, watch, and neutral")
    if not bands["strong"] > bands["watch"] > bands["neutral"]:
        raise ValueError("recommendation bands must be strictly descending")
    confidence = config.get("confidence") or {}
    canonical = json.dumps(config, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return ScoringRules(
        model_version=str(config.get("model_version") or MODEL_VERSION),
        weights=weights,
        max_risk_penalty=max_risk,
        bands=bands,
        minimum_confidence=float(confidence.get("minimum_for_recommendation", 70)),
        critical_features=tuple(confidence.get("critical_features") or ()),
        market={key: float(value) for key, value in (config.get("market_condition") or {}).items()},
        rules_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


class IpoScoringService:
    def __init__(
        self,
        default_user_cash: Optional[int] = None,
        rules: Optional[ScoringRules] = None,
    ) -> None:
        self.default_user_cash = (
            default_user_cash if default_user_cash is not None else get_settings().default_user_cash
        )
        self.rules = rules or load_scoring_rules()

    def score_and_save(
        self,
        session: Session,
        ipo: Ipo,
        score_date: Optional[date] = None,
        calculated_at: Optional[datetime] = None,
    ) -> ScoreSnapshot:
        score_date = score_date or local_today()
        calculated_at = calculated_at or datetime.now(timezone.utc)
        result = self.score(session, ipo)
        input_fingerprint = result.explanation["input_fingerprint"]
        snapshot = (
            session.execute(
                select(ScoreSnapshot).where(
                    ScoreSnapshot.ipo_id == ipo.id,
                    ScoreSnapshot.score_date == score_date,
                    ScoreSnapshot.model_version == result.model_version,
                    ScoreSnapshot.input_fingerprint == input_fingerprint,
                )
            )
            .scalars()
            .first()
        )
        if snapshot is None:
            snapshot = ScoreSnapshot(
                ipo_id=ipo.id,
                score_date=score_date,
                model_version=result.model_version,
                input_fingerprint=input_fingerprint,
            )
            session.add(snapshot)
        else:
            return snapshot
        snapshot.calculated_at = calculated_at
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

    def score(self, session: Session, ipo: Ipo) -> ScoreResult:
        missing: list[str] = []
        demand = _latest(session, DemandForecast, ipo.id)
        financial = _latest_financial(session, ipo.id)
        valuation = _latest(session, ValuationMetric, ipo.id)
        structure = _latest(session, IpoStructureMetric, ipo.id)
        news = _latest(session, NewsMetric, ipo.id)
        trend = _latest(session, SearchTrend, ipo.id)
        market = _latest_market(session)

        raw_demand, demand_exp = score_demand(demand)
        raw_financial, financial_exp = score_financial(financial)
        raw_valuation, valuation_exp = score_valuation(valuation)
        raw_structure, structure_exp = score_structure(structure)
        raw_news, news_exp = score_news_trend(news, trend)
        raw_allocation, allocation_exp = self._score_allocation(session, ipo)
        raw_market, market_exp = score_market_condition(market, self.rules.market)
        raw_risk, risk_exp = score_risk_penalty(financial, structure, news)

        demand_score = _scale(raw_demand, 25, self.rules.weights["demand"])
        financial_score = _scale(raw_financial, 15, self.rules.weights["financial"])
        valuation_score = _scale(raw_valuation, 15, self.rules.weights["valuation"])
        structure_score = _scale(raw_structure, 15, self.rules.weights["structure"])
        news_trend_score = _scale(raw_news, 10, self.rules.weights["news_trend"])
        allocation_score = _scale(raw_allocation, 10, self.rules.weights["allocation"])
        market_condition_score = _scale(raw_market, 10, self.rules.weights["market_condition"])
        risk_penalty_score = _scale(raw_risk, 20, self.rules.max_risk_penalty)

        feature_presence = {
            "demand_forecast": demand is not None,
            "financial_metrics": financial is not None,
            "valuation_metrics": valuation is not None,
            "ipo_structure_metrics": structure is not None,
            "news_metrics": news is not None,
            "search_trends": trend is not None,
            "offer_price": bool(
                ipo.offer_price_final or ipo.offer_price_max or ipo.offer_price_min
            ),
            "market_condition": market is not None,
        }
        missing.extend(name for name, present in feature_presence.items() if not present)
        confidence_score = self._confidence(feature_presence)
        critical_missing = [
            name for name in self.rules.critical_features if not feature_presence.get(name, False)
        ]

        total = (
            demand_score
            + financial_score
            + valuation_score
            + structure_score
            + news_trend_score
            + allocation_score
            + market_condition_score
            - risk_penalty_score
        )
        total = round(max(min(total, 100), 0), 1)
        if confidence_score < self.rules.minimum_confidence or critical_missing:
            band = "Insufficient Data"
        else:
            band = recommendation_band(total, self.rules)

        explanation = {
            "demand": demand_exp,
            "financial": financial_exp,
            "valuation": valuation_exp,
            "structure": structure_exp,
            "news_trend": news_exp,
            "allocation": allocation_exp,
            "market_condition": market_exp,
            "risk": risk_exp,
            "confidence": {
                "score": confidence_score,
                "minimum": self.rules.minimum_confidence,
                "critical_missing": critical_missing,
                "presence": feature_presence,
            },
        }
        explanation["input_fingerprint"] = _input_fingerprint(
            ipo,
            demand,
            financial,
            valuation,
            structure,
            news,
            trend,
            market,
        )
        return ScoreResult(
            total_score=total,
            demand_score=round(demand_score, 1),
            financial_score=round(financial_score, 1),
            valuation_score=round(valuation_score, 1),
            structure_score=round(structure_score, 1),
            news_trend_score=round(news_trend_score, 1),
            allocation_score=round(allocation_score, 1),
            market_condition_score=round(market_condition_score, 1),
            risk_penalty_score=round(risk_penalty_score, 1),
            recommendation_band=band,
            confidence_score=round(confidence_score, 1),
            missing_features=missing,
            explanation=explanation,
            model_version=self.rules.model_version,
            rules_hash=self.rules.rules_hash,
        )

    def _confidence(self, presence: dict[str, bool]) -> float:
        available = 0.0
        available += self.rules.weights["demand"] if presence["demand_forecast"] else 0
        available += self.rules.weights["financial"] if presence["financial_metrics"] else 0
        available += self.rules.weights["valuation"] if presence["valuation_metrics"] else 0
        available += self.rules.weights["structure"] if presence["ipo_structure_metrics"] else 0
        half_news = self.rules.weights["news_trend"] / 2
        available += half_news if presence["news_metrics"] else 0
        available += half_news if presence["search_trends"] else 0
        available += self.rules.weights["allocation"] if presence["offer_price"] else 0
        available += self.rules.weights["market_condition"] if presence["market_condition"] else 0
        return min(available, 100.0)

    def _score_allocation(self, session: Session, ipo: Ipo) -> tuple[float, dict[str, Any]]:
        if not (ipo.offer_price_final or ipo.offer_price_max or ipo.offer_price_min):
            return 0.0, {"score": 0, "reason": "공모가 없음"}
        candidates = ipo.underwriters or [None]
        best: Optional[tuple[float, dict[str, Any]]] = None
        for underwriter in candidates:
            try:
                assumptions = derive_allocation_assumptions(session, underwriter)
                scenario = build_allocation_scenario(
                    ipo,
                    underwriter,
                    self.default_user_cash,
                    assumptions=assumptions,
                )
            except ValueError as exc:
                return 0.0, {"score": 0, "reason": str(exc)}
            score = 0.0
            if scenario.expected_equal_shares_mid >= 1:
                score += 3
            elif scenario.expected_equal_shares_mid >= 0.2:
                score += 1.5
            if scenario.expected_proportional_shares_base >= 2:
                score += 3
            elif scenario.expected_proportional_shares_base >= 0.5:
                score += 1.5
            if ipo.refund_date and ipo.subscription_end_date:
                days = (ipo.refund_date - ipo.subscription_end_date).days
                score += 1 if 0 <= days <= 2 else 0.5
            if scenario.estimated_fee <= 2000:
                score += 1
            if len(ipo.underwriters) >= 2:
                score += 2
            score = min(score, 10)
            detail = {
                "score": score,
                "broker": scenario.broker_name,
                "apply_shares": scenario.apply_shares,
                "expected_equal_mid": scenario.expected_equal_shares_mid,
                "expected_proportional_base": scenario.expected_proportional_shares_base,
                "assumption_source": scenario.assumption_source,
            }
            if best is None or score > best[0]:
                best = (score, detail)
        return best or (0.0, {"score": 0})


def score_demand(demand: Optional[DemandForecast]) -> tuple[float, dict[str, Any]]:
    if demand is None:
        return 0.0, {"score": 0, "reason": "수요예측 데이터 없음"}
    ratio_score = score_institutional_competition(demand.institutional_competition_ratio)
    lockup = demand.lockup_commitment_ratio or 0
    if lockup > 1:
        lockup /= 100
    lockup_score = 7 if lockup >= 0.3 else 5 if lockup >= 0.15 else 3 if lockup >= 0.05 else 0
    price_score = {"above_band": 4, "top_band": 3, "in_band": 2, "below_band": 0}.get(
        demand.final_price_position or "", 0
    )
    participants_score = (
        2 if (demand.participants_count or 0) >= 1500 else 1 if demand.participants_count else 0
    )
    above_band_score = 2 if (demand.above_band_participation_ratio or 0) >= 0.5 else 0
    score = min(
        ratio_score + lockup_score + price_score + participants_score + above_band_score, 25
    )
    return score, {
        "score": score,
        "institutional_competition_ratio": demand.institutional_competition_ratio,
        "ratio_score": ratio_score,
        "lockup_score": lockup_score,
        "price_score": price_score,
    }


def score_institutional_competition(ratio: Optional[float]) -> int:
    if ratio is None:
        return 0
    if ratio >= 1500:
        return 10
    if ratio >= 1000:
        return 8
    if ratio >= 500:
        return 6
    if ratio >= 100:
        return 3
    return 0


def score_financial(metric: Optional[FinancialMetric]) -> tuple[float, dict[str, Any]]:
    if metric is None:
        return 0.0, {"score": 0, "reason": "재무 데이터 없음"}
    score = 0.0
    growth = metric.revenue_yoy_growth
    if growth is not None:
        score += 4 if growth >= 0.3 else 3 if growth >= 0.1 else 1.5 if growth >= 0 else 0
    if metric.operating_profit is not None and metric.operating_profit > 0:
        score += 3
    margin = metric.operating_margin
    if margin is not None:
        score += 3 if margin >= 0.15 else 2 if margin >= 0.05 else 1 if margin >= 0 else 0
    if metric.net_income is not None and metric.net_income > 0:
        score += 2
    if metric.operating_cash_flow is not None and metric.operating_cash_flow > 0:
        score += 2
    if metric.debt_ratio is not None:
        score += 1 if metric.debt_ratio <= 1 else 0.5 if metric.debt_ratio <= 2 else 0
    score = min(score, 15)
    return score, {
        "score": score,
        "revenue_yoy_growth": growth,
        "operating_margin": metric.operating_margin,
        "debt_ratio": metric.debt_ratio,
    }


def score_valuation(metric: Optional[ValuationMetric]) -> tuple[float, dict[str, Any]]:
    if metric is None:
        return 0.0, {"score": 0, "reason": "밸류에이션 데이터 없음"}
    score = 0.0
    premium = metric.valuation_discount_premium_ratio
    if premium is not None:
        score += 6 if premium <= -0.2 else 4 if premium <= 0 else 2 if premium <= 0.2 else 0
    if metric.per is not None and metric.peer_median_per is not None:
        score += 3 if metric.per <= metric.peer_median_per else 1
    if metric.psr is not None and metric.peer_median_psr is not None:
        score += 3 if metric.psr <= metric.peer_median_psr else 1
    if metric.valuation_comment:
        score += 3
    score = min(score, 15)
    return score, {"score": score, "valuation_discount_premium_ratio": premium}


def score_structure(metric: Optional[IpoStructureMetric]) -> tuple[float, dict[str, Any]]:
    if metric is None:
        return 0.0, {"score": 0, "reason": "공모 구조 데이터 없음"}
    score = 0.0
    free_float = metric.free_float_ratio_on_listing
    if free_float is not None:
        score += (
            5 if free_float <= 0.2 else 3 if free_float <= 0.35 else 1 if free_float <= 0.5 else 0
        )
    if metric.free_float_market_cap is not None:
        score += (
            3
            if metric.free_float_market_cap <= 50_000_000_000
            else 1.5
            if metric.free_float_market_cap <= 150_000_000_000
            else 0
        )
    secondary = metric.secondary_share_ratio
    if secondary is not None:
        score += 3 if secondary <= 0.1 else 1.5 if secondary <= 0.3 else 0
    if metric.use_of_proceeds_summary:
        score += (
            2
            if any(
                keyword in metric.use_of_proceeds_summary
                for keyword in ("시설", "연구", "성장", "투자")
            )
            else 1
        )
    if metric.vc_overhang_ratio is not None:
        score += (
            2 if metric.vc_overhang_ratio <= 0.1 else 1 if metric.vc_overhang_ratio <= 0.25 else 0
        )
    score = min(score, 15)
    return score, {"score": score, "free_float_ratio_on_listing": free_float}


def score_news_trend(
    news: Optional[NewsMetric], trend: Optional[SearchTrend]
) -> tuple[float, dict[str, Any]]:
    score = 0.0
    explanation: dict[str, Any] = {"score": 0}
    if news:
        growth = news.article_growth_rate_7d or 0
        score += 2 if growth >= 1 else 1 if growth > 0 else 0
        media = news.major_media_count or 0
        score += 2 if media >= 3 else 1 if media >= 1 else 0
        if (news.positive_keyword_count or 0) > 0:
            score += 1
        explanation.update(
            article_growth_rate_7d=news.article_growth_rate_7d,
            risk_keyword_count=news.risk_keyword_count,
        )
    if trend:
        growth = trend.trend_growth_rate or 0
        score += 3 if growth >= 1 else 1.5 if growth > 0 else 0
        if (trend.peak_index or 0) >= 70 and (trend.zero_data_ratio or 0) < 0.5:
            score += 2
        explanation.update(
            trend_growth_rate=trend.trend_growth_rate,
            zero_data_ratio=trend.zero_data_ratio,
        )
    score = max(min(score, 10), 0)
    explanation["score"] = score
    return score, explanation


def score_market_condition(
    market: Optional[MarketConditionSnapshot],
    rules: dict[str, float],
) -> tuple[float, dict[str, Any]]:
    if market is None:
        return 0.0, {"score": 0, "reason": "시장 환경 데이터 없음"}
    score = float(rules.get("neutral_score", 5))
    momentum = market.kosdaq_momentum_20d
    success_rate = market.recent_ipo_success_rate
    volatility = market.volatility_20d
    if momentum is not None:
        if momentum >= rules.get("kosdaq_momentum_positive", 0.05):
            score += 2
        elif momentum <= rules.get("kosdaq_momentum_negative", -0.05):
            score -= 2
    if success_rate is not None:
        if success_rate >= rules.get("recent_ipo_success_high", 0.60):
            score += 2
        elif success_rate <= rules.get("recent_ipo_success_low", 0.35):
            score -= 2
    if volatility is not None:
        score += 1 if volatility <= 0.20 else -1 if volatility >= 0.40 else 0
    score = max(min(score, 10), 0)
    return score, {
        "score": score,
        "as_of_date": market.as_of_date.isoformat(),
        "kosdaq_momentum_20d": momentum,
        "recent_ipo_success_rate": success_rate,
        "volatility_20d": volatility,
    }


def score_risk_penalty(
    financial: Optional[FinancialMetric],
    structure: Optional[IpoStructureMetric],
    news: Optional[NewsMetric],
) -> tuple[float, dict[str, Any]]:
    penalty = 0.0
    reasons: list[str] = []
    if financial:
        if financial.operating_profit is not None and financial.operating_profit < 0:
            penalty += 3
            reasons.append("영업손실")
        if financial.has_negative_ocf:
            penalty += 2
            reasons.append("영업현금흐름 음수")
        if financial.debt_ratio is not None and financial.debt_ratio > 3:
            penalty += 2
            reasons.append("부채비율 높음")
    if structure:
        if structure.secondary_share_ratio is not None and structure.secondary_share_ratio > 0.5:
            penalty += 3
            reasons.append("구주매출 과다")
        if (
            structure.free_float_ratio_on_listing is not None
            and structure.free_float_ratio_on_listing > 0.5
        ):
            penalty += 3
            reasons.append("유통가능물량 과다")
        risk_flags = list(dict.fromkeys(structure.risk_flags_json or []))
        penalty += min(len(risk_flags) * 2, 6)
        reasons.extend(str(flag) for flag in risk_flags[:3])
    if news and (news.risk_keyword_count or 0) > 0:
        penalty += min(news.risk_keyword_count or 0, 5)
        reasons.append("뉴스 리스크 사건")
    penalty = min(penalty, 20)
    return penalty, {"penalty": penalty, "reasons": reasons}


def recommendation_band(total_score: float, rules: Optional[ScoringRules] = None) -> str:
    active_rules = rules or load_scoring_rules()
    if total_score >= active_rules.bands["strong"]:
        return "Strong"
    if total_score >= active_rules.bands["watch"]:
        return "Watch"
    if total_score >= active_rules.bands["neutral"]:
        return "Neutral"
    return "Avoid"


def _scale(value: float, native_max: float, configured_max: float) -> float:
    if native_max <= 0:
        return 0.0
    return max(min(value / native_max * configured_max, configured_max), 0)


def _input_fingerprint(ipo: Ipo, *rows: Any) -> str:
    payload = {
        "ipo": {
            "offer_price_min": ipo.offer_price_min,
            "offer_price_max": ipo.offer_price_max,
            "offer_price_final": ipo.offer_price_final,
            "offer_amount_million": ipo.offer_amount_million,
            "subscription_end_date": ipo.subscription_end_date,
            "refund_date": ipo.refund_date,
        },
        "underwriters": [
            {
                column.name: getattr(underwriter, column.name)
                for column in underwriter.__table__.columns
                if column.name not in {"id", "ipo_id", "updated_at"}
            }
            for underwriter in sorted(ipo.underwriters, key=lambda item: item.broker_name)
        ],
        "rows": [
            None
            if row is None
            else {
                "table": row.__tablename__,
                "values": {
                    column.name: getattr(row, column.name)
                    for column in row.__table__.columns
                    if column.name not in {"id", "ipo_id", "updated_at", "created_at"}
                },
            }
            for row in rows
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _latest(session: Session, model, ipo_id: int):
    stmt = (
        select(model).where(model.ipo_id == ipo_id).order_by(desc(model.updated_at), desc(model.id))
    )
    return session.execute(stmt).scalars().first()


def _latest_financial(session: Session, ipo_id: int) -> Optional[FinancialMetric]:
    stmt = (
        select(FinancialMetric)
        .where(FinancialMetric.ipo_id == ipo_id)
        .order_by(desc(FinancialMetric.fiscal_year), desc(FinancialMetric.updated_at))
    )
    return session.execute(stmt).scalars().first()


def _latest_market(session: Session) -> Optional[MarketConditionSnapshot]:
    stmt = select(MarketConditionSnapshot).order_by(
        desc(MarketConditionSnapshot.as_of_date), desc(MarketConditionSnapshot.id)
    )
    return session.execute(stmt).scalars().first()
