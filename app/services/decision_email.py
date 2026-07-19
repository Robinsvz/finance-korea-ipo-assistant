from __future__ import annotations

from datetime import date, datetime
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from app.models import Ipo, ScoreSnapshot
from app.services.backtest import BacktestReport, build_backtest_report
from app.services.decision import DecisionAssessment, DecisionPolicy, assess_ipo_decision
from app.services.email import EmailContent
from app.services.email_formatter import (
    DecisionEmailItem,
    ScheduleEmailItem,
    build_decision_email,
)
from app.services.outcome import latest_actionable_score
from app.services.scoring_v3 import resolve_scoring_identity
from app.services.validation import DataReadiness, assess_data_readiness

ACTION_LABELS = {
    "CANDIDATE": "청약 후보",
    "RESEARCH_ONLY": "검증 대기",
    "VALUE_SKIP": "가치 기준 제외",
    "DATA_REQUIRED": "데이터 부족",
    "BLOCKED": "중대 위험 차단",
}
ACTION_GUIDANCE = {
    "CANDIDATE": "증권사별 배정 조건과 청약 마감 시간을 확인하세요.",
    "RESEARCH_ONLY": "추가 검증 전에는 청약 후보로 해석하지 마세요.",
    "VALUE_SKIP": "현재 조건에서는 자금 투입을 보류하세요.",
    "DATA_REQUIRED": "누락된 공시·수요예측 데이터를 먼저 확인하세요.",
    "BLOCKED": "중대 위험 사유를 해소하기 전에는 청약하지 마세요.",
}
SCHEDULE_FIELDS = (
    ("수요예측 시작", "demand_start_date"),
    ("수요예측 종료", "demand_end_date"),
    ("청약 시작", "subscription_start_date"),
    ("청약 마감", "subscription_end_date"),
    ("환불", "refund_date"),
    ("납입", "payment_date"),
    ("상장", "listing_date"),
)


def build_period_decision_email(
    session: Session,
    ipos: Iterable[Ipo],
    *,
    schedule_items: Iterable[ScheduleEmailItem] = (),
    user_cash: int,
    title: str,
    period_label: str,
    generated_at: datetime,
    minimum_samples: int = 30,
    model_version: Optional[str] = None,
    rules_hash: Optional[str] = None,
) -> EmailContent:
    active_model, active_rules_hash = resolve_scoring_identity(model_version, rules_hash)
    policy = DecisionPolicy(minimum_samples=minimum_samples)
    readiness = assess_data_readiness(
        session,
        model_version=active_model,
        rules_hash=active_rules_hash,
        minimum_samples=minimum_samples,
    )
    report_cache: dict[tuple[str, Optional[str]], BacktestReport] = {}
    blocked_report = _data_readiness_blocked_report(minimum_samples)
    items: list[DecisionEmailItem] = []
    for ipo in ipos:
        score = latest_actionable_score(
            session,
            ipo.id,
            ipo.subscription_start_date,
            ipo.actual_listing_date or ipo.listing_date,
            model_version=active_model,
            rules_hash=active_rules_hash,
        )
        report = None
        if score is not None:
            key = (score.model_version, score.rules_hash)
            report = report_cache.get(key)
            if report is None and readiness.validation_ready:
                report = build_backtest_report(
                    session,
                    model_version=score.model_version,
                    rules_hash=score.rules_hash,
                    minimum_samples=minimum_samples,
                    validation_eligible_only=True,
                )
                report_cache[key] = report
            elif report is None:
                report = blocked_report
        assessment = assess_ipo_decision(
            session,
            ipo,
            score,
            user_cash=user_cash,
            policy=policy,
            report=report,
        )
        items.append(decision_email_item(ipo, score, assessment, minimum_samples))
    items.sort(key=_decision_sort_key)
    return build_decision_email(
        title,
        period_label,
        items,
        generated_at,
        _readiness_note(readiness),
        schedule_items=schedule_items,
    )


