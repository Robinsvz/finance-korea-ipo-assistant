from __future__ import annotations

from datetime import date
from typing import Iterable, Optional

from app.models import (
    FinancialMetric,
    Ipo,
    NewsMetric,
    OutcomeEvaluation,
    ScoreSnapshot,
    SearchTrend,
)
from app.schemas import AllocationResult, HealthStatus
from app.services.allocation import equal_expectation_label
from app.services.backtest import BacktestReport, OperationalBacktest

DISCLAIMER = "참고용 자동 분류이며 투자 추천이나 청약 실행 자동화가 아닙니다."


def format_health(status: HealthStatus) -> str:
    return "\n".join(
        [
            "[healthcheck]",
            f"database: {'ok' if status.database_ok else 'fail'}",
            f"schema: {'current' if status.schema_current else 'outdated'} "
            f"({status.schema_revision or 'missing'} / "
            f"expected {status.expected_schema_revision or '-'})",
            f"telegram: {'configured' if status.telegram_configured else 'missing'}",
            f"email: {'configured' if status.email_configured else 'missing'}",
            f"dart: {'configured' if status.dart_configured else 'missing'}",
            f"naver: {'configured' if status.naver_configured else 'missing'}",
            f"database_url: {status.database_url}",
        ]
    )


def format_ipo_summary(ipo: Ipo) -> str:
    brokers = ", ".join([u.broker_name for u in ipo.underwriters]) or "-"
    return "\n".join(
        [
            f"[공모주] {ipo.corp_name}",
            f"시장: {ipo.market or '-'}",
            f"청약일: {_date_range(ipo.subscription_start_date, ipo.subscription_end_date)}",
            f"환불일: {_fmt_date(ipo.refund_date, missing='미확정')}",
            f"납입일: {_fmt_date(ipo.payment_date)}",
            f"상장일: {_fmt_date(ipo.listing_date)}",
            f"확정 공모가: {_money(ipo.offer_price_final)}",
            f"주관사: {brokers}",
            f"상태: {ipo.status}",
            DISCLAIMER,
        ]
    )


