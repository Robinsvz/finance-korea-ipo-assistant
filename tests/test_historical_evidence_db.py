from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.models import Ipo, OutcomeEvaluation, ScoreSnapshot
from app.services.historical_evidence import (
    HistoricalEvidenceAvailability,
    HistoricalReturnDistribution,
    _return_distribution,
    build_db_historical_evidence_availability,
)
from app.services.monthly_email import (
    _historical_html,
    _historical_text,
    _history_distribution_text,
    _history_gate_label,
)
from app.services.scoring_v3 import resolve_scoring_identity


def _ipo(name: str, *, ipo_type: str = "NORMAL", origin: str = "kind") -> Ipo:
    return Ipo(
        corp_name=name,
        normalized_name=name,
        ipo_type=ipo_type,
        data_origin=origin,
        is_sample=origin == "sample",
        subscription_start_date=date(2026, 6, 20),
        listing_date=date(2026, 7, 1),
        status="listed",
    )


def _outcome(
    ipo: Ipo,
    *,
    version: str,
    d0: float | None,
    d1: float | None,
    d3: float | None,
    evaluated_at: datetime,
    score_snapshot_id: int | None = None,
) -> OutcomeEvaluation:
    return OutcomeEvaluation(
        ipo_id=ipo.id,
        score_snapshot_id=score_snapshot_id,
        evaluation_version=version,
        d0_open_return_pct=d0,
        d1_close_return_pct=d1,
        d3_close_return_pct=d3,
        evaluated_at=evaluated_at,
    )


def test_db_history_separates_gross_distributions_from_strict_gate(db_session):
    operating = _ipo("일반기업")
    spac = _ipo("테스트스팩", ipo_type="SPAC")
    reit = _ipo("테스트리츠", ipo_type="REIT")
    sample = _ipo("ABC테크", origin="sample")
    unsafe_origin = _ipo("테스트원본", origin="test")
    future = _ipo("미래상장")
    future.listing_date = date(2026, 8, 1)
    db_session.add_all([operating, spac, reit, sample, unsafe_origin, future])
    db_session.flush()

    model_version, rules_hash = resolve_scoring_identity()
    score = ScoreSnapshot(
        ipo_id=operating.id,
        score_date=date(2026, 6, 19),
        calculated_at=datetime(2026, 6, 19, tzinfo=timezone.utc),
        total_score=70,
        confidence_score=95,
        recommendation_band="Watch",
        model_version=model_version,
        rules_hash=rules_hash,
        explanation_json={
            "gates": {"status": "pass"},
            "sources": {
                "demand": {"source": "dart", "as_of": "2026-06-18T00:00:00Z"},
                "supply": {"source": "dart", "as_of": "2026-06-18"},
            },
        },
    )
    db_session.add(score)
    db_session.flush()
    unsafe_score = ScoreSnapshot(
        ipo_id=unsafe_origin.id,
        score_date=date(2026, 6, 19),
        calculated_at=datetime(2026, 6, 19, tzinfo=timezone.utc),
        total_score=70,
        recommendation_band="Watch",
        model_version=model_version,
        rules_hash=rules_hash,
        explanation_json=score.explanation_json,
    )
    db_session.add(unsafe_score)
    db_session.flush()
    db_session.add_all(
        [
            _outcome(
                operating,
                version="old",
                d0=5,
                d1=-10,
                d3=-20,
                evaluated_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
            ),
            _outcome(
                operating,
                version="strict",
                d0=30,
                d1=20,
                d3=10,
                evaluated_at=datetime(2026, 7, 3, tzinfo=timezone.utc),
                score_snapshot_id=score.id,
            ),
            _outcome(
                operating,
                version="late-inserted-older",
                d0=-50,
                d1=-50,
                d3=-50,
                evaluated_at=datetime(2026, 7, 2, 12, tzinfo=timezone.utc),
            ),
            _outcome(
                operating,
                version="strict-future",
                d0=777,
                d1=777,
                d3=777,
                evaluated_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
                score_snapshot_id=score.id,
            ),
            _outcome(
                spac,
                version="spac",
                d0=-2,
                d1=-5,
                d3=None,
                evaluated_at=datetime(2026, 7, 4, tzinfo=timezone.utc),
            ),
            _outcome(
                reit,
                version="reit",
                d0=1,
                d1=8,
                d3=4,
                evaluated_at=datetime(2026, 7, 5, tzinfo=timezone.utc),
            ),
            _outcome(
                sample,
                version="sample",
                d0=999,
                d1=999,
                d3=999,
                evaluated_at=datetime(2026, 7, 5, tzinfo=timezone.utc),
            ),
            _outcome(
                unsafe_origin,
                version="unsafe-origin",
                d0=666,
                d1=666,
                d3=666,
                evaluated_at=datetime(2026, 7, 5, tzinfo=timezone.utc),
                score_snapshot_id=unsafe_score.id,
            ),
            _outcome(
                future,
                version="future-listing",
                d0=888,
                d1=888,
                d3=888,
                evaluated_at=datetime(2026, 7, 5, tzinfo=timezone.utc),
            ),
            _outcome(
                spac,
                version="future-evaluation",
                d0=777,
                d1=777,
                d3=777,
                evaluated_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
            ),
        ]
    )
    db_session.flush()

    report = build_db_historical_evidence_availability(
        db_session,
        as_of=datetime(2026, 7, 19, 9, tzinfo=timezone.utc),
        minimum_complete_pairs=2,
    )

    assert report.gross_outcome_count == 3
    assert report.strict_complete_pair_count == 1
    assert report.validation_status == "insufficient_sample"
    assert report.recommendation_eligible is False
    by_label = {item.label: item for item in report.distributions}
    assert by_label["D1 종가 전체"].sample_count == 3
    assert by_label["D1 종가 전체"].median_return_pct == 8
    assert by_label["D1 종가 전체"].loss_rate == pytest.approx(1 / 3)
    assert by_label["D1 종가 일반기업"].median_return_pct == 20
    assert by_label["D1 종가 SPAC"].median_return_pct == -5
    assert by_label["D1 종가 REIT"].median_return_pct == 8
    assert by_label["D3 종가 전체"].sample_count == 2

    approval_needed = build_db_historical_evidence_availability(
        db_session,
        as_of=datetime(2026, 7, 19, 9, tzinfo=timezone.utc),
        minimum_complete_pairs=1,
    )
    ready = build_db_historical_evidence_availability(
        db_session,
        as_of=datetime(2026, 7, 19, 9, tzinfo=timezone.utc),
        minimum_complete_pairs=1,
        validation_approved=True,
    )
    assert approval_needed.validation_status == "validation_required"
    assert approval_needed.recommendation_eligible is False
    assert ready.validation_status == "ready"
    assert ready.recommendation_eligible is True


