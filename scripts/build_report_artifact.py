from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Ipo, OutcomeEvaluation
from app.services.clock import local_now
from app.services.scoring_v3 import resolve_scoring_identity
from app.services.validation import assess_data_readiness

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "analysis" / "ipo_report_artifact.json"
MINIMUM_SAMPLES = 30
TITLE = "Korea IPO Assistant 개선 및 검증 리포트"


def main() -> None:
    model_version, rules_hash = resolve_scoring_identity()
    with SessionLocal() as session:
        readiness = assess_data_readiness(
            session,
            model_version=model_version,
            rules_hash=rules_hash,
            minimum_samples=MINIMUM_SAMPLES,
        )
        outcomes = _canonical_outcomes(session)
    generated_at = local_now().isoformat(timespec="seconds")
    as_of = readiness.as_of_date.isoformat()
    sources = _sources(as_of, generated_at, model_version, rules_hash)
    artifact = {
        "surface": "report",
        "manifest": _manifest(generated_at, sources, readiness.complete_pair_count),
        "snapshot": _snapshot(readiness, outcomes, generated_at, model_version),
        "sources": sources,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(artifact, ensure_ascii=False))


def _canonical_outcomes(session) -> list[tuple[Ipo, OutcomeEvaluation]]:
    rows = session.execute(
        select(Ipo, OutcomeEvaluation)
        .join(OutcomeEvaluation, OutcomeEvaluation.ipo_id == Ipo.id)
        .where(
            Ipo.is_sample.is_(False),
            OutcomeEvaluation.d0_open_return_pct.is_not(None),
            OutcomeEvaluation.d1_close_return_pct.is_not(None),
        )
    ).all()
    canonical: dict[int, tuple[Ipo, OutcomeEvaluation]] = {}
    for ipo, outcome in rows:
        current = canonical.get(ipo.id)
        if current is None or _outcome_order(outcome) > _outcome_order(current[1]):
            canonical[ipo.id] = (ipo, outcome)
    return sorted(
        canonical.values(),
        key=lambda row: (row[0].actual_listing_date or row[0].listing_date, row[0].id),
    )


def _outcome_order(outcome: OutcomeEvaluation) -> tuple[datetime, int]:
    evaluated_at = outcome.evaluated_at
    if evaluated_at.tzinfo is None:
        evaluated_at = evaluated_at.replace(tzinfo=timezone.utc)
    return evaluated_at, outcome.id