def format_schedule(
    title: str,
    ipos: Iterable[Ipo],
    start: Optional[date] = None,
    end: Optional[date] = None,
) -> str:
    rows = list(ipos)
    if not rows:
        return f"[{title}]\n일정 없음"
    events = []
    for ipo in rows:
        brokers = ", ".join([u.broker_name for u in ipo.underwriters[:3]]) or "-"
        label = f"{ipo.corp_name} ({brokers})"
        dated_events = (
            (ipo.demand_start_date, "수요예측 시작"),
            (ipo.demand_end_date, "수요예측 마감"),
            (ipo.subscription_start_date, "청약 시작"),
            (ipo.subscription_end_date, "청약 마감"),
            (ipo.refund_date, "환불"),
            (ipo.payment_date, "납입"),
            (ipo.listing_date, "상장"),
        )
        added = False
        for event_date, event_name in dated_events:
            if (
                event_date is not None
                and (start is None or event_date >= start)
                and (end is None or event_date <= end)
            ):
                events.append((event_date, event_name, label))
                added = True
        if not added and start is None and end is None:
            events.append((date.max, "일정 미정", label))
    if not events:
        return f"[{title}]\n일정 없음"
    lines = [f"[{title}]"]
    for event_date, event_name, label in sorted(
        events, key=lambda item: (item[0], item[1], item[2])
    ):
        shown_date = "-" if event_date == date.max else event_date.isoformat()
        lines.append(f"- {shown_date} · {event_name}: {label}")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def format_score(ipo: Ipo, score: Optional[ScoreSnapshot]) -> str:
    if score is None:
        return f"[공모주 평가] {ipo.corp_name}\n점수 스냅샷 없음"
    missing = ", ".join(score.missing_features_json or []) or "없음"
    lines = [
        f"[공모주 평가] {ipo.corp_name}",
        f"청약일: {_date_range(ipo.subscription_start_date, ipo.subscription_end_date)}",
        f"확정 공모가: {_money(ipo.offer_price_final)}",
        "",
        f"종합 점수: {score.total_score:.1f}점 / {score.recommendation_band}",
        f"데이터 완성도: {score.confidence_score:.1f}%",
        f"모델: {score.model_version}",
        "",
    ]
    if score.model_version == "rules-v3-simple":
        pillars = dict((score.explanation_json or {}).get("pillars") or {})
        gates = dict((score.explanation_json or {}).get("gates") or {})
        lines.extend(
            [
                "4대 판단축:",
                _pillar_line("기관 수요", pillars.get("demand"), score.demand_score, 45),
                _pillar_line("상장일 공급압력", pillars.get("supply"), score.structure_score, 30),
                _pillar_line("가격 매력도", pillars.get("pricing"), score.valuation_score, 15),
                _pillar_line("시장 국면", pillars.get("market"), score.market_condition_score, 10),
                "- 재무·뉴스·예상 배정은 총점과 분리해 위험/실행가치로 확인",
                "",
                f"Gate: {gates.get('status', '-')}",
            ]
        )
        warnings = gates.get("review_warnings") or gates.get("review_reasons") or []
        blocked = gates.get("blocked_reasons") or gates.get("hard_block_reasons") or []
        if blocked:
            lines.append(f"차단 사유: {', '.join(str(value) for value in blocked)}")
        if warnings:
            lines.append(f"검토 경고: {', '.join(str(value) for value in warnings)}")
    else:
        lines.extend(
            [
                "세부 점수:",
                f"- 기관 수요예측: {score.demand_score:.1f}/25",
                f"- 재무 품질: {score.financial_score:.1f}/15",
                f"- 밸류에이션: {score.valuation_score:.1f}/15",
                f"- 공모 구조: {score.structure_score:.1f}/15",
                f"- 뉴스/검색 트렌드: {score.news_trend_score:.1f}/10",
                f"- 예상 배정 효율: {score.allocation_score:.1f}/10",
                f"- 시장 환경: {score.market_condition_score:.1f}/10",
                f"- 리스크 패널티: -{score.risk_penalty_score:.1f}",
            ]
        )
    lines.extend(["", f"누락 지표: {missing}", DISCLAIMER])
    return "\n".join(lines)


def _pillar_line(label: str, payload, fallback_score: float, fallback_max: float) -> str:
    values = payload if isinstance(payload, dict) else {}
    score = float(values.get("score", fallback_score) or 0)
    maximum = float(values.get("max_score", fallback_max) or fallback_max)
    return f"- {label}: {score:.1f}/{maximum:.0f}"


def format_allocation(ipo: Ipo, result: AllocationResult) -> str:
    equal_label = equal_expectation_label(result.expected_equal_shares_mid)
    return "\n".join(
        [
            f"[예상 배정] {ipo.corp_name}",
            f"증권사: {result.broker_name}",
            f"증거금: {_money(result.user_cash)}",
            f"공모가: {_money(result.offer_price)}",
            f"청약 가능 수량: {result.apply_shares:,}주",
            f"필요 증거금: {_money(result.estimated_required_deposit)}",
            f"균등 예상: {result.expected_equal_shares_mid:.2f}주 ({equal_label})",
            f"비례 예상: 보수 {result.expected_proportional_shares_conservative:.2f}주 / "
            f"기준 {result.expected_proportional_shares_base:.2f}주 / "
            f"낙관 {result.expected_proportional_shares_optimistic:.2f}주",
            f"수수료: {_money(result.estimated_fee)}",
            f"가정 출처: {result.assumption_source}",
            f"가정 갱신: {_fmt_datetime(result.assumption_updated_at)}",
            DISCLAIMER,
        ]
    )


