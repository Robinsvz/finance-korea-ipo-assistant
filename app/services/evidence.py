from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Mapping, Optional, Union

from app.collectors.dart import (
    DartDocumentFamily,
    DartDocumentParseResult,
    RawEvidence,
)


class DecisionStage(str, Enum):
    PRE_OPEN = "PRE_OPEN"
    IN_SUBSCRIPTION = "IN_SUBSCRIPTION"
    POST_CLOSE = "POST_CLOSE"


class KnowledgePhase(str, Enum):
    PRE_SUBSCRIPTION = "pre_subscription"
    DURING_SUBSCRIPTION = "during_subscription"
    POST_SUBSCRIPTION = "post_subscription"
    POST_LISTING = "post_listing"


@dataclass(frozen=True)
class EvidenceObservation:
    """Pure persistence payload for one point-in-time IPO fact."""

    field_name: str
    value: Any
    unit: str
    quality_state: str
    knowledge_phase: KnowledgePhase
    source: str
    source_document_id: Optional[str]
    published_at: Optional[datetime]
    observed_at: Optional[datetime]
    parser_name: str
    parser_version: str
    excerpt: str
    confidence: float
    canonical_evidence_category: str
    canonical_ipo_id: Optional[str] = None
    data_origin: str = "official"
    is_sample: bool = False

    def as_persistence_values(self) -> dict[str, Any]:
        return {
            "field_name": self.field_name,
            "value": self.value,
            "unit": self.unit,
            "quality_state": self.quality_state,
            "knowledge_phase": self.knowledge_phase.value,
            "published_at": self.published_at,
            "observed_at": self.observed_at,
            "parser_name": self.parser_name,
            "parser_version": self.parser_version,
        }


@dataclass(frozen=True)
class EvidenceGuardResult:
    allowed: bool
    reason: Optional[str] = None


StageKey = Union[DecisionStage, str]


def build_evidence_observations(
    parse_result: DartDocumentParseResult,
    stage_cutoffs: Optional[Mapping[StageKey, datetime]] = None,
    *,
    observed_at: Optional[datetime] = None,
    canonical_ipo_id: Optional[str] = None,
    data_origin: str = "official",
) -> tuple[EvidenceObservation, ...]:
    """Map parser evidence to append-only observations without database side effects.

    When stage cutoffs are supplied, claims that were not yet knowable at the
    relevant stage are omitted. This keeps the same function safe for live email
    rendering and point-in-time historical reconstruction.
    """
    effective_observed_at = observed_at or parse_result.published_at
    is_sample = _is_sample_origin(data_origin)
    observations: list[EvidenceObservation] = []
    for raw in parse_result.raw_evidence:
        for field_name, value, unit in _expanded_fields(raw):
            phase = _phase_for_field(parse_result.document_family, field_name)
            observation = EvidenceObservation(
                canonical_ipo_id=canonical_ipo_id,
                field_name=field_name,
                value=value,
                unit=unit,
                quality_state=_quality_state(raw.canonical_evidence_category),
                knowledge_phase=phase,
                source="opendart",
                source_document_id=(f"opendart:{raw.receipt_no}" if raw.receipt_no else None),
                published_at=raw.published_at or parse_result.published_at,
                observed_at=effective_observed_at,
                parser_name=raw.parser_name,
                parser_version=raw.parser_version,
                excerpt=raw.excerpt,
                confidence=raw.confidence,
                canonical_evidence_category=raw.canonical_evidence_category,
                data_origin=data_origin,
                is_sample=is_sample,
            )
            cutoff = _cutoff_for_phase(stage_cutoffs, phase)
            if stage_cutoffs and cutoff is None:
                continue
            if cutoff is not None:
                stage = _stage_for_phase(phase)
                if not guard_observation(observation, stage, cutoff).allowed:
                    continue
            observations.append(observation)
    return tuple(observations)


def guard_observation(
    observation: EvidenceObservation,
    stage: Union[DecisionStage, str],
    cutoff: datetime,
) -> EvidenceGuardResult:
    """Explain whether an observation was knowable and usable at a decision stage."""
    normalized_stage = _coerce_stage(stage)
    if observation.is_sample or _is_sample_origin(observation.data_origin):
        return EvidenceGuardResult(False, "sample_or_synthetic")
    if observation.value is None:
        return EvidenceGuardResult(False, "missing_value")
    if observation.quality_state.lower() in {
        "missing",
        "conflict",
        "stale",
        "legacy_unverified",
        "not_published",
    }:
        return EvidenceGuardResult(False, "unsafe_quality_state")
    if observation.published_at is None:
        return EvidenceGuardResult(False, "missing_published_at")
    if observation.observed_at is None:
        return EvidenceGuardResult(False, "missing_observed_at")
    normalized_cutoff = _as_utc(cutoff)
    if _as_utc(observation.published_at) > normalized_cutoff:
        return EvidenceGuardResult(False, "published_after_cutoff")
    if _as_utc(observation.observed_at) > normalized_cutoff:
        return EvidenceGuardResult(False, "observed_after_cutoff")
    required_phase = _minimum_phase_for_field(observation.field_name)
    effective_phase = max(
        observation.knowledge_phase,
        required_phase,
        key=_phase_rank,
    )
    if _phase_rank(effective_phase) > _stage_rank(normalized_stage):
        return EvidenceGuardResult(False, "future_knowledge_phase")
    return EvidenceGuardResult(True)