def _snapshot(readiness, outcomes, generated_at: str, model_version: str) -> dict:
    listed_count = readiness.listed_count
    headline = {
        "as_of_date": readiness.as_of_date.isoformat(),
        "listed_count": listed_count,
        "listing_price_count": readiness.listing_price_count,
        "price_coverage_rate": (
            readiness.listing_price_count / listed_count if listed_count else 0
        ),
        "outcome_count": readiness.outcome_count,
        "complete_pair_count": readiness.complete_pair_count,
        "minimum_samples": readiness.minimum_samples,
        "sample_ipo_count": readiness.sample_ipo_count,
        "model_version": model_version,
        "rules_hash": readiness.selected_rules_hash,
    }
    coverage = [
        _coverage_row("상장 완료", listed_count, listed_count, "완료", readiness),
        _coverage_row("티커 확보", readiness.ticker_count, listed_count, "완료", readiness),
        _coverage_row("D0/D1 가격", readiness.listing_price_count, listed_count, "완료", readiness),
        _coverage_row("Gross 성과", readiness.outcome_count, listed_count, "완료", readiness),
        _coverage_row(
            "역사 수요예측",
            readiness.demand_count,
            listed_count,
            "완료" if readiness.demand_count == listed_count else "미수집",
            readiness,
        ),
        _coverage_row(
            "역사 유통물량",
            readiness.structure_count,
            listed_count,
            "완료" if readiness.structure_count == listed_count else "미수집",
            readiness,
        ),
        {
            "stage": "v3 완전 검증쌍",
            "count": readiness.complete_pair_count,
            "denominator": readiness.minimum_samples,
            "status": ("검증 가능" if readiness.validation_ready else "차단 요인 존재"),
            "as_of_date": readiness.as_of_date.isoformat(),
            "cohort": "subscription-open policy",
        },
    ]
    cohort_returns = []
    cohort_table = []
    for segment, spac in (("운영기업", False), ("SPAC", True)):
        segment_rows = [row for row in outcomes if ("스팩" in row[0].corp_name) == spac]
        d0 = [float(row[1].d0_open_return_pct) for row in segment_rows]
        d1 = [float(row[1].d1_close_return_pct) for row in segment_rows]
        listing_dates = [row[0].actual_listing_date or row[0].listing_date for row in segment_rows]
        date_window = (
            f"{min(listing_dates).isoformat()}~{max(listing_dates).isoformat()}"
            if listing_dates
            else "-"
        )
        d0_stats = _return_stats(d0)
        d1_stats = _return_stats(d1)
        cohort_returns.extend(
            [
                _cohort_return_row(segment, "D0_OPEN", d0_stats, date_window),
                _cohort_return_row(segment, "D1_CLOSE", d1_stats, date_window),
            ]
        )
        cohort_table.append(
            {
                "segment": segment,
                "n": len(segment_rows),
                "d0_positive_count": d0_stats["positive_count"],
                "d0_positive_rate": d0_stats["positive_rate"],
                "average_d0_return_pct": d0_stats["average_return_pct"],
                "median_d0_return_pct": d0_stats["median_return_pct"],
                "d1_positive_count": d1_stats["positive_count"],
                "d1_positive_rate": d1_stats["positive_rate"],
                "average_d1_return_pct": d1_stats["average_return_pct"],
                "median_d1_return_pct": d1_stats["median_return_pct"],
                "min_d1_return_pct": d1_stats["min_return_pct"],
                "max_d1_return_pct": d1_stats["max_return_pct"],
                "date_window": date_window,
            }
        )
    return {
        "version": 1,
        "generatedAt": generated_at,
        "status": "ready",
        "datasets": {
            "headline": [headline],
            "coverage": coverage,
            "cohort_returns": cohort_returns,
            "cohort_table": cohort_table,
        },
    }


def _coverage_row(stage: str, count: int, denominator: int, status: str, readiness) -> dict:
    return {
        "stage": stage,
        "count": count,
        "denominator": denominator,
        "status": status,
        "as_of_date": readiness.as_of_date.isoformat(),
        "cohort": "all real IPOs",
    }


def _return_stats(values: list[float]) -> dict:
    if not values:
        return {
            "n": 0,
            "positive_count": 0,
            "positive_rate": None,
            "average_return_pct": None,
            "median_return_pct": None,
            "min_return_pct": None,
            "max_return_pct": None,
        }
    return {
        "n": len(values),
        "positive_count": sum(value > 0 for value in values),
        "positive_rate": sum(value > 0 for value in values) / len(values),
        "average_return_pct": round(mean(values), 2),
        "median_return_pct": round(median(values), 2),
        "min_return_pct": round(min(values), 2),
        "max_return_pct": round(max(values), 2),
    }


def _cohort_return_row(segment: str, policy: str, stats: dict, window: str) -> dict:
    return {
        "segment": segment,
        "exit_policy": policy,
        **stats,
        "date_window": window,
    }