def format_trend(ipo: Ipo, news: Optional[NewsMetric], trend: Optional[SearchTrend]) -> str:
    lines = [f"[뉴스/검색 트렌드] {ipo.corp_name}"]
    if news:
        recent = news.recent_7d_article_count or 0
        lines.append(
            f"뉴스: 최근 {news.deduped_article_count or 0}건, 최근 7일 {recent}건, "
            f"증가율 {(news.article_growth_rate_7d or 0):.1%}"
        )
        lines.append(f"리스크 키워드: {news.risk_keyword_count or 0}회")
        if news.summary:
            lines.append(news.summary)
    else:
        lines.append("뉴스 데이터 없음")
    if trend:
        baseline = trend.baseline_avg_index
        lines.append(
            f"검색: 최근 평균 {trend.recent_avg_index:.1f}, 기준 평균 {baseline:.1f}, "
            f"상승률 {trend.trend_growth_rate:.1%}, zero ratio {trend.zero_data_ratio:.1%}"
        )
    else:
        lines.append("검색 트렌드 데이터 없음")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def format_financial(ipo: Ipo, metric: Optional[FinancialMetric]) -> str:
    if metric is None:
        return f"[재무 요약] {ipo.corp_name}\n재무 데이터 없음"
    return "\n".join(
        [
            f"[재무 요약] {ipo.corp_name} ({metric.fiscal_year})",
            f"매출: {_money(metric.revenue)} / YoY: {_financial_yoy(metric)}",
            f"영업이익: {_money(metric.operating_profit)} / "
            f"영업이익률: {_pct(metric.operating_margin)}",
            f"순이익: {_money(metric.net_income)} / 순이익률: {_pct(metric.net_margin)}",
            f"부채비율: {_pct(metric.debt_ratio)}",
            f"영업현금흐름: {_money(metric.operating_cash_flow)}",
            f"리스크: {'영업현금흐름 음수' if metric.has_negative_ocf else '특이사항 없음'}",
            DISCLAIMER,
        ]
    )


def format_result(ipo: Ipo, evaluation: Optional[OutcomeEvaluation]) -> str:
    if evaluation is None:
        return f"[상장 후 검증] {ipo.corp_name}\n검증 결과 없음"
    return "\n".join(
        [
            f"[상장 후 검증] {ipo.corp_name}",
            f"공모가: {_money(evaluation.offer_price)}",
            f"시초가 수익률: {_pct(evaluation.d0_open_return_pct, already_pct=True)}",
            f"D0 종가 수익률: {_pct(evaluation.d0_close_return_pct, already_pct=True)}",
            f"D1 종가 수익률: {_pct(evaluation.d1_close_return_pct, already_pct=True)}",
            f"D3 종가 수익률: {_pct(evaluation.d3_close_return_pct, already_pct=True)}",
            f"예측 적중: {_bool(evaluation.prediction_was_correct)}",
            DISCLAIMER,
        ]
    )