def filter_evidence_for_stage(
    observations: Iterable[EvidenceObservation],
    stage: Union[DecisionStage, str],
    cutoff: datetime,
) -> tuple[EvidenceObservation, ...]:
    return tuple(
        observation
        for observation in observations
        if guard_observation(observation, stage, cutoff).allowed
    )


def with_observed_at(
    observations: Iterable[EvidenceObservation], observed_at: datetime
) -> tuple[EvidenceObservation, ...]:
    """Set a collector-owned observation time without mutating pure parser output."""
    return tuple(replace(item, observed_at=observed_at) for item in observations)


def _expanded_fields(raw: RawEvidence) -> tuple[tuple[str, Any, str], ...]:
    prefix = "retail_brokers."
    if not raw.field_name.startswith(prefix) or not isinstance(raw.value, dict):
        return ((raw.field_name, raw.value, raw.unit),)
    broker = str(raw.value.get("broker_name") or raw.field_name[len(prefix) :]).strip()
    if not broker:
        return ()
    mappings = (
        ("competition_ratio", "actual_proportional_competition", "ratio_to_one"),
        ("applicants_count", "actual_applicants_count", "applicants"),
        ("subscribed_shares", "subscribed_shares", "shares"),
        ("allocated_shares", "allocated_shares", "shares"),
    )
    return tuple(
        (f"broker.{broker}.{target}", raw.value[source], unit)
        for source, target, unit in mappings
        if raw.value.get(source) is not None
    )


def _phase_for_field(
    family: DartDocumentFamily,
    field_name: str,
) -> KnowledgePhase:
    if family is DartDocumentFamily.ISSUANCE_RESULT:
        return KnowledgePhase.POST_SUBSCRIPTION
    return _minimum_phase_for_field(field_name)


def _minimum_phase_for_field(field_name: str) -> KnowledgePhase:
    normalized = field_name.lower()
    if normalized.startswith(("listing_", "actual_listing", "outcome_", "d0_", "d1_")):
        return KnowledgePhase.POST_LISTING
    if normalized.startswith(("retail_", "broker.", "underwriter.")):
        return KnowledgePhase.POST_SUBSCRIPTION
    if normalized.startswith(("live_", "realtime_", "subscription_live_")):
        return KnowledgePhase.DURING_SUBSCRIPTION
    return KnowledgePhase.PRE_SUBSCRIPTION


def _quality_state(category: str) -> str:
    normalized = category.strip().lower()
    if normalized in {"reported_fact", "derived_reported_fact"}:
        return "reported"
    if normalized in {"missing", "conflict", "stale", "legacy_unverified", "not_published"}:
        return normalized
    return "estimated"


def _cutoff_for_phase(
    stage_cutoffs: Optional[Mapping[StageKey, datetime]],
    phase: KnowledgePhase,
) -> Optional[datetime]:
    if not stage_cutoffs:
        return None
    normalized = {_coerce_stage(key): value for key, value in stage_cutoffs.items()}
    preferred = _stage_for_phase(phase)
    if preferred in normalized:
        return normalized[preferred]
    eligible = [
        (stage, cutoff)
        for stage, cutoff in normalized.items()
        if _stage_rank(stage) >= _phase_rank(phase)
    ]
    return min(eligible, key=lambda item: _stage_rank(item[0]))[1] if eligible else None


def _stage_for_phase(phase: KnowledgePhase) -> DecisionStage:
    if phase is KnowledgePhase.PRE_SUBSCRIPTION:
        return DecisionStage.PRE_OPEN
    if phase is KnowledgePhase.DURING_SUBSCRIPTION:
        return DecisionStage.IN_SUBSCRIPTION
    return DecisionStage.POST_CLOSE


def _stage_rank(stage: DecisionStage) -> int:
    return {
        DecisionStage.PRE_OPEN: 0,
        DecisionStage.IN_SUBSCRIPTION: 1,
        DecisionStage.POST_CLOSE: 3,
    }[stage]


def _phase_rank(phase: KnowledgePhase) -> int:
    return {
        KnowledgePhase.PRE_SUBSCRIPTION: 0,
        KnowledgePhase.DURING_SUBSCRIPTION: 1,
        KnowledgePhase.POST_SUBSCRIPTION: 2,
        KnowledgePhase.POST_LISTING: 3,
    }[phase]


def _coerce_stage(value: StageKey) -> DecisionStage:
    if isinstance(value, DecisionStage):
        return value
    normalized = str(value).strip().upper()
    try:
        return DecisionStage(normalized)
    except ValueError as exc:
        raise ValueError(f"unknown decision stage: {value}") from exc


def _is_sample_origin(value: str) -> bool:
    normalized = str(value or "").strip().lower()
    return normalized in {"sample", "synthetic", "test", "dummy", "demo"}


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