def _sources(
    as_of: str,
    generated_at: str,
    model_version: str,
    rules_hash: str,
) -> list[dict]:
    return [
        {
            "id": "db_readiness",
            "label": "Korea IPO local SQLite readiness snapshot",
            "query": {
                "engine": "SQLite",
                "language": "SQL",
                "executed_at": generated_at,
                "description": (
                    "Counts real IPOs by as-of-date collection and point-in-time validation stage."
                ),
                "tables_used": [
                    "ipos",
                    "listing_prices",
                    "outcome_evaluations",
                    "demand_forecasts",
                    "ipo_structure_metrics",
                    "score_snapshots",
                ],
                "filters": [
                    "ipos.is_sample = 0",
                    f"listing date <= {as_of}",
                    f"model_version = {model_version}",
                    f"rules_hash = {rules_hash}",
                    "non-empty rules_hash and hard gate status = pass",
                    "demand and supply sources have non-empty provenance",
                    "demand and supply sources were available by score time",
                    "complete pairs require non-null D0-open and D1-close returns",
                    "decision cutoff = subscription-open 09:00 Asia/Seoul",
                ],
                "metric_definitions": [
                    "Price coverage = real listed IPOs with listing_prices / real IPOs "
                    "listed by the as-of date.",
                    "Complete v3 pair = one real IPO listed by the as-of date with a "
                    "single-rule-hash rules-v3-simple score created before subscription opens, "
                    "a passed hard gate, point-in-time demand/supply provenance, and non-null "
                    "D0-open and D1-close returns.",
                ],
                "sql": f"""WITH listed AS (
  SELECT id
  FROM ipos
  WHERE is_sample = 0
    AND COALESCE(actual_listing_date, listing_date) <= '{as_of}'
), decisions AS (
  SELECT id,
         COALESCE(subscription_start_date, actual_listing_date, listing_date) AS decision_date
  FROM ipos
  WHERE is_sample = 0
), eligible_scores AS (
  SELECT s.id, s.ipo_id, s.rules_hash
  FROM score_snapshots s
  JOIN decisions d ON d.id = s.ipo_id
  WHERE s.ipo_id IN (SELECT id FROM listed)
    AND s.model_version = '{model_version}'
    AND s.rules_hash = '{rules_hash}'
    AND trim(COALESCE(s.rules_hash, '')) <> ''
    AND s.score_date <= d.decision_date
    AND s.calculated_at < datetime(d.decision_date || ' 00:00:00')
    AND json_extract(s.explanation_json, '$.gates.status') = 'pass'
    AND trim(COALESCE(json_extract(s.explanation_json, '$.sources.demand.source'), '')) <> ''
    AND trim(COALESCE(json_extract(s.explanation_json, '$.sources.supply.source'), '')) <> ''
    AND CASE
          WHEN instr(json_extract(s.explanation_json, '$.sources.demand.as_of'), 'T') = 0
          THEN date(json_extract(s.explanation_json, '$.sources.demand.as_of'))
               <= date(s.calculated_at, '+9 hours')
          ELSE datetime(json_extract(s.explanation_json, '$.sources.demand.as_of'))
               <= datetime(s.calculated_at)
        END
    AND CASE
          WHEN instr(json_extract(s.explanation_json, '$.sources.supply.as_of'), 'T') = 0
          THEN date(json_extract(s.explanation_json, '$.sources.supply.as_of'))
               <= date(s.calculated_at, '+9 hours')
          ELSE datetime(json_extract(s.explanation_json, '$.sources.supply.as_of'))
               <= datetime(s.calculated_at)
        END
), complete_by_rule AS (
  SELECT s.rules_hash, COUNT(DISTINCT o.ipo_id) AS pair_count
  FROM outcome_evaluations o
  JOIN eligible_scores s ON s.id = o.score_snapshot_id
  WHERE o.d0_open_return_pct IS NOT NULL
    AND o.d1_close_return_pct IS NOT NULL
  GROUP BY s.rules_hash
)
SELECT
  (SELECT COUNT(*) FROM listed) AS listed_count,
  (SELECT COUNT(DISTINCT ipo_id) FROM listing_prices
   WHERE ipo_id IN (SELECT id FROM listed)) AS listing_price_count,
  (SELECT COUNT(DISTINCT ipo_id) FROM outcome_evaluations
   WHERE ipo_id IN (SELECT id FROM listed)) AS outcome_count,
  COALESCE((SELECT MAX(pair_count) FROM complete_by_rule), 0) AS complete_pair_count;""",
            },
        },
        {
            "id": "db_outcomes",
            "label": "Korea IPO gross listing outcome snapshot",
            "query": {
                "engine": "SQLite + Python statistics",
                "language": "SQL",
                "executed_at": generated_at,
                "description": (
                    "Extracts one gross D0-open and D1-close outcome per real IPO; "
                    "Python computes cohort counts, means, medians, extrema, and positive rates."
                ),
                "tables_used": ["ipos", "outcome_evaluations"],
                "filters": [
                    "ipos.is_sample = 0",
                    "D0-open return is not null",
                    "D1-close return is not null",
                    "latest evaluation per IPO",
                    "SPAC identified by company name containing 스팩",
                ],
                "metric_definitions": [
                    "Gross return (%) = (market price / final offer price - 1) * 100.",
                    "Positive rate = count(return > 0) / non-null outcomes within cohort.",
                    "Fees, allocation probability, and cash opportunity cost are excluded.",
                ],
                "sql": """WITH ranked AS (
  SELECT
    i.id AS ipo_id,
    i.corp_name,
    COALESCE(i.actual_listing_date, i.listing_date) AS listing_date,
    CASE WHEN instr(i.corp_name, '스팩') > 0
         THEN 'SPAC' ELSE 'Operating company' END AS segment,
    o.d0_open_return_pct,
    o.d1_close_return_pct,
    ROW_NUMBER() OVER (
      PARTITION BY i.id ORDER BY o.evaluated_at DESC, o.id DESC
    ) AS row_number
  FROM outcome_evaluations o
  JOIN ipos i ON i.id = o.ipo_id
  WHERE i.is_sample = 0
    AND o.d0_open_return_pct IS NOT NULL
    AND o.d1_close_return_pct IS NOT NULL
)
SELECT ipo_id, corp_name, listing_date, segment,
       d0_open_return_pct, d1_close_return_pct
FROM ranked
WHERE row_number = 1
ORDER BY listing_date, ipo_id;""",
            },
        },
        {
            "id": "rules_v3",
            "label": "rules-v3-simple configuration",
            "path": "configs/scoring_rules_v3.yaml",
        },
    ]


