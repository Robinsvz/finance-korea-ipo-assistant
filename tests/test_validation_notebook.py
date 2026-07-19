from datetime import datetime, timezone

from sqlalchemy import text

from app.models import Ipo, OutcomeEvaluation
from scripts.build_validation_notebook import SEGMENT_OUTCOME_SQL


def test_segment_outcome_sql_uses_only_latest_complete_evaluation_per_ipo(db_session):
    ipo = Ipo(
        identity_key="kind:notebook-canonical",
        corp_name="노트북검증기업",
        normalized_name="노트북검증기업",
        status="listed",
    )
    sample = Ipo(
        identity_key="sample:notebook-canonical",
        corp_name="노트북샘플",
        normalized_name="노트북샘플",
        status="listed",
        is_sample=True,
        data_origin="synthetic",
    )
    db_session.add_all([ipo, sample])
    db_session.flush()
    db_session.add_all(
        [
            OutcomeEvaluation(
                ipo_id=ipo.id,
                evaluation_version="older-complete",
                evaluated_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
                d0_open_return_pct=100,
                d1_close_return_pct=100,
            ),
            OutcomeEvaluation(
                ipo_id=ipo.id,
                evaluation_version="newer-complete",
                evaluated_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
                d0_open_return_pct=20,
                d1_close_return_pct=-10,
            ),
            OutcomeEvaluation(
                ipo_id=sample.id,
                evaluation_version="sample-complete",
                evaluated_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
                d0_open_return_pct=300,
                d1_close_return_pct=300,
            ),
        ]
    )
    db_session.flush()

    rows = db_session.execute(text(SEGMENT_OUTCOME_SQL)).mappings().all()

    assert [dict(row) for row in rows] == [
        {
            "segment": "Operating company",
            "n": 1,
            "unique_ipo_n": 1,
            "avg_d0_open_return_pct": 20.0,
            "avg_d1_close_return_pct": -10.0,
            "d1_positive_pct": 0.0,
        }
    ]