def decision_email_item(
    ipo: Ipo,
    score: Optional[ScoreSnapshot],
    assessment: DecisionAssessment,
    minimum_samples: int,
) -> DecisionEmailItem:
    return DecisionEmailItem(
        corp_name=ipo.corp_name,
        decision=ACTION_LABELS.get(assessment.action, assessment.action),
        next_action=ACTION_GUIDANCE.get(
            assessment.action,
            "공모 일정과 공시 원문을 직접 확인하세요.",
        ),
        total_score=float(score.total_score) if score else None,
        confidence_score=float(score.confidence_score or 0) if score else None,
        reasons=assessment.reasons,
        risks=assessment.risks,
        subscription_start_date=ipo.subscription_start_date,
        subscription_end_date=ipo.subscription_end_date,
        refund_date=ipo.refund_date,
        listing_date=ipo.actual_listing_date or ipo.listing_date,
        offer_price=ipo.offer_price_final or ipo.offer_price_max or ipo.offer_price_min,
        broker_name=assessment.broker_name,
        expected_shares=assessment.expected_shares,
        break_even_return_pct=assessment.break_even_return_pct,
        expected_net_profit=assessment.expected_net_profit,
        positive_probability=assessment.positive_probability,
        validation_sample_count=assessment.validation_sample_count,
        validation_summary=_validation_summary(assessment, minimum_samples),
    )


def _validation_summary(assessment: DecisionAssessment, minimum_samples: int) -> str:
    if assessment.positive_probability is not None:
        median = (
            f" / D1 중앙값 {assessment.benchmark_median_return_pct:.1f}%"
            if assessment.benchmark_median_return_pct is not None
            else ""
        )
        return (
            f"동일 등급 n={assessment.validation_sample_count} / "
            f"D1 양수 {assessment.positive_probability:.1%}{median}"
        )
    if assessment.validation_status == "insufficient-sample":
        return (
            f"검증 표본 {assessment.validation_sample_count}/{minimum_samples} — "
            "확률·기대수익 미제공"
        )
    if assessment.validation_status == "incomplete-outcomes":
        return "성과값 불완전 — 확률·기대수익 미제공"
    labels = {
        "data-readiness-blocked": "데이터 검증 준비 중",
        "insufficient-data": "필수 데이터 부족",
        "missing-score": "사전 점수 없음",
        "blocked": "중대 위험 차단",
        "not-eligible": "가치 기준 제외",
    }
    return f"{labels.get(assessment.validation_status, '검증 준비 중')} — 확률·기대수익 미제공"


def _readiness_note(readiness: DataReadiness) -> str:
    if readiness.validation_ready:
        return (
            f"누수 없는 검증 표본 {readiness.complete_pair_count}건 확보 — "
            "동일 모델·규칙 결과만 사용합니다."
        )
    return (
        f"검증 표본 {readiness.complete_pair_count}/{readiness.minimum_samples} 확보 중 — "
        "충분한 표본 전에는 확률·기대수익을 표시하지 않습니다."
    )


def build_schedule_email_items(
    ipos: Iterable[Ipo],
    start: date,
    end: date,
) -> tuple[ScheduleEmailItem, ...]:
    items = [
        ScheduleEmailItem(corp_name=ipo.corp_name, event_label=label, event_date=value)
        for ipo in ipos
        for label, field_name in SCHEDULE_FIELDS
        if (value := getattr(ipo, field_name)) is not None and start <= value <= end
    ]
    return tuple(
        sorted(items, key=lambda item: (item.event_date, item.corp_name, item.event_label))
    )


def _data_readiness_blocked_report(minimum_samples: int) -> BacktestReport:
    return BacktestReport(
        evaluated_count=0,
        prediction_accuracy=None,
        average_open_return_pct=None,
        average_d1_return_pct=None,
        strong_watch_accuracy=None,
        avoid_accuracy=None,
        minimum_samples=minimum_samples,
        validation_status="data-readiness-blocked",
    )


def _decision_sort_key(item: DecisionEmailItem) -> tuple[int, date, str]:
    priority = {
        "청약 후보": 0,
        "검증 대기": 1,
        "중대 위험 차단": 2,
        "데이터 부족": 3,
        "가치 기준 제외": 4,
    }
    return (
        priority.get(item.decision, 5),
        item.subscription_end_date or date.max,
        item.corp_name,
    )