def _manifest(generated_at: str, sources: list[dict], complete_pairs: int) -> dict:
    return {
        "version": 1,
        "surface": "report",
        "title": TITLE,
        "description": (
            "Local data readiness, gross D0/D1 outcomes, and simplified-score validation "
            "status as of 2026-07-19."
        ),
        "generatedAt": generated_at,
        "sources": sources,
        "blocks": [
            {"id": "title", "type": "markdown", "body": f"# {TITLE}"},
            {
                "id": "executive_summary",
                "type": "markdown",
                "sourceId": "db_readiness",
                "body": (
                    "## Executive Summary\n상장 완료 종목의 티커·D0/D1 가격·gross 성과 "
                    "백필은 완료됐습니다. 그러나 청약 시작 전에 계산된 "
                    f"`rules-v3-simple` 점수와 성과를 연결한 완전 표본은 {complete_pairs}건입니다. "
                    "따라서 현재 새 점수의 예측력을 주장하거나 확률·기대수익을 투자 판단에 "
                    "노출해서는 안 됩니다."
                ),
            },
            {
                "id": "headline_metrics",
                "type": "metric-strip",
                "cardIds": ["price_coverage", "gross_outcomes", "v3_pairs"],
            },
            {
                "id": "readiness_heading",
                "type": "markdown",
                "sourceId": "db_readiness",
                "body": (
                    "## 데이터 파이프라인 준비도\n일정·종목코드·가격·성과는 연결됐지만, "
                    "역사 수요예측과 상장일 유통물량 피처가 없어 모델 검증 단계에서 멈춥니다."
                ),
            },
            {"id": "readiness_chart_block", "type": "chart", "chartId": "readiness_chart"},
            {
                "id": "outcomes_heading",
                "type": "markdown",
                "sourceId": "db_outcomes",
                "body": (
                    "## 관측된 gross D0/D1 결과\n짧고 특정 시기에 집중된 기술 통계입니다. "
                    "SPAC과 운영기업, D0-open과 D1-close를 각각 분리합니다."
                ),
            },
            {"id": "outcome_chart_block", "type": "chart", "chartId": "cohort_return_chart"},
            {"id": "outcome_table_block", "type": "table", "tableId": "cohort_outcome_table"},
            {
                "id": "methodology",
                "type": "markdown",
                "body": (
                    "## 범위와 방법\n샘플 IPO는 `is_sample`로 제외합니다. 점수는 청약 시작일 "
                    "09:00 KST 이전 생성분 중 gate를 통과하고 당시 수요·공급 근거가 확인된 "
                    "경우만 인정합니다. IPO·모델·규칙별 최신 스냅샷 하나만 사용하며, D0는 "
                    "실제 상장 거래일 시가, D1은 다음 거래일 종가입니다."
                ),
            },
            {
                "id": "limitations",
                "type": "markdown",
                "body": (
                    "## 한계와 강건성\n성과는 gross 기준이며 수수료·배정 확률·현금 점유 비용을 "
                    "포함하지 않습니다. 표본이 작고 2026년 일부 기간에 집중돼 있습니다. v3 "
                    "가중치는 역사 피처가 채워질 때까지 경험적으로 검증된 계수가 아닙니다."
                ),
            },
            {
                "id": "next_steps",
                "type": "markdown",
                "body": (
                    "## 다음 실행 단계\n1. OpenDART 수요예측·공모 구조를 관측시각과 원문 해시로 "
                    "불변 저장합니다.\n2. 완전 표본 30건 전에는 확률·기대수익을 비웁니다.\n"
                    "3. 30건 이후 SPAC/운영기업과 D0/D1을 분리해 walk-forward 검증합니다.\n"
                    "4. 실제 참여·배정·매도 원장으로 개인 순이익을 별도 검증합니다."
                ),
            },
            {
                "id": "questions",
                "type": "markdown",
                "body": (
                    "## 추가 검증 질문\n어떤 피처가 시간순 검증에서 안정적인가? 점수 구간별 "
                    "양수확률의 Wilson 구간이 정책 하한을 넘는가? 실제 순이익은 gross "
                    "수익률과 얼마나 다른가?"
                ),
            },
        ],
        "cards": _cards(),
        "charts": _charts(),
        "tables": _tables(),
    }


