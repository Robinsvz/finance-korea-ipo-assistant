from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Sequence

from app.services.evidence import EvidenceObservation


@dataclass(frozen=True)
class DataQualityFinding:
    code: str
    severity: str
    field_name: Optional[str]
    detail: str


@dataclass(frozen=True)
class DataQualityReport:
    ready: bool
    finding_count: int
    error_count: int
    warning_count: int
    findings: tuple[DataQualityFinding, ...]


def assess_evidence_quality(
    observations: Iterable[EvidenceObservation],
    *,
    required_fields: Sequence[str] = (),
    cutoff: Optional[datetime] = None,
    max_age: Optional[timedelta] = None,
) -> DataQualityReport:
    """Return compact, deterministic diagnostics for an evidence bundle."""
    rows = tuple(observations)
    findings: list[DataQualityFinding] = []
    present_fields = {row.field_name for row in rows if row.value is not None}
    for field_name in sorted(set(required_fields) - present_fields):
        findings.append(
            DataQualityFinding(
                "required_field_missing", "error", field_name, "required value absent"
            )
        )

    seen: Counter[tuple[str, str, Optional[datetime]]] = Counter()
    values_by_revision: defaultdict[tuple[str, Optional[datetime]], set[str]] = defaultdict(set)
    for row in rows:
        if row.is_sample or _sample_origin(row.data_origin):
            findings.append(
                DataQualityFinding("sample_or_synthetic", "error", row.field_name, row.data_origin)
            )
        if not row.source or not row.source_document_id:
            findings.append(
                DataQualityFinding(
                    "source_missing", "error", row.field_name, "source document unavailable"
                )
            )
        if row.published_at is None or row.observed_at is None:
            findings.append(
                DataQualityFinding(
                    "timestamp_missing", "error", row.field_name, "published/observed time absent"
                )
            )
        if cutoff is not None:
            normalized_cutoff = _as_utc(cutoff)
            if row.published_at is not None and _as_utc(row.published_at) > normalized_cutoff:
                findings.append(
                    DataQualityFinding(
                        "future_published", "error", row.field_name, "published after cutoff"
                    )
                )
            if row.observed_at is not None and _as_utc(row.observed_at) > normalized_cutoff:
                findings.append(
                    DataQualityFinding(
                        "future_observed", "error", row.field_name, "observed after cutoff"
                    )
                )
            if (
                max_age is not None
                and row.observed_at is not None
                and normalized_cutoff - _as_utc(row.observed_at) > max_age
            ):
                findings.append(
                    DataQualityFinding("stale", "warning", row.field_name, "observation too old")
                )
        if row.quality_state in {"conflict", "missing", "legacy_unverified", "not_published"}:
            findings.append(
                DataQualityFinding(
                    "unsafe_quality_state", "error", row.field_name, row.quality_state
                )
            )
        elif row.quality_state in {"estimated", "stale"}:
            findings.append(
                DataQualityFinding(
                    "non_reported_quality", "warning", row.field_name, row.quality_state
                )
            )
        key = (row.field_name, repr(row.value), row.published_at)
        seen[key] += 1
        values_by_revision[(row.field_name, row.published_at)].add(repr(row.value))

    for (field_name, _value, published_at), count in sorted(
        seen.items(), key=lambda item: repr(item[0])
    ):
        if count > 1:
            findings.append(
                DataQualityFinding(
                    "duplicate_observation",
                    "warning",
                    field_name,
                    f"{count} identical observations at {published_at}",
                )
            )
    for (field_name, published_at), values in sorted(
        values_by_revision.items(), key=lambda item: repr(item[0])
    ):
        if len(values) > 1:
            findings.append(
                DataQualityFinding(
                    "conflicting_values",
                    "error",
                    field_name,
                    f"{len(values)} values at {published_at}",
                )
            )

    findings.sort(key=lambda item: (item.severity != "error", item.code, item.field_name or ""))
    errors = sum(item.severity == "error" for item in findings)
    warnings = len(findings) - errors
    return DataQualityReport(
        ready=errors == 0,
        finding_count=len(findings),
        error_count=errors,
        warning_count=warnings,
        findings=tuple(findings),
    )


def _sample_origin(value: str) -> bool:
    return str(value or "").strip().lower() in {
        "sample",
        "synthetic",
        "test",
        "dummy",
        "demo",
    }


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