def format_backtest(report: BacktestReport) -> str:
    lines = [
        "[공모주 백테스트 선택 집합]",
        f"연결된 점수-성과 쌍: {report.evaluated_count}",
        f"검증 상태: {report.validation_status}",
        f"모델: {', '.join(report.model_versions) or '-'}",
        f"규칙 해시 수: {len(report.rules_hashes)}",
        f"방향성 적중률: {_pct(report.prediction_accuracy)}",
        f"Strong/Watch 적중률: {_pct(report.strong_watch_accuracy)}",
        f"Avoid 적중률: {_pct(report.avoid_accuracy)}",
        f"Neutral 보조 적중률: {_pct(report.neutral_accuracy)}",
        f"평균 시초가 수익률: {_pct(report.average_open_return_pct, already_pct=True)}",
        f"중앙 시초가 수익률: {_pct(report.median_open_return_pct, already_pct=True)}",
        f"평균 D1 종가 수익률: {_pct(report.average_d1_return_pct, already_pct=True)}",
        f"중앙 D1 종가 수익률: {_pct(report.median_d1_return_pct, already_pct=True)}",
        f"D1 손실 확률: {_pct(report.loss_probability)}",
        "",
        "등급별:",
    ]
    for band, metrics in report.band_metrics.items():
        warning = " / 소표본" if metrics.small_sample_warning else ""
        lines.append(
            f"- {band}: n={metrics.sample_count}, 적중 {_pct(metrics.prediction_accuracy)}, "
            f"D1 평균 {_pct(metrics.average_d1_return_pct, already_pct=True)}{warning}"
        )
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def format_operational_backtest(bundle: OperationalBacktest) -> str:
    report = bundle.validation
    readiness = bundle.readiness
    gross = bundle.gross_outcomes
    model_ready = readiness.validation_ready and report.validation_status == "ready"
    lines = [
        "[월간 공모주 운영 검증]",
        f"운영 모델: {bundle.model_version}",
        f"규칙 해시: {bundle.rules_hash[:12]}…",
        f"검증 준비: {'완료' if model_ready else '미완료'}",
        (f"적격 점수-D0/D1 쌍: {readiness.complete_pair_count}/{readiness.minimum_samples}"),
    ]
    if readiness.blockers:
        lines.append("차단 요인:")
        lines.extend(f"- {blocker}" for blocker in readiness.blockers)

    lines.extend(
        [
            "",
            "[Gross 기술통계 — 모델 성과 아님]",
            f"실제 IPO 성과 레코드: {gross.ipo_count}",
            (
                f"D0 시초가: n={gross.d0_open.sample_count}, "
                f"평균 {_pct(gross.d0_open.average_return_pct, already_pct=True)}, "
                f"중앙 {_pct(gross.d0_open.median_return_pct, already_pct=True)}, "
                f"양수 비율 {_pct(gross.d0_open.positive_probability)}"
            ),
            (
                f"D1 종가: n={gross.d1_close.sample_count}, "
                f"평균 {_pct(gross.d1_close.average_return_pct, already_pct=True)}, "
                f"중앙 {_pct(gross.d1_close.median_return_pct, already_pct=True)}, "
                f"양수 비율 {_pct(gross.d1_close.positive_probability)}"
            ),
            "수수료·배정 확률·현금 기회비용을 반영하지 않은 기술통계입니다.",
            "",
            "[운영 모델 검증]",
        ]
    )
    if not model_ready:
        lines.extend(
            [
                "준비도 gate 미충족으로 적중률·확률·기대수익을 제공하지 않습니다.",
                f"내부 검증 상태: {report.validation_status}",
            ]
        )
    else:
        lines.extend(
            [
                f"검증 표본: {report.evaluated_count}",
                f"방향성 적중률: {_pct(report.prediction_accuracy)}",
                f"Strong/Watch 적중률: {_pct(report.strong_watch_accuracy)}",
                f"Avoid 적중률: {_pct(report.avoid_accuracy)}",
                (
                    "D1 평균/중앙: "
                    f"{_pct(report.average_d1_return_pct, already_pct=True)} / "
                    f"{_pct(report.median_d1_return_pct, already_pct=True)}"
                ),
                f"D1 손실 확률: {_pct(report.loss_probability)}",
            ]
        )
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def _fmt_date(value: Optional[date], *, missing: str = "-") -> str:
    return value.isoformat() if value else missing


def _date_range(start: Optional[date], end: Optional[date]) -> str:
    if start and end and start != end:
        return f"{start.isoformat()} ~ {end.isoformat()}"
    return _fmt_date(start or end)


def _money(value) -> str:
    if value is None:
        return "-"
    return f"{float(value):,.0f}원"


def _pct(value, already_pct: bool = False) -> str:
    if value is None:
        return "-"
    pct = value if already_pct else value * 100
    return f"{pct:+.1f}%"


def _bool(value: Optional[bool]) -> str:
    if value is None:
        return "-"
    return "예" if value else "아니오"


def _fmt_datetime(value) -> str:
    return value.isoformat() if value else "-"


def _financial_yoy(metric: FinancialMetric) -> str:
    if metric.revenue_yoy_status == "prior_zero":
        return "전년도 매출 0으로 산정 불가"
    if metric.revenue_yoy_status == "missing_prior":
        return "전년도 데이터 없음"
    return _pct(metric.revenue_yoy_growth)