def _cards() -> list[dict]:
    return [
        {
            "id": "price_coverage",
            "dataset": "headline",
            "description": "상장 완료 실 IPO 중 D0/D1 가격이 확보된 비율.",
            "sourceId": "db_readiness",
            "metrics": [
                {
                    "label": "D0/D1 가격 커버리지",
                    "field": "price_coverage_rate",
                    "format": "percent",
                },
                {"label": "상장 종목", "field": "listed_count", "format": "number"},
            ],
        },
        {
            "id": "gross_outcomes",
            "dataset": "headline",
            "description": "최종 공모가와 실제 D0/D1 가격을 조인한 gross 성과 건수.",
            "sourceId": "db_readiness",
            "metrics": [
                {"label": "Gross 성과", "field": "outcome_count", "format": "number"},
                {
                    "label": "가격 확보",
                    "field": "listing_price_count",
                    "format": "number",
                },
            ],
        },
        {
            "id": "v3_pairs",
            "dataset": "headline",
            "description": (
                "청약 시작 전 gate·시점 근거를 충족한 단일 규칙 v3 점수와 "
                "D0/D1이 존재하는 고유 IPO 수."
            ),
            "sourceId": "db_readiness",
            "metrics": [
                {
                    "label": "누수 없는 v3 표본",
                    "field": "complete_pair_count",
                    "format": "number",
                },
                {"label": "최소 기준", "field": "minimum_samples", "format": "number"},
            ],
        },
    ]


