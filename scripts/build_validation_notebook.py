from __future__ import annotations

from pathlib import Path

import nbformat
from nbclient import NotebookClient

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "analysis" / "ipo_validation.ipynb"
SEGMENT_OUTCOME_SQL = """WITH ranked_complete AS (
  SELECT
    i.id AS ipo_id,
    CASE WHEN instr(i.corp_name, '스팩') > 0
         THEN 'SPAC' ELSE 'Operating company' END AS segment,
    o.d0_open_return_pct,
    o.d1_close_return_pct,
    ROW_NUMBER() OVER (
      PARTITION BY i.id
      ORDER BY o.evaluated_at DESC, o.id DESC
    ) AS outcome_rank
  FROM outcome_evaluations o
  JOIN ipos i ON i.id = o.ipo_id
  WHERE i.is_sample = 0
    AND o.d0_open_return_pct IS NOT NULL
    AND o.d1_close_return_pct IS NOT NULL
)
SELECT
  segment,
  COUNT(*) AS n,
  COUNT(DISTINCT ipo_id) AS unique_ipo_n,
  ROUND(AVG(d0_open_return_pct), 2) AS avg_d0_open_return_pct,
  ROUND(AVG(d1_close_return_pct), 2) AS avg_d1_close_return_pct,
  ROUND(100.0 * SUM(d1_close_return_pct > 0) / COUNT(*), 1) AS d1_positive_pct
FROM ranked_complete
WHERE outcome_rank = 1
GROUP BY segment
ORDER BY segment"""


def main() -> None:
    notebook = nbformat.v4.new_notebook(
        metadata={
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python", "version": "3.9"},
        }
    )
    notebook.cells = [
        nbformat.v4.new_markdown_cell(
            """# Korea IPO historical validation audit

This notebook is the reproducible companion to `MASTER_PLAN.md`. It reads the local
SQLite database without changing it, excludes explicitly marked samples, separates
SPACs from operating companies, and treats D0/D1 returns as gross market outcomes.
It does **not** claim that `rules-v3-simple` is effective until point-in-time feature
snapshots and at least 30 complete score/outcome pairs exist."""
        ),
        nbformat.v4.new_code_cell(
            """from pathlib import Path
import sys

import pandas as pd
from sqlalchemy import text

PROJECT_ROOT = Path.cwd()
if not (PROJECT_ROOT / "app").exists():
    PROJECT_ROOT = PROJECT_ROOT.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.db import SessionLocal
from app.services.scoring_v3 import resolve_scoring_identity
from app.services.validation import assess_data_readiness, build_validation_dataset

session = SessionLocal()
model_version, rules_hash = resolve_scoring_identity()
readiness = assess_data_readiness(
    session,
    model_version=model_version,
    rules_hash=rules_hash,
    minimum_samples=30,
)
readiness.to_dict()"""
        ),
        nbformat.v4.new_markdown_cell("## Data pipeline readiness"),
        nbformat.v4.new_code_cell(
            """coverage = pd.DataFrame(
    [
        ("Listed by as-of date", readiness.listed_count),
        ("Ticker resolved", readiness.ticker_count),
        ("D0/D1 price", readiness.listing_price_count),
        ("Gross outcome", readiness.outcome_count),
        ("Historical demand feature", readiness.demand_count),
        ("Historical supply feature", readiness.structure_count),
        ("Eligible v3 score/outcome pair", readiness.complete_pair_count),
    ],
    columns=["stage", "count"],
)
coverage"""
        ),
        nbformat.v4.new_markdown_cell("## Observed gross D0/D1 outcomes"),
        nbformat.v4.new_code_cell(
            f"""segment_sql = text({SEGMENT_OUTCOME_SQL!r})
segment_outcomes = pd.read_sql_query(segment_sql, session.connection())
assert (segment_outcomes["n"] == segment_outcomes["unique_ipo_n"]).all()
segment_outcomes = segment_outcomes.drop(columns=["unique_ipo_n"])
segment_outcomes"""
        ),
        nbformat.v4.new_markdown_cell(
            """The table above is a small, date-concentrated descriptive baseline. It is not a
causal estimate and is not evidence that any score pillar predicts returns. D0-open
and D1-close are separate exit policies; fees, allocation probability, and cash
opportunity cost are excluded."""
        ),
        nbformat.v4.new_markdown_cell("## Point-in-time v3 validation dataset"),
        nbformat.v4.new_code_cell(
            """validation_rows = build_validation_dataset(
    session,
    model_version=model_version,
    rules_hash=rules_hash,
)
validation = pd.DataFrame([row.to_dict() for row in validation_rows])
print(f"eligible rows: {len(validation)}")
validation.head()"""
        ),
        nbformat.v4.new_markdown_cell("## Integrity assertions"),
        nbformat.v4.new_code_cell(
            """assert readiness.ipo_count == readiness.real_ipo_count + readiness.sample_ipo_count
assert readiness.ticker_count <= readiness.listed_count
assert readiness.listing_price_count <= readiness.listed_count
assert readiness.outcome_count <= readiness.listing_price_count
assert readiness.complete_pair_count <= readiness.eligible_score_count
assert readiness.complete_pair_count <= len(validation_rows)
assert readiness.selected_rules_hash == rules_hash
assert readiness.eligible_rules_hash_count <= 1
assert len(validation_rows) == readiness.complete_pair_count
assert readiness.validation_ready == (not readiness.blockers)

session.close()
print("All point-in-time readiness assertions passed.")"""
        ),
    ]
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    client = NotebookClient(
        notebook,
        timeout=120,
        kernel_name="python3",
        resources={"metadata": {"path": str(ROOT)}},
    )
    client.execute()
    nbformat.validate(notebook)
    nbformat.write(notebook, OUTPUT)
    print(f"Wrote executed notebook: {OUTPUT}")


if __name__ == "__main__":
    main()