def test_db_history_empty_validation_and_facts_only_rendering(db_session):
    with pytest.raises(ValueError, match="positive"):
        build_db_historical_evidence_availability(
            db_session,
            as_of=datetime(2026, 7, 19),
            minimum_complete_pairs=0,
        )

    report = build_db_historical_evidence_availability(
        db_session,
        as_of=datetime(2026, 7, 19),
    )
    assert report.gross_outcome_count == 0
    assert all(item.sample_count == 0 for item in report.distributions)
    text = "\n".join(_historical_text(report))
    html = _historical_html(report)
    assert "엄격 시점 일치 완전 표본: 0/30" in text
    assert "추천 게이트: 비활성(표본 부족)" in text
    assert "현재 종목과의 유사성은 검증되지 않았습니다" in text
    assert "D1 종가 전체: n=0 · 표본 없음" in text
    assert "과거 기준판 (설명용 사실)" in html
    assert "추천" in html
    assert _historical_text(None) == []
    assert _historical_html(None) == ""


def test_history_render_helpers_cover_distribution_and_gate_labels():
    distribution = HistoricalReturnDistribution(
        label="D1",
        sample_count=2,
        median_return_pct=5,
        q1_return_pct=-5,
        q3_return_pct=15,
        min_return_pct=-10,
        max_return_pct=20,
        loss_rate=0.5,
    )
    assert (
        _history_distribution_text(distribution)
        == "D1: n=2 · 중앙값 +5.00% · IQR -5.00%~+15.00% · 범위 "
        "-10.00%~+20.00% · 손실 빈도 50.0%"
    )
    base = HistoricalEvidenceAvailability(
        as_of=datetime(2026, 7, 19, tzinfo=timezone.utc),
        gross_outcome_count=2,
        distributions=(distribution,),
        strict_complete_pair_count=30,
        minimum_complete_pairs=30,
        recommendation_eligible=False,
        validation_status="validation_required",
    )
    assert _history_gate_label(base) == "비활성(별도 검증 승인 필요)"
    assert (
        _history_gate_label(
            HistoricalEvidenceAvailability(
                **{**base.__dict__, "recommendation_eligible": True, "validation_status": "ready"}
            )
        )
        == "통과(이 facts-only 메일은 추천을 생성하지 않음)"
    )
    assert (
        _history_gate_label(
            HistoricalEvidenceAvailability(**{**base.__dict__, "validation_status": "unknown"})
        )
        == "비활성(검증 상태 확인 필요)"
    )


def test_return_distribution_rejects_non_finite_values_without_hiding_valid_rows():
    ipo = _ipo("단위테스트")
    finite = OutcomeEvaluation(d1_close_return_pct=10)
    non_finite = OutcomeEvaluation(d1_close_return_pct=float("nan"))
    report = _return_distribution(
        "D1",
        ((finite, ipo), (non_finite, ipo)),
        "d1_close_return_pct",
    )
    assert report.sample_count == 1
    assert report.median_return_pct == 10
    assert report.loss_rate == 0