def _charts() -> list[dict]:
    return [
        {
            "id": "readiness_chart",
            "title": "검증 파이프라인 단계별 확보 건수",
            "subtitle": "가격·성과와 달리 역사 피처·v3 완전 표본은 비어 있습니다.",
            "type": "horizontalBar",
            "intent": "comparison",
            "question": "모델 검증 파이프라인은 어느 단계에서 막히는가?",
            "rationale": "단계별 건수를 수평 막대로 비교해 결손 지점을 확인합니다.",
            "dataset": "coverage",
            "sourceId": "db_readiness",
            "encodings": {
                "x": {"field": "stage", "type": "nominal", "label": "단계"},
                "y": {"field": "count", "type": "quantitative", "label": "확보 건수"},
                "tooltip": [
                    {"field": "denominator", "type": "quantitative", "label": "기준"},
                    {"field": "status", "type": "text", "label": "상태"},
                    {"field": "as_of_date", "type": "temporal", "label": "기준일"},
                ],
            },
            "valueFormat": "number",
            "settings": {"orientation": "horizontal", "sort": "custom", "showValues": True},
            "surface": {"surface": "card", "viewMode": "both"},
        },
        {
            "id": "cohort_return_chart",
            "title": "코호트·매도정책별 gross 중앙값 수익률",
            "subtitle": "SPAC과 운영기업의 D0/D1 결과를 분리한 소표본 기술 통계입니다.",
            "type": "bar",
            "intent": "comparison",
            "question": "SPAC과 운영기업의 D0-open 및 D1-close 결과가 어떻게 다른가?",
            "rationale": "코호트와 매도정책을 그룹 막대로 분리해 평균 합산 왜곡을 피합니다.",
            "comparisonContext": {
                "grain": "cohort × exit policy",
                "unit": "percentage points",
                "denominator": "non-null gross return outcomes",
            },
            "dataset": "cohort_returns",
            "sourceId": "db_outcomes",
            "encodings": {
                "x": {"field": "segment", "type": "nominal", "label": "코호트"},
                "y": {
                    "field": "median_return_pct",
                    "type": "quantitative",
                    "label": "중앙값 gross 수익률",
                    "unit": "%",
                },
                "color": {"field": "exit_policy", "type": "nominal", "label": "매도정책"},
                "tooltip": [
                    {"field": "n", "type": "quantitative", "label": "표본"},
                    {
                        "field": "positive_rate",
                        "type": "quantitative",
                        "format": "percent",
                        "label": "양수 비율",
                    },
                    {
                        "field": "average_return_pct",
                        "type": "quantitative",
                        "label": "평균",
                        "unit": "%",
                    },
                    {"field": "date_window", "type": "text", "label": "상장 기간"},
                ],
            },
            "valueFormat": "number",
            "unit": "%",
            "settings": {"groupMode": "grouped", "showValues": True},
            "legend": {"position": "bottom", "title": "매도정책"},
            "referenceLines": [{"axis": "y", "value": 0, "label": "공모가 기준"}],
            "surface": {"surface": "card", "viewMode": "both"},
        },
    ]


def _tables() -> list[dict]:
    return [
        {
            "id": "cohort_outcome_table",
            "title": "코호트별 gross 성과 상세",
            "subtitle": "D0-open과 D1-close를 분리한 기술 통계; 비용 미반영.",
            "dataset": "cohort_table",
            "sourceId": "db_outcomes",
            "defaultSort": {"field": "d1_positive_rate", "direction": "desc"},
            "density": "dense",
            "columns": [
                {"field": "segment", "label": "코호트", "type": "text"},
                {"field": "n", "label": "n", "format": "number"},
                {"field": "d0_positive_rate", "label": "D0 양수", "format": "percent"},
                {
                    "field": "median_d0_return_pct",
                    "label": "D0 중앙값 (%)",
                    "format": "number",
                },
                {"field": "d1_positive_rate", "label": "D1 양수", "format": "percent"},
                {
                    "field": "median_d1_return_pct",
                    "label": "D1 중앙값 (%)",
                    "format": "number",
                },
                {
                    "field": "min_d1_return_pct",
                    "label": "D1 최소 (%)",
                    "format": "number",
                },
                {
                    "field": "max_d1_return_pct",
                    "label": "D1 최대 (%)",
                    "format": "number",
                },
            ],
        }
    ]


if __name__ == "__main__":
    main()
