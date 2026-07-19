# ruff: noqa: E501
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta, timezone
from html import escape
from typing import Callable, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    DemandForecast,
    Ipo,
    IpoFactObservation,
    IpoStructureMetric,
    ListingPrice,
    OutcomeEvaluation,
    SourceDocument,
    UnderwriterAllocation,
)
from app.services.email import EmailContent
from app.services.historical_evidence import (
    HistoricalEvidenceAvailability,
    HistoricalReturnDistribution,
    build_db_historical_evidence_availability,
)

KST = ZoneInfo("Asia/Seoul")
DEFAULT_MAX_HTML_BYTES = 90_000
UNSAFE_ORIGINS = frozenset({"sample", "synthetic", "test"})
UNSAFE_SOURCE_MARKERS = frozenset({"sample", "synthetic", "fixture", "test"})
STAGE_LABELS = {
    "PRE_OPEN": "청약 전",
    "IN_SUBSCRIPTION": "청약 진행 중",
    "POST_CLOSE": "청약 종료 후",
}
EVENT_BADGE_LABELS = {
    "demand_forecast": "수요예측",
    "subscription": "청약",
    "refund": "환불",
    "payment": "납입",
    "actual_listing": "실제 상장",
    "planned_listing": "예정 상장",
    "filing_update": "공시 변경",
}
STATE_LABELS = {
    "verified": "확인됨",
    "cross_checked": "교차확인",
    "reported": "공시값",
    "legacy_unverified": "미검증 기존값",
    "post_event": "사후 관측",
    "estimated": "추정 — 실제값 아님",
    "conflict": "출처 충돌 — 검토 필요",
    "stale": "오래된 값",
    "not_published": "공시 전",
    "missing": "미수집",
    "waiting": "대기",
    "not_applicable": "해당 없음",
}
DISCLAIMER = (
    "확인된 사실과 결측 상태를 전달하는 자료이며 투자 추천이나 수익 보장이 아닙니다. "
    "출처·기준시각을 확인한 뒤 최종 판단과 청약은 사용자가 직접 수행해야 합니다."
)
HISTORICAL_LIMITATION = (
    "상장 결과의 시장 기초분포(gross)이며 개인 배정·청약 수수료·증거금 기회비용을 "
    "반영하지 않았습니다. 현재 종목과의 유사성은 검증되지 않았습니다."
)


@dataclass(frozen=True)
class EvidenceFact:
    label: str
    value: str
    state: str
    source: Optional[str] = None
    as_of: Optional[str] = None
    source_ref: Optional[str] = None
    available: bool = True
    raw_value: object = None


@dataclass(frozen=True)
class InstitutionalPriceDistributionBand:
    label: str
    participant_ratio_pct: Optional[float]
    participant_ratio_basis: str
    reported_participant_ratio_pct: Optional[float]
    participants_count: Optional[int]
    requested_shares: Optional[int]
    requested_shares_ratio_pct: Optional[float]
    requested_shares_ratio_basis: str
    reported_requested_shares_ratio_pct: Optional[float]
    price_krw: Optional[int]
    source: Optional[str]
    as_of: Optional[str]
    source_ref: Optional[str]


@dataclass(frozen=True)
class UnderwriterBrief:
    broker_name: str
    role: EvidenceFact
    public_offer_shares: EvidenceFact
    retail_allocated_shares: EvidenceFact
    actual_competition: EvidenceFact
    actual_applicants: EvidenceFact
    subscription_fee: EvidenceFact
    account_open_deadline: EvidenceFact
    source: str
    as_of: str


@dataclass(frozen=True)
class MonthlyIpoBrief:
    ipo_id: Optional[int]
    corp_name: str
    stage: str
    stage_as_of: str
    status: str
    event_badges: tuple[str, ...]
    reconciliation_state: str
    identity: tuple[EvidenceFact, ...]
    schedule: tuple[EvidenceFact, ...]
    offer: tuple[EvidenceFact, ...]
    demand: tuple[EvidenceFact, ...]
    institutional_price_distribution: tuple[InstitutionalPriceDistributionBand, ...]
    underwriters: tuple[UnderwriterBrief, ...]
    retail: tuple[EvidenceFact, ...]
    structure: tuple[EvidenceFact, ...]
    financial: tuple[EvidenceFact, ...]
    valuation: tuple[EvidenceFact, ...]
    outcomes: tuple[EvidenceFact, ...]
    missing_labels: tuple[str, ...]
    completeness_complete: int
    completeness_total: int
    quality_label: str


@dataclass(frozen=True)
class MonthlyEmailReadModel:
    month_start: date
    month_end: date
    generated_at: datetime
    core: tuple[MonthlyIpoBrief, ...]
    filing_appendix: tuple[MonthlyIpoBrief, ...]
    reconciliation_state: str
    universe_hash: str
    evidence_hash: str
    historical_evidence: Optional[HistoricalEvidenceAvailability] = None


@dataclass(frozen=True)
class MonthlyEmailBundle:
    parts: tuple[EmailContent, ...]
    read_model: MonthlyEmailReadModel

    @property
    def universe_hash(self) -> str:
        return self.read_model.universe_hash

    @property
    def evidence_hash(self) -> str:
        return self.read_model.evidence_hash


@dataclass(frozen=True)
class _UniverseItem:
    ipo: Ipo
    event_badges: tuple[str, ...]
    reconciliation_state: str


@dataclass(frozen=True)
class _PartItem:
    brief: MonthlyIpoBrief
    appendix: bool


@dataclass(frozen=True)
class _SingleUniverse:
    core: tuple[object, ...]
    filing_appendix: tuple[object, ...]
    reconciliation_state: str


MonthlyFactAdapter = Callable[
    [Session, tuple[Ipo, ...], datetime],
    Mapping[int, Mapping[str, EvidenceFact]],
]


def build_monthly_ipo_email(
    session: Session,
    universe_or_ipos: object,
    *,
    month_start: date,
    month_end: date,
    generated_at: datetime,
    title: Optional[str] = None,
    max_html_bytes: int = DEFAULT_MAX_HTML_BYTES,
    fact_adapter: Optional[MonthlyFactAdapter] = None,
) -> MonthlyEmailBundle:
    """Build a complete monthly facts email, split only at IPO boundaries.

    ``universe_or_ipos`` accepts the monthly-universe result contract
    (``core``/``filing_appendix`` entries) or a plain iterable of ``Ipo`` rows.
    The latter keeps preview and migration callers backward-compatible.
    """

    read_model = build_monthly_email_read_model(
        session,
        universe_or_ipos,
        month_start=month_start,
        month_end=month_end,
        generated_at=generated_at,
        fact_adapter=fact_adapter,
    )
    parts = render_monthly_ipo_email(
        read_model,
        title=title,
        max_html_bytes=max_html_bytes,
    )
    return MonthlyEmailBundle(parts=parts, read_model=read_model)


def build_single_ipo_email(
    session: Session,
    ipo_or_entry: object,
    *,
    month_start: date,
    month_end: date,
    generated_at: datetime,
    title: Optional[str] = None,
    max_html_bytes: int = DEFAULT_MAX_HTML_BYTES,
    fact_adapter: Optional[MonthlyFactAdapter] = None,
) -> MonthlyEmailBundle:
    """Build one independently sendable IPO report.

    The caller is expected to select the D-1 subscription target. This builder
    deliberately accepts exactly one operational IPO so a scheduler cannot
    accidentally combine two same-day subscription notices into one email.
    """

    universe = _SingleUniverse(
        core=(ipo_or_entry,),
        filing_appendix=(),
        reconciliation_state=str(getattr(ipo_or_entry, "reconciliation_state", "미검증")),
    )
    read_model = build_monthly_email_read_model(
        session,
        universe,
        month_start=month_start,
        month_end=month_end,
        generated_at=generated_at,
        fact_adapter=fact_adapter,
    )
    if len(read_model.core) != 1 or read_model.filing_appendix:
        raise ValueError("single IPO email requires exactly one operational core IPO")
    content = render_single_ipo_email(
        read_model,
        title=title,
        max_html_bytes=max_html_bytes,
    )
    return MonthlyEmailBundle(parts=(content,), read_model=read_model)


def build_monthly_email_read_model(
    session: Session,
    universe_or_ipos: object,
    *,
    month_start: date,
    month_end: date,
    generated_at: datetime,
    fact_adapter: Optional[MonthlyFactAdapter] = None,
) -> MonthlyEmailReadModel:
    if month_start > month_end:
        raise ValueError("month_start must be on or before month_end")

    core_items, appendix_items, reconciliation_state = _coerce_universe(
        session,
        universe_or_ipos,
        month_start,
        month_end,
    )
    operational_core = _deduplicate_operational(core_items)
    core_keys = {_ipo_key(item.ipo) for item in operational_core}
    operational_appendix = tuple(
        item
        for item in _deduplicate_operational(appendix_items)
        if _ipo_key(item.ipo) not in core_keys
    )
    all_items = (*operational_core, *operational_appendix)
    related = _load_related(session, tuple(item.ipo for item in all_items))
    localized = _localized(generated_at)
    adapter = fact_adapter or load_official_fact_overrides
    fact_overrides = adapter(session, tuple(item.ipo for item in all_items), localized)
    historical_evidence = build_db_historical_evidence_availability(
        session,
        as_of=localized,
        validation_approved=bool(get_settings().historical_evidence_enabled),
    )
    core = tuple(
        _build_brief(item, localized, related, fact_overrides.get(item.ipo.id or -1, {}))
        for item in sorted(operational_core, key=_universe_sort_key)
    )
    filing_appendix = tuple(
        _build_brief(item, localized, related, fact_overrides.get(item.ipo.id or -1, {}))
        for item in sorted(operational_appendix, key=_universe_sort_key)
    )
    universe_payload = [
        {
            "ipo_id": brief.ipo_id,
            "corp_name": brief.corp_name,
            "badges": brief.event_badges,
            "appendix": appendix,
        }
        for appendix, rows in ((False, core), (True, filing_appendix))
        for brief in rows
    ]
    universe_hash = _stable_hash(universe_payload)
    evidence_hash = _stable_hash(
        {
            "month_start": month_start,
            "month_end": month_end,
            "core": tuple(_brief_evidence_payload(brief) for brief in core),
            "filing_appendix": tuple(_brief_evidence_payload(brief) for brief in filing_appendix),
            "reconciliation_state": reconciliation_state,
            "historical_evidence": _historical_evidence_payload(historical_evidence),
        }
    )
    return MonthlyEmailReadModel(
        month_start=month_start,
        month_end=month_end,
        generated_at=localized,
        core=core,
        filing_appendix=filing_appendix,
        reconciliation_state=reconciliation_state,
        universe_hash=universe_hash,
        evidence_hash=evidence_hash,
        historical_evidence=historical_evidence,
    )


def render_monthly_ipo_email(
    read_model: MonthlyEmailReadModel,
    *,
    title: Optional[str] = None,
    max_html_bytes: int = DEFAULT_MAX_HTML_BYTES,
) -> tuple[EmailContent, ...]:
    if max_html_bytes <= 0:
        raise ValueError("max_html_bytes must be positive")
    resolved_title = title or (
        f"[Korea IPO] {read_model.month_start.year}년 {read_model.month_start.month}월"
    )
    items = tuple(
        [
            *(_PartItem(brief, False) for brief in read_model.core),
            *(_PartItem(brief, True) for brief in read_model.filing_appendix),
        ]
    )
    groups = _partition_items(read_model, items, resolved_title, max_html_bytes)
    total_parts = len(groups)
    contents = tuple(
        _render_part(
            read_model,
            group,
            resolved_title,
            part_number=index,
            total_parts=total_parts,
        )
        for index, group in enumerate(groups, 1)
    )
    if any(len(content.html.encode("utf-8")) > max_html_bytes for content in contents):
        raise ValueError("rendered monthly IPO email exceeds max_html_bytes")
    return contents


def render_single_ipo_email(
    read_model: MonthlyEmailReadModel,
    *,
    title: Optional[str] = None,
    max_html_bytes: int = DEFAULT_MAX_HTML_BYTES,
) -> EmailContent:
    """Render an independently sendable report from a one-IPO read model."""

    if max_html_bytes <= 0:
        raise ValueError("max_html_bytes must be positive")
    if len(read_model.core) != 1 or read_model.filing_appendix:
        raise ValueError("single IPO renderer requires exactly one core IPO")
    brief = read_model.core[0]
    resolved_title = title or (
        f"[Korea IPO] 청약 D-1 · {brief.corp_name} · {_subscription_start_subject(brief)} 청약 시작"
    )
    content = _render_part(
        read_model,
        (_PartItem(brief, False),),
        resolved_title,
        part_number=1,
        total_parts=1,
        single_report=True,
    )
    if len(content.html.encode("utf-8")) > max_html_bytes:
        raise ValueError(f"single IPO detail exceeds max_html_bytes: {brief.corp_name}")
    return content


def _coerce_universe(
    session: Session,
    value: object,
    month_start: date,
    month_end: date,
) -> tuple[tuple[_UniverseItem, ...], tuple[_UniverseItem, ...], str]:
    if hasattr(value, "core"):
        core_source = value.core  # type: ignore[attr-defined]
        appendix_source = getattr(value, "filing_update_appendix", None)
        if appendix_source is None:
            appendix_source = getattr(value, "filing_appendix", ())
        explicit_reconciliation = getattr(value, "reconciliation_state", None)
    else:
        core_source = value
        appendix_source = ()
        explicit_reconciliation = "미검증"
    core = tuple(
        item
        for entry in _as_iterable(core_source)
        if (item := _coerce_entry(session, entry, month_start, month_end, False)) is not None
    )
    appendix = tuple(
        item
        for entry in _as_iterable(appendix_source)
        if (item := _coerce_entry(session, entry, month_start, month_end, True)) is not None
    )
    reconciliation_state = (
        str(explicit_reconciliation)
        if explicit_reconciliation is not None
        else _aggregate_reconciliation(core)
    )
    return core, appendix, reconciliation_state


def _aggregate_reconciliation(items: tuple[_UniverseItem, ...]) -> str:
    states = tuple(dict.fromkeys(item.reconciliation_state for item in items))
    if not states:
        return "미검증"
    return states[0] if len(states) == 1 else "혼합(" + ", ".join(states) + ")"


def _as_iterable(value: object) -> Iterable[object]:
    if value is None:
        return ()
    try:
        return iter(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise TypeError("universe_or_ipos must be iterable or expose core") from exc


def _coerce_entry(
    session: Session,
    entry: object,
    month_start: date,
    month_end: date,
    appendix: bool,
) -> Optional[_UniverseItem]:
    ipo = getattr(entry, "ipo", entry)
    if not isinstance(ipo, Ipo):
        ipo_id = getattr(entry, "ipo_id", None)
        ipo = session.get(Ipo, ipo_id) if ipo_id is not None else None
    if ipo is None:
        return None
    explicit_badges = getattr(entry, "event_badges", ())
    badges = tuple(str(badge) for badge in explicit_badges if str(badge).strip())
    if not badges:
        badges = ("공시변경",) if appendix else _event_badges(ipo, month_start, month_end)
    state = str(getattr(entry, "reconciliation_state", "미검증"))
    return _UniverseItem(ipo=ipo, event_badges=badges, reconciliation_state=state)


def _deduplicate_operational(items: Iterable[_UniverseItem]) -> tuple[_UniverseItem, ...]:
    unique: dict[object, _UniverseItem] = {}
    for item in items:
        if _is_operational_ipo(item.ipo):
            unique.setdefault(_ipo_key(item.ipo), item)
    return tuple(unique.values())


def _is_operational_ipo(ipo: Ipo) -> bool:
    origin = str(ipo.data_origin or "").strip().lower()
    return not bool(ipo.is_sample) and origin not in UNSAFE_ORIGINS


def _ipo_key(ipo: Ipo) -> object:
    if ipo.id is not None:
        return ("id", ipo.id)
    if ipo.identity_key:
        return ("identity", ipo.identity_key)
    return (
        "natural",
        ipo.normalized_name or ipo.corp_name,
        ipo.subscription_start_date,
    )


def _event_badges(ipo: Ipo, start: date, end: date) -> tuple[str, ...]:
    events = (
        ("수요예측", ipo.demand_start_date, ipo.demand_end_date),
        ("청약", ipo.subscription_start_date, ipo.subscription_end_date),
        ("환불", ipo.refund_date, ipo.refund_date),
        ("납입", ipo.payment_date, ipo.payment_date),
        (
            "상장",
            ipo.actual_listing_date or ipo.listing_date,
            ipo.actual_listing_date or ipo.listing_date,
        ),
    )
    badges = tuple(
        label
        for label, event_start, event_end in events
        if _overlaps(event_start, event_end, start, end)
    )
    return badges or ("월간 포함",)


def _overlaps(
    event_start: Optional[date],
    event_end: Optional[date],
    start: date,
    end: date,
) -> bool:
    if event_start is None and event_end is None:
        return False
    resolved_start = event_start or event_end
    resolved_end = event_end or event_start
    return bool(resolved_start <= end and resolved_end >= start)


def _universe_sort_key(item: _UniverseItem) -> tuple[date, str, int]:
    ipo = item.ipo
    dates = tuple(
        value
        for value in (
            ipo.demand_start_date,
            ipo.subscription_start_date,
            ipo.payment_date,
            ipo.refund_date,
            ipo.actual_listing_date,
            ipo.listing_date,
        )
        if value is not None
    )
    return (min(dates, default=date.max), ipo.corp_name, ipo.id or 0)


def _load_related(session: Session, ipos: tuple[Ipo, ...]) -> dict[str, dict[int, list[object]]]:
    ids = tuple(ipo.id for ipo in ipos if ipo.id is not None)
    names_and_models = (
        ("underwriters", UnderwriterAllocation),
        ("demand", DemandForecast),
        ("structure", IpoStructureMetric),
        ("prices", ListingPrice),
        ("outcomes", OutcomeEvaluation),
        ("documents", SourceDocument),
        ("observations", IpoFactObservation),
    )
    related: dict[str, dict[int, list[object]]] = {name: {} for name, _model in names_and_models}
    if not ids:
        return related
    for name, model in names_and_models:
        rows = session.execute(select(model).where(model.ipo_id.in_(ids))).scalars().all()
        for row in rows:
            related[name].setdefault(row.ipo_id, []).append(row)
    return related


def load_official_fact_overrides(
    session: Session,
    ipos: tuple[Ipo, ...],
    generated_at: datetime,
) -> Mapping[int, Mapping[str, EvidenceFact]]:
    """Adapt append-only official observations into renderer-level evidence facts.

    The normalized keys intentionally mirror current-table field names. A collector
    can replace this adapter when it needs richer normalization, while the default
    path remains source-document-backed and point-in-time safe.
    """

    ids = tuple(ipo.id for ipo in ipos if ipo.id is not None)
    if not ids:
        return {}
    documents = (
        session.execute(select(SourceDocument).where(SourceDocument.ipo_id.in_(ids)))
        .scalars()
        .all()
    )
    document_by_id = {
        document.id: document for document in documents if _safe_source(document.source)
    }
    observations = (
        session.execute(select(IpoFactObservation).where(IpoFactObservation.ipo_id.in_(ids)))
        .scalars()
        .all()
    )
    selected: dict[int, dict[str, tuple[IpoFactObservation, SourceDocument, object]]] = {}
    stages = {
        ipo.id: _decision_stage(ipo, generated_at.date()) for ipo in ipos if ipo.id is not None
    }
    for observation in observations:
        document = document_by_id.get(observation.source_document_id)
        if document is None or not _observation_is_visible(
            observation,
            generated_at,
            stages.get(observation.ipo_id, "POST_CLOSE"),
        ):
            continue
        key, value = _normalized_observation(observation)
        if _is_actual_competition_key(key) and _is_default_competition(value):
            continue
        current = selected.setdefault(observation.ipo_id, {}).get(key)
        if current is None or _observation_sort_key(observation) > _observation_sort_key(
            current[0]
        ):
            selected[observation.ipo_id][key] = (observation, document, value)
    return {
        ipo_id: {
            key: _observation_fact(key, observation, document, value)
            for key, (observation, document, value) in facts.items()
        }
        for ipo_id, facts in selected.items()
    }


def _observation_is_visible(
    observation: IpoFactObservation,
    generated_at: datetime,
    stage: str,
) -> bool:
    cutoff = _database_datetime(generated_at)
    proof_times = tuple(
        _database_datetime(value)
        for value in (observation.observed_at, observation.fetched_at)
        if value is not None
    )
    if any(value > cutoff for value in proof_times):
        return False
    published_at = observation.published_at
    if published_at is not None and _database_datetime(published_at) > cutoff:
        published_kst = _database_datetime(published_at).astimezone(KST)
        cutoff_kst = cutoff.astimezone(KST)
        artificial_same_day_eod = (
            published_kst.date() == cutoff_kst.date()
            and published_kst.hour == 23
            and published_kst.minute == 59
        )
        if not artificial_same_day_eod or not proof_times:
            return False
    allowed_phases = {
        "PRE_OPEN": {"pre_subscription"},
        "IN_SUBSCRIPTION": {"pre_subscription", "during_subscription"},
        "POST_CLOSE": {
            "pre_subscription",
            "during_subscription",
            "post_subscription",
            "post_listing",
        },
    }
    return observation.knowledge_phase in allowed_phases[stage]


def _normalized_observation(
    observation: IpoFactObservation,
) -> tuple[str, object]:
    field_name = observation.field_name
    value = observation.value_json
    if isinstance(value, dict) and value.get("broker_name"):
        broker_name = str(value["broker_name"])
        normalized_value = value.get("value")
        return f"underwriter:{broker_name}:{field_name}", normalized_value
    for separator in (".", ":"):
        parts = field_name.split(separator)
        if len(parts) >= 3 and parts[0] in {"underwriter", "broker"}:
            return f"underwriter:{separator.join(parts[1:-1])}:{parts[-1]}", value
    return field_name, value


def _observation_fact(
    key: str,
    observation: IpoFactObservation,
    document: SourceDocument,
    value: object,
) -> EvidenceFact:
    state = (
        "post_event"
        if _is_retail_result_key(key)
        else _normalized_quality_state(observation.quality_state)
    )
    available = value is not None and state in {
        "verified",
        "cross_checked",
        "reported",
        "post_event",
    }
    rendered_value = (
        _large_money(value)
        if key in {"market_cap_at_offer", "free_float_market_cap"} and value is not None
        else _observation_value(value, observation.unit)
    )
    return EvidenceFact(
        label=observation.field_name,
        value=rendered_value,
        state=state,
        source=document.source,
        as_of=_format_as_of(
            observation.observed_at
            or observation.fetched_at
            or observation.published_at
            or document.fetched_at
        ),
        source_ref=document.official_url or document.document_key,
        available=available,
        raw_value=value,
    )


def _normalized_quality_state(value: str) -> str:
    return value if value in STATE_LABELS else "reported"


def _observation_value(value: object, unit: Optional[str]) -> str:
    if value is None:
        return "미수집"
    normalized_unit = str(unit or "").strip().lower()
    if normalized_unit in {"krw", "원"}:
        return _money(value) or "미수집"
    if normalized_unit == "백만원":
        return _scaled_money(value, "백만원", 1_000_000)
    if normalized_unit == "천원":
        return _scaled_money(value, "천원", 1_000)
    if normalized_unit == "ratio_to_one":
        return _ratio(float(value)) or "미수집"
    if normalized_unit in {"ratio", "percent"}:
        return _percent(float(value)) or "미수집"
    if normalized_unit == "shares":
        return _shares(int(value)) or "미수집"
    if normalized_unit == "applicants":
        return _count(int(value), "명") or "미수집"
    if normalized_unit == "institutions":
        return _count(int(value), "개 기관") or "미수집"
    if normalized_unit == "mixed" and isinstance(value, dict):
        return _price_distribution_value(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{value}{unit}" if unit else str(value)


def _scaled_money(value: object, unit: str, won_multiplier: int) -> str:
    numeric = float(value)
    hundred_million_won = numeric * won_multiplier / 100_000_000
    return f"{numeric:,.0f}{unit} ({hundred_million_won:,.2f}억원)"


def _large_money(value: object) -> str:
    numeric = float(value)
    return f"{numeric:,.0f}원 ({numeric / 100_000_000:,.1f}억원)"


def _price_distribution_value(value: Mapping[str, object]) -> str:
    label = str(value.get("label") or "가격 구간")
    participants = value.get("participants_count")
    ratio = value.get("ratio")
    shares = value.get("requested_shares")
    details = []
    if participants is not None:
        details.append(f"{int(participants):,}건")
    if ratio is not None:
        details.append(_percent(float(ratio)) or "")
    if shares is not None:
        details.append(f"{int(shares):,}주")
    rendered = ", ".join(item for item in details if item)
    return f"{label} ({rendered})" if rendered else label


def _price_distribution_fact(
    overrides: Mapping[str, EvidenceFact], missing_state: str
) -> EvidenceFact:
    keys = sorted(
        (key for key in overrides if key.startswith("institutional_price_distribution.")),
        key=lambda key: int(key.rsplit(".", 1)[-1]),
    )
    facts = tuple(overrides[key] for key in keys if overrides[key].available)
    if not facts:
        return _missing_fact("기관 가격분포", missing_state)
    first = facts[0]
    return replace(
        first,
        label="기관 가격분포",
        value=" · ".join(fact.value for fact in facts),
        source_ref=", ".join(dict.fromkeys(fact.source_ref for fact in facts if fact.source_ref))
        or None,
        raw_value=tuple(fact.raw_value for fact in facts),
    )


def _price_distribution_bands(
    overrides: Mapping[str, EvidenceFact],
) -> tuple[InstitutionalPriceDistributionBand, ...]:
    keys = sorted(
        (key for key in overrides if key.startswith("institutional_price_distribution.")),
        key=lambda key: int(key.rsplit(".", 1)[-1]),
    )
    bands = []
    for key in keys:
        fact = overrides[key]
        raw = fact.raw_value
        if not fact.available or not isinstance(raw, Mapping):
            continue
        bands.append(
            InstitutionalPriceDistributionBand(
                label=str(raw.get("label") or "가격 구간"),
                participant_ratio_pct=_distribution_percent(raw.get("ratio")),
                participant_ratio_basis="reported_ratio",
                reported_participant_ratio_pct=_distribution_percent(raw.get("ratio")),
                participants_count=_optional_int(raw.get("participants_count")),
                requested_shares=_optional_int(raw.get("requested_shares")),
                requested_shares_ratio_pct=_distribution_percent(raw.get("requested_shares_ratio")),
                requested_shares_ratio_basis="reported_ratio",
                reported_requested_shares_ratio_pct=_distribution_percent(
                    raw.get("requested_shares_ratio")
                ),
                price_krw=_optional_int(raw.get("price_krw")),
                source=fact.source,
                as_of=fact.as_of,
                source_ref=fact.source_ref,
            )
        )
    participant_counts = tuple(
        band.participants_count for band in bands if band.participants_count is not None
    )
    count_total = sum(participant_counts)
    counts_complete = bool(bands) and len(participant_counts) == len(bands)
    if counts_complete and count_total > 0:
        bands = [
            replace(
                band,
                participant_ratio_pct=band.participants_count / count_total * 100,
                participant_ratio_basis="participants_count_recalculated",
            )
            for band in bands
        ]
    elif counts_complete:
        bands = [
            replace(
                band,
                participant_ratio_pct=None,
                participant_ratio_basis="unavailable",
            )
            for band in bands
        ]
    elif not _reported_distribution_is_valid(
        tuple(band.reported_participant_ratio_pct for band in bands)
    ):
        bands = [
            replace(
                band,
                participant_ratio_pct=None,
                participant_ratio_basis="unavailable",
            )
            for band in bands
        ]
    requested_shares = tuple(
        band.requested_shares for band in bands if band.requested_shares is not None
    )
    requested_shares_total = sum(requested_shares)
    shares_complete = bool(bands) and len(requested_shares) == len(bands)
    if shares_complete and requested_shares_total > 0:
        bands = [
            replace(
                band,
                requested_shares_ratio_pct=band.requested_shares / requested_shares_total * 100,
                requested_shares_ratio_basis="requested_shares_recalculated",
            )
            for band in bands
        ]
    elif shares_complete:
        bands = [
            replace(
                band,
                requested_shares_ratio_pct=None,
                requested_shares_ratio_basis="unavailable",
            )
            for band in bands
        ]
    elif not _reported_distribution_is_valid(
        tuple(band.reported_requested_shares_ratio_pct for band in bands)
    ):
        bands = [
            replace(
                band,
                requested_shares_ratio_pct=None,
                requested_shares_ratio_basis="unavailable",
            )
            for band in bands
        ]
    return tuple(sorted(bands, key=_price_distribution_sort_key))


def _reported_distribution_is_valid(values: tuple[Optional[float], ...]) -> bool:
    # DART can publish 가격미제시 as a supplemental row outside the priced-row
    # subtotal, so this is not a stacked composition and must not be forced to 100%.
    return bool(values) and all(value is not None for value in values)


def _optional_int(value: object) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return None


def _distribution_percent(value: object) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    if -1 <= numeric <= 1:
        numeric *= 100
    return numeric if 0 <= numeric <= 100 else None


def _price_distribution_sort_key(
    band: InstitutionalPriceDistributionBand,
) -> tuple[int, int, str]:
    label = band.label.replace(" ", "")
    if "가격미제시" in label:
        rank = 5
    elif "초과" in label and "미만" in label:
        rank = 2
    elif "초과" in label:
        rank = 0
    elif "(상단)" in label:
        rank = 1
    elif "(하단)" in label:
        rank = 3
    elif "미만" in label:
        rank = 4
    else:
        rank = 2
    descending_price = -(band.price_krw or -1)
    return rank, descending_price, band.label


def _observation_sort_key(observation: IpoFactObservation) -> tuple[datetime, datetime, int]:
    return (
        _database_datetime(observation.effective_from or observation.published_at),
        _database_datetime(observation.observed_at),
        observation.id or 0,
    )


def _database_datetime(value: Optional[datetime]) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _is_actual_competition_key(key: str) -> bool:
    return key.endswith("actual_proportional_competition") or key in {
        "retail_competition_ratio",
        "retail_actual_competition_ratio",
        "overall_retail_competition_ratio",
    }


def _is_retail_result_key(key: str) -> bool:
    return (
        _is_actual_competition_key(key)
        or key.endswith(("actual_applicants_count", "subscribed_shares", "allocated_shares"))
        or key.startswith("retail_")
    )


def _is_default_competition(value: object) -> bool:
    try:
        normalized = str(value).replace(",", "").replace(":1", "").strip()
        return float(normalized) == 1000.0
    except (TypeError, ValueError):
        return False


def _apply_fact_overrides(
    facts: tuple[EvidenceFact, ...],
    overrides: Mapping[str, EvidenceFact],
    keys: tuple[tuple[str, ...], ...],
) -> tuple[EvidenceFact, ...]:
    return tuple(
        _prefer_override(overrides, aliases, fact.label, fact) for fact, aliases in zip(facts, keys)
    )


def _prefer_override(
    overrides: Mapping[str, EvidenceFact],
    keys: tuple[str, ...],
    label: str,
    fallback: EvidenceFact,
    *,
    actual_only: bool = False,
    reject_default_competition: bool = False,
) -> EvidenceFact:
    fact = next((overrides[key] for key in keys if key in overrides), None)
    if fact is None:
        return fallback
    if actual_only and (fact.state != "post_event" or not fact.source or not fact.as_of):
        return fallback
    if reject_default_competition and _is_default_competition(fact.value):
        return fallback
    return replace(fact, label=label)


def _build_brief(
    item: _UniverseItem,
    generated_at: datetime,
    related: dict[str, dict[int, list[object]]],
    overrides: Mapping[str, EvidenceFact],
) -> MonthlyIpoBrief:
    ipo = item.ipo
    ipo_id = ipo.id or -1
    stage = _decision_stage(ipo, generated_at.date())
    fallback_source = "기존 DB(필드 원문 미연결)"
    fallback_as_of = _format_as_of(ipo.updated_at)
    fallback_ref = None
    fallback_state = "legacy_unverified"

    identity = (
        _prefer_override(
            overrides,
            ("market",),
            "시장",
            _present_fact(
                "시장", ipo.market, fallback_state, fallback_source, fallback_as_of, fallback_ref
            ),
        ),
        _prefer_override(
            overrides,
            ("ipo_type",),
            "유형",
            _present_fact(
                "유형", ipo.ipo_type, fallback_state, fallback_source, fallback_as_of, fallback_ref
            ),
        ),
        _prefer_override(
            overrides,
            ("ticker",),
            "종목코드",
            _present_fact(
                "종목코드",
                ipo.ticker,
                fallback_state,
                fallback_source,
                fallback_as_of,
                fallback_ref,
            ),
        ),
    )
    schedule = (
        _prefer_override(
            overrides,
            ("demand_period",),
            "수요예측",
            _present_fact(
                "수요예측",
                _date_range(ipo.demand_start_date, ipo.demand_end_date),
                fallback_state,
                fallback_source,
                fallback_as_of,
                fallback_ref,
            ),
        ),
        _prefer_override(
            overrides,
            ("subscription_period",),
            "청약",
            _present_fact(
                "청약",
                _date_range(ipo.subscription_start_date, ipo.subscription_end_date),
                fallback_state,
                fallback_source,
                fallback_as_of,
                fallback_ref,
            ),
        ),
        _prefer_override(
            overrides,
            ("refund_date",),
            "환불",
            _present_fact(
                "환불",
                _date_text(ipo.refund_date),
                fallback_state,
                fallback_source,
                fallback_as_of,
                fallback_ref,
            ),
        ),
        _prefer_override(
            overrides,
            ("payment_date",),
            "납입",
            _present_fact(
                "납입",
                _date_text(ipo.payment_date),
                fallback_state,
                fallback_source,
                fallback_as_of,
                fallback_ref,
            ),
        ),
        _prefer_override(
            overrides,
            ("listing_date", "planned_listing_date"),
            "예정 상장",
            _present_fact(
                "예정 상장",
                _date_text(ipo.listing_date),
                fallback_state,
                fallback_source,
                fallback_as_of,
                fallback_ref,
            ),
        ),
        _prefer_override(
            overrides,
            ("actual_listing_date",),
            "실제 상장",
            _present_fact(
                "실제 상장",
                _date_text(ipo.actual_listing_date),
                fallback_state
                if ipo.actual_listing_date
                else _outcome_missing_state(ipo, generated_at.date(), 0),
                fallback_source if ipo.actual_listing_date else None,
                fallback_as_of if ipo.actual_listing_date else None,
                fallback_ref if ipo.actual_listing_date else None,
            ),
        ),
    )
    offer = (
        _prefer_override(
            overrides,
            ("offer_price_band",),
            "공모가 밴드",
            _present_fact(
                "공모가 밴드",
                _price_band(ipo.offer_price_min, ipo.offer_price_max),
                fallback_state,
                fallback_source,
                fallback_as_of,
                fallback_ref,
            ),
        ),
        _prefer_override(
            overrides,
            ("offer_price_final",),
            "확정 공모가",
            _present_fact(
                "확정 공모가",
                _money(ipo.offer_price_final),
                fallback_state,
                fallback_source,
                fallback_as_of,
                fallback_ref,
            ),
        ),
        _prefer_override(
            overrides,
            ("offer_amount_million",),
            "공모금액",
            _present_fact(
                "공모금액",
                _million_won(ipo.offer_amount_million),
                fallback_state,
                fallback_source,
                fallback_as_of,
                fallback_ref,
            ),
        ),
    )

    demand_row = _latest_safe(
        related["demand"].get(ipo_id, []),
        lambda row: (row.result_published_at or row.updated_at, row.id),
        lambda row: row.source,
    )
    demand_missing = _demand_missing_state(ipo, generated_at.date())
    demand = _apply_fact_overrides(
        _demand_facts(demand_row, demand_missing),
        overrides,
        (
            ("institutional_competition_ratio",),
            ("participants_count",),
            ("lockup_commitment_ratio",),
            ("price_band_top_ratio",),
            ("above_band_participation_ratio",),
            ("final_price_position",),
        ),
    )
    demand = (*demand, _price_distribution_fact(overrides, demand_missing))
    institutional_price_distribution = _price_distribution_bands(overrides)

    underwriter_rows = tuple(
        sorted(
            (row for row in related["underwriters"].get(ipo_id, []) if _safe_source(row.source)),
            key=lambda row: (row.broker_name, row.id),
        )
    )
    underwriters = tuple(_underwriter_brief(row, stage, overrides) for row in underwriter_rows)
    retail_state = _retail_missing_state(stage)
    retail = (
        _prefer_override(
            overrides,
            (
                "retail_competition_ratio",
                "retail_actual_competition_ratio",
                "overall_retail_competition_ratio",
            ),
            "전체 실제 일반청약 경쟁률",
            _missing_fact("전체 실제 일반청약 경쟁률", retail_state),
            actual_only=True,
            reject_default_competition=True,
        ),
        _prefer_override(
            overrides,
            ("retail_applicants_count", "overall_retail_applicants_count"),
            "전체 실제 신청자",
            _missing_fact("전체 실제 신청자", retail_state),
            actual_only=True,
        ),
    )

    structure_row = _latest_safe(
        related["structure"].get(ipo_id, []),
        lambda row: (row.as_of_date, row.updated_at, row.id),
        lambda row: row.source,
    )
    structure = _apply_fact_overrides(
        _structure_facts(structure_row),
        overrides,
        (
            ("total_offer_shares",),
            ("new_share_ratio",),
            ("secondary_share_ratio",),
            ("free_float_ratio_on_listing", "free_float_ratio"),
            ("free_float_shares_on_listing", "free_float_shares"),
            ("free_float_market_cap",),
            ("vc_overhang_ratio",),
            ("lockup_expiry_schedule_json",),
            ("use_of_proceeds_summary",),
            ("risk_flags_json",),
        ),
    )

    financial = (
        _prefer_override(
            overrides,
            ("financial_period",),
            "최근 재무기간",
            _missing_fact("최근 재무기간", "missing"),
        ),
        _prefer_override(
            overrides,
            ("financial_revenue",),
            "매출·영업수익",
            _missing_fact("매출·영업수익", "missing"),
        ),
        _prefer_override(
            overrides,
            ("financial_operating_profit",),
            "영업이익(손실)",
            _missing_fact("영업이익(손실)", "missing"),
        ),
        _prefer_override(
            overrides,
            ("financial_net_income",),
            "당기순이익(손실)",
            _missing_fact("당기순이익(손실)", "missing"),
        ),
        _prefer_override(
            overrides,
            ("financial_statement_basis",),
            "재무제표 기준",
            _missing_fact("재무제표 기준", "missing"),
        ),
    )
    valuation = (
        _prefer_override(
            overrides,
            ("market_cap_at_offer",),
            "공모가 기준 시가총액",
            _missing_fact("공모가 기준 시가총액", "missing"),
        ),
        _prefer_override(
            overrides,
            ("per", "valuation_per"),
            "PER",
            _missing_fact("PER", "missing"),
        ),
        _prefer_override(
            overrides,
            ("pbr", "valuation_pbr"),
            "PBR",
            _missing_fact("PBR", "missing"),
        ),
        _prefer_override(
            overrides,
            ("psr", "valuation_psr"),
            "PSR",
            _missing_fact("PSR", "missing"),
        ),
        _prefer_override(
            overrides,
            ("peer_group", "peer_group_json"),
            "비교기업",
            _missing_fact("비교기업", "missing"),
        ),
        _prefer_override(
            overrides,
            ("valuation_discount_premium_ratio",),
            "평가 할인·할증",
            _missing_fact("평가 할인·할증", "missing"),
        ),
    )

    price_row = _latest_safe(
        related["prices"].get(ipo_id, []),
        lambda row: (row.updated_at, row.id),
        lambda row: row.source,
    )
    outcome_row = _latest(
        related["outcomes"].get(ipo_id, []),
        lambda row: (row.evaluated_at, row.id),
    )
    outcomes = _apply_fact_overrides(
        _outcome_facts(ipo, outcome_row, price_row, generated_at.date()),
        overrides,
        (
            ("d0_open_return_pct",),
            ("d0_close_return_pct",),
            ("d1_close_return_pct",),
            ("d3_close_return_pct",),
        ),
    )

    underwriter_available = bool(underwriters) and all(row.role.available for row in underwriters)
    retail_available = retail[0].available and retail[1].available
    if underwriters:
        retail_available = retail_available and all(
            row.actual_competition.available
            and row.actual_applicants.available
            and row.retail_allocated_shares.available
            for row in underwriters
        )
    required = [
        schedule[0],
        schedule[1],
        schedule[4],
        offer[1],
        demand[0],
        demand[1],
        demand[2],
        demand[6],
        structure[0],
        structure[1],
        structure[2],
        structure[3],
        financial[0],
        financial[1],
        financial[2],
        financial[3],
        valuation[0],
    ]
    required_available = [fact.available for fact in required] + [underwriter_available]
    if stage == "POST_CLOSE":
        required_available.append(retail_available)
    listing_day = ipo.actual_listing_date or ipo.listing_date
    if listing_day is not None and generated_at.date() >= listing_day:
        required_available.extend(fact.available for fact in outcomes)
    complete = sum(required_available)
    total = len(required_available)
    all_facts = (
        *identity,
        *schedule,
        *offer,
        *demand,
        *retail,
        *structure,
        *financial,
        *valuation,
        *outcomes,
    )
    missing_labels = tuple(
        fact.label for fact in all_facts if not fact.available and fact.state == "missing"
    )
    missing_labels += tuple(
        f"{row.broker_name} 실제 일반청약 경쟁률"
        for row in underwriters
        if not row.actual_competition.available and row.actual_competition.state == "missing"
    )
    quality_label = "자료 확인 가능" if complete == total else "데이터 보강 필요"
    return MonthlyIpoBrief(
        ipo_id=ipo.id,
        corp_name=ipo.corp_name,
        stage=stage,
        stage_as_of=_format_as_of(generated_at),
        status=ipo.status or "미정",
        event_badges=item.event_badges,
        reconciliation_state=item.reconciliation_state,
        identity=identity,
        schedule=schedule,
        offer=offer,
        demand=demand,
        institutional_price_distribution=institutional_price_distribution,
        underwriters=underwriters,
        retail=retail,
        structure=structure,
        financial=financial,
        valuation=valuation,
        outcomes=outcomes,
        missing_labels=tuple(dict.fromkeys(missing_labels)),
        completeness_complete=complete,
        completeness_total=total,
        quality_label=quality_label,
    )


def _demand_facts(row: Optional[DemandForecast], missing_state: str) -> tuple[EvidenceFact, ...]:
    if row is None:
        return (
            _missing_fact("기관 경쟁률", missing_state),
            _missing_fact("참여기관 수", missing_state),
            _missing_fact("의무보유확약", missing_state),
            _missing_fact("밴드 상단 제시", missing_state),
            _missing_fact("밴드 초과 제시", missing_state),
            _missing_fact("확정가 위치", missing_state),
        )
    state = _source_state(row.source)
    source = row.source or "기존 DB(원문 미연결)"
    as_of = _format_as_of(row.result_published_at or row.updated_at)
    return (
        _present_fact(
            "기관 경쟁률",
            _ratio(row.institutional_competition_ratio),
            state,
            source,
            as_of,
            row.receipt_no,
        ),
        _present_fact(
            "참여기관 수",
            _count(row.participants_count, "개"),
            state,
            source,
            as_of,
            row.receipt_no,
        ),
        _present_fact(
            "의무보유확약",
            _percent(row.lockup_commitment_ratio),
            state,
            source,
            as_of,
            row.receipt_no,
        ),
        _present_fact(
            "밴드 상단 제시",
            _percent(row.price_band_top_ratio),
            state,
            source,
            as_of,
            row.receipt_no,
        ),
        _present_fact(
            "밴드 초과 제시",
            _percent(row.above_band_participation_ratio),
            state,
            source,
            as_of,
            row.receipt_no,
        ),
        _present_fact(
            "확정가 위치", row.final_price_position, state, source, as_of, row.receipt_no
        ),
    )


def _underwriter_brief(
    row: UnderwriterAllocation,
    stage: str,
    overrides: Mapping[str, EvidenceFact],
) -> UnderwriterBrief:
    state = _source_state(row.source)
    source = row.source or "기존 DB(원문 미연결)"
    as_of = _format_as_of(row.updated_at)
    actual_is_verified = (
        bool(row.actual_source)
        and _safe_source(row.actual_source)
        and row.actual_recorded_at is not None
    )
    competition_is_actual = (
        actual_is_verified
        and row.actual_proportional_competition is not None
        and float(row.actual_proportional_competition) != 1000.0
    )
    actual_state = _retail_missing_state(stage)
    actual_source = row.actual_source if actual_is_verified else None
    actual_as_of = _format_as_of(row.actual_recorded_at) if actual_is_verified else None
    competition = (
        _present_fact(
            "실제 일반청약 경쟁률",
            _ratio(row.actual_proportional_competition),
            "post_event",
            actual_source,
            actual_as_of,
        )
        if competition_is_actual
        else _missing_fact("실제 일반청약 경쟁률", actual_state)
    )
    applicants = (
        _present_fact(
            "실제 신청자",
            _count(row.actual_applicants_count, "명"),
            "post_event",
            actual_source,
            actual_as_of,
        )
        if actual_is_verified
        else _missing_fact("실제 신청자", actual_state)
    )
    broker_prefixes = (
        f"underwriter:{row.broker_name}:",
        f"broker:{row.broker_name}:",
    )
    role_keys = tuple(f"{prefix}role" for prefix in broker_prefixes)
    allocated_keys = tuple(f"{prefix}allocated_shares" for prefix in broker_prefixes)
    competition_keys = tuple(
        f"{prefix}actual_proportional_competition" for prefix in broker_prefixes
    )
    applicants_keys = tuple(f"{prefix}actual_applicants_count" for prefix in broker_prefixes)
    assumption_source = row.assumption_source or "기존 DB(공식 조건 미연결)"
    assumption_as_of = _format_as_of(row.assumption_updated_at or row.updated_at)
    return UnderwriterBrief(
        broker_name=row.broker_name,
        role=_prefer_override(
            overrides,
            role_keys,
            "역할",
            _present_fact("역할", row.role, state, source, as_of),
        ),
        public_offer_shares=_present_fact(
            "인수·공모주식",
            _shares(row.public_offer_shares),
            "legacy_unverified",
            "기존 DB(공식 배정 원문 미연결)",
            as_of,
        ),
        retail_allocated_shares=_prefer_override(
            overrides,
            allocated_keys,
            "일반배정 물량",
            _present_fact(
                "일반배정 물량",
                _shares(row.retail_allocated_shares),
                "legacy_unverified",
                "기존 DB(공식 배정 원문 미연결)",
                as_of,
            ),
            actual_only=True,
        ),
        actual_competition=_prefer_override(
            overrides,
            competition_keys,
            "실제 일반청약 경쟁률",
            competition,
            actual_only=True,
            reject_default_competition=True,
        ),
        actual_applicants=_prefer_override(
            overrides,
            applicants_keys,
            "실제 신청자",
            applicants,
            actual_only=True,
        ),
        subscription_fee=_present_fact(
            "청약 수수료",
            _money(row.subscription_fee),
            "estimated",
            assumption_source,
            assumption_as_of,
        ),
        account_open_deadline=_present_fact(
            "계좌 개설 마감",
            _date_text(row.account_open_deadline),
            "estimated",
            assumption_source,
            assumption_as_of,
        ),
        source=source,
        as_of=as_of,
    )


def _structure_facts(row: Optional[IpoStructureMetric]) -> tuple[EvidenceFact, ...]:
    labels = (
        "총 공모주식",
        "신주 비율",
        "구주 비율",
        "상장일 유통가능 비율",
        "상장일 유통가능 주식",
        "유통가능 시가총액",
        "VC 오버행",
        "보호예수 만기",
        "공모자금 용도",
        "구조 위험",
    )
    if row is None:
        return tuple(_missing_fact(label, "missing") for label in labels)
    state = _source_state(row.source)
    source = row.source or "기존 DB(원문 미연결)"
    as_of = _format_as_of(row.as_of_date or row.updated_at)
    values = (
        _shares(row.total_offer_shares),
        _percent(row.new_share_ratio),
        _percent(row.secondary_share_ratio),
        _percent(row.free_float_ratio_on_listing),
        _shares(row.free_float_shares_on_listing),
        _money(row.free_float_market_cap),
        _percent(row.vc_overhang_ratio),
        _json_value(row.lockup_expiry_schedule_json),
        row.use_of_proceeds_summary,
        _json_value(row.risk_flags_json),
    )
    return tuple(
        _present_fact(label, value, state, source, as_of) for label, value in zip(labels, values)
    )


def _outcome_facts(
    ipo: Ipo,
    outcome: Optional[OutcomeEvaluation],
    price: Optional[ListingPrice],
    today: date,
) -> tuple[EvidenceFact, ...]:
    source = price.source if price and price.source else "성과평가"
    as_of = _format_as_of(price.updated_at if price else outcome.evaluated_at if outcome else None)
    offer_price = float(ipo.offer_price_final) if ipo.offer_price_final else None
    rows = (
        (
            "D0 시초 반응",
            outcome.d0_open_return_pct
            if outcome
            else _return_from_price(price.d0_open_price if price else None, offer_price),
            0,
        ),
        (
            "D0 종가 반응",
            outcome.d0_close_return_pct
            if outcome
            else _return_from_price(price.d0_close_price if price else None, offer_price),
            0,
        ),
        (
            "D1 종가 반응",
            outcome.d1_close_return_pct
            if outcome
            else _return_from_price(price.d1_close_price if price else None, offer_price),
            1,
        ),
        (
            "D3 종가 반응",
            outcome.d3_close_return_pct
            if outcome
            else _return_from_price(price.d3_close_price if price else None, offer_price),
            3,
        ),
    )
    return tuple(
        _present_fact(label, _signed_percent(value), "post_event", source, as_of)
        if value is not None and _safe_source(source)
        else _missing_fact(label, _outcome_missing_state(ipo, today, day_offset))
        for label, value, day_offset in rows
    )


def _present_fact(
    label: str,
    value: object,
    state: str,
    source: Optional[str],
    as_of: Optional[str],
    source_ref: Optional[str] = None,
) -> EvidenceFact:
    if value is None or value == "":
        return _missing_fact(label, "missing")
    return EvidenceFact(
        label=label,
        value=str(value),
        state=state,
        source=source,
        as_of=as_of,
        source_ref=source_ref or source,
        available=state in {"verified", "cross_checked", "reported", "post_event"},
    )


def _missing_fact(label: str, state: str) -> EvidenceFact:
    return EvidenceFact(
        label=label,
        value=STATE_LABELS[state],
        state=state,
        available=False,
    )


def _source_state(source: Optional[str]) -> str:
    normalized = str(source or "").strip().lower()
    return "legacy_unverified" if normalized in {"", "manual", "unknown", "legacy"} else "reported"


def _safe_source(source: Optional[str]) -> bool:
    normalized = str(source or "").strip().lower()
    return not any(marker in normalized for marker in UNSAFE_SOURCE_MARKERS)


def _latest_safe(rows: Iterable[object], key, source) -> Optional[object]:
    safe_rows = tuple(row for row in rows if _safe_source(source(row)))
    return _latest(safe_rows, key)


def _latest(rows: Iterable[object], key) -> Optional[object]:
    materialized = tuple(rows)
    return max(materialized, key=key) if materialized else None


def _decision_stage(ipo: Ipo, today: date) -> str:
    start = ipo.subscription_start_date
    end = ipo.subscription_end_date or start
    if start is None or today < start:
        return "PRE_OPEN"
    if end is not None and today <= end:
        return "IN_SUBSCRIPTION"
    return "POST_CLOSE"


def _demand_missing_state(ipo: Ipo, today: date) -> str:
    end = ipo.demand_end_date
    return "not_published" if end is not None and today <= end else "missing"


def _retail_missing_state(stage: str) -> str:
    return "not_published" if stage != "POST_CLOSE" else "missing"


def _outcome_missing_state(ipo: Ipo, today: date, day_offset: int) -> str:
    listing = ipo.actual_listing_date or ipo.listing_date
    if listing is None:
        return "missing"
    return "waiting" if today <= listing + timedelta(days=day_offset) else "missing"


def _return_from_price(value: object, offer_price: Optional[float]) -> Optional[float]:
    if value is None or offer_price in {None, 0}:
        return None
    return (float(value) / float(offer_price) - 1.0) * 100.0


def _partition_items(
    read_model: MonthlyEmailReadModel,
    items: tuple[_PartItem, ...],
    title: str,
    max_html_bytes: int,
) -> tuple[tuple[_PartItem, ...], ...]:
    if not items:
        empty = _render_part(read_model, (), title, part_number=999, total_parts=999)
        if len(empty.html.encode("utf-8")) > max_html_bytes:
            raise ValueError("monthly IPO email header exceeds max_html_bytes")
        return ((),)
    groups: list[tuple[_PartItem, ...]] = []
    current: tuple[_PartItem, ...] = ()
    for item in items:
        candidate = (*current, item)
        trial = _render_part(read_model, candidate, title, part_number=999, total_parts=999)
        if len(trial.html.encode("utf-8")) <= max_html_bytes:
            current = candidate
            continue
        if not current:
            raise ValueError(f"single IPO detail exceeds max_html_bytes: {item.brief.corp_name}")
        groups.append(current)
        current = (item,)
        single = _render_part(read_model, current, title, part_number=999, total_parts=999)
        if len(single.html.encode("utf-8")) > max_html_bytes:
            raise ValueError(f"single IPO detail exceeds max_html_bytes: {item.brief.corp_name}")
    groups.append(current)
    return tuple(groups)


def _render_part(
    read_model: MonthlyEmailReadModel,
    items: tuple[_PartItem, ...],
    title: str,
    *,
    part_number: int,
    total_parts: int,
    single_report: bool = False,
) -> EmailContent:
    core = tuple(item.brief for item in items if not item.appendix)
    appendix = tuple(item.brief for item in items if item.appendix)
    subscription_events = sum(
        any("청약" in _event_badge_label(badge) for badge in brief.event_badges)
        for brief in read_model.core
    )
    listing_events = sum(
        any("상장" in _event_badge_label(badge) for badge in brief.event_badges)
        for brief in read_model.core
    )
    needs_data = sum(bool(brief.missing_labels) for brief in read_model.core)
    if single_report:
        subject = title
    else:
        suffix = f" ({part_number}/{total_parts})" if total_parts > 1 else ""
        subject = (
            f"{title} 전체 {len(read_model.core)}개 · 청약 이벤트 {subscription_events} · "
            f"상장 이벤트 {listing_events} · 데이터 보강 필요 {needs_data}{suffix}"
        )
    text = _render_text(
        read_model,
        core,
        appendix,
        part_number,
        total_parts,
        single_report=single_report,
    )
    html = _render_html(
        read_model,
        core,
        appendix,
        part_number,
        total_parts,
        single_report=single_report,
    )
    return EmailContent(subject=subject, text=text, html=html)


def _render_text(
    read_model: MonthlyEmailReadModel,
    core: tuple[MonthlyIpoBrief, ...],
    appendix: tuple[MonthlyIpoBrief, ...],
    part_number: int,
    total_parts: int,
    *,
    single_report: bool = False,
) -> str:
    if single_report:
        brief = core[0]
        lines = [
            "[청약 D-1] 내일 일반청약 시작",
            f"{brief.corp_name} 종목별 사실 리포트",
            f"공식 대조 {read_model.reconciliation_state}",
            f"업데이트: {_format_as_of(read_model.generated_at)}",
            f"데이터 hash: {read_model.evidence_hash[:12]}",
        ]
    else:
        lines = [
            f"{read_model.month_start.year}년 {read_model.month_start.month}월 IPO 전체 사실 브리핑",
            f"전체 {len(read_model.core)}개 · 공식 대조 {read_model.reconciliation_state}",
            f"업데이트: {_format_as_of(read_model.generated_at)}",
            f"데이터 hash: {read_model.evidence_hash[:12]}",
        ]
    if total_parts > 1:
        lines.append(f"분할 메일: {part_number}/{total_parts} · 모든 파트를 함께 확인하세요.")
    lines.extend(["", "한눈에 보기"])
    lines.extend(_text_roster(brief, False) for brief in core)
    if appendix:
        lines.extend(["", "이달 신규·정정·철회 공시 부록"])
        lines.extend(_text_roster(brief, True) for brief in appendix)
    if not core and not appendix:
        lines.append("- 수록 종목 없음")
    if core:
        lines.extend(["", "IPO별 상세 근거"])
        for brief in core:
            lines.extend(_text_brief(brief))
    if appendix:
        lines.extend(["", "공시 부록 상세"])
        for brief in appendix:
            lines.extend(_text_brief(brief))
    lines.extend(_historical_text(read_model.historical_evidence))
    lines.extend(["", DISCLAIMER])
    return "\n".join(lines)


def _text_roster(brief: MonthlyIpoBrief, appendix: bool) -> str:
    label = "부록" if appendix else STAGE_LABELS[brief.stage]
    summary = "\n".join(
        f"  - {item_label}: {value} [{meta}]" if meta else f"  - {item_label}: {value}"
        for item_label, value, meta in _summary_items(brief)
    )
    return (
        f"- [{label}] {brief.corp_name} · {_event_badges_text(brief.event_badges)} · "
        f"완성도 {brief.completeness_complete}/{brief.completeness_total} "
        f"({brief.quality_label})\n{summary}"
    )


def _text_brief(brief: MonthlyIpoBrief) -> list[str]:
    lines = [
        "",
        f"[{STAGE_LABELS[brief.stage]}] {brief.corp_name}",
        f"상태: {brief.status} · stage as-of {brief.stage_as_of} · 대조 {brief.reconciliation_state}",
        f"포함 이유: {_event_badges_text(brief.event_badges)}",
        f"데이터 완성도: {brief.completeness_complete}/{brief.completeness_total} · {brief.quality_label}",
    ]
    for title, facts in (
        ("기본", brief.identity),
        ("전체 일정", brief.schedule),
        ("공모", brief.offer),
        ("기관 수요예측", brief.demand),
    ):
        lines.append(f"{title}:")
        lines.extend(f"- {_fact_text(fact)}" for fact in facts)
    lines.extend(_price_distribution_text(brief))
    lines.append("모든 주관사·일반청약:")
    if brief.underwriters:
        for row in brief.underwriters:
            lines.append(f"- {row.broker_name} [출처 {row.source} · as-of {row.as_of}]")
            lines.extend(f"  - {_fact_text(fact)}" for fact in _underwriter_facts(row))
    else:
        lines.append("- 미수집")
    for title, facts in (
        ("전체 일반청약 결과", brief.retail),
        ("공모 구조", brief.structure),
        ("최근 재무", brief.financial),
        ("밸류에이션", brief.valuation),
        ("시장 반응", brief.outcomes),
    ):
        lines.append(f"{title}:")
        lines.extend(f"- {_fact_text(fact)}" for fact in facts)
    lines.append(
        "미수집 항목: " + (", ".join(brief.missing_labels) if brief.missing_labels else "없음")
    )
    return lines


def _render_html(
    read_model: MonthlyEmailReadModel,
    core: tuple[MonthlyIpoBrief, ...],
    appendix: tuple[MonthlyIpoBrief, ...],
    part_number: int,
    total_parts: int,
    *,
    single_report: bool = False,
) -> str:
    split_note = (
        f'<div style="margin-top:8px;color:#FFE6A6">분할 메일 {part_number}/{total_parts} · 모든 파트를 함께 확인하세요.</div>'
        if total_parts > 1
        else ""
    )
    roster_rows = "".join(_html_roster(brief, False) for brief in core)
    appendix_roster = "".join(_html_roster(brief, True) for brief in appendix)
    empty_row = (
        '<tr><td style="padding:14px;color:#687789">수록 종목 없음</td></tr>'
        if not roster_rows and not appendix_roster
        else ""
    )
    details = "".join(_html_brief(brief) for brief in core)
    appendix_details = "".join(_html_brief(brief) for brief in appendix)
    appendix_section = (
        f'<tr><td style="padding:22px 22px 8px;font-size:18px;font-weight:800">이달 신규·정정·철회 공시 부록</td></tr>'
        f'<tr><td style="padding:0 22px"><table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border:1px solid #D9E2EC">{appendix_roster}</table></td></tr>'
        f'<tr><td style="padding:12px 0">{appendix_details}</td></tr>'
        if appendix
        else ""
    )
    historical_section = _historical_html(read_model.historical_evidence)
    if single_report:
        brief = core[0]
        eyebrow = "KOREA IPO ASSISTANT · SUBSCRIPTION D-1"
        heading = f"{escape(brief.corp_name)} 종목별 사실 리포트"
        header_meta = (
            f"공식 대조 {escape(read_model.reconciliation_state)} · "
            f"{_format_as_of(read_model.generated_at)}"
        )
        d1_banner = '<tr><td bgcolor="#FFF3CD" style="padding:12px 22px;color:#694C00;font-size:15px;font-weight:800">청약 D-1 · 내일 일반청약 시작</td></tr>'
    else:
        eyebrow = "KOREA IPO ASSISTANT · FACTS V3"
        heading = (
            f"{read_model.month_start.year}년 {read_model.month_start.month}월 IPO 전체 사실 브리핑"
        )
        header_meta = (
            f"전체 {len(read_model.core)}개 · 공식 대조 "
            f"{escape(read_model.reconciliation_state)} · {_format_as_of(read_model.generated_at)}"
        )
        d1_banner = ""
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;background:#EEF2F7;font-family:Arial,'Apple SD Gothic Neo',sans-serif;color:#152238;line-height:1.55;word-break:break-word">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" bgcolor="#EEF2F7"><tr><td align="center" style="padding:18px 8px">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" bgcolor="#FFFFFF" style="max-width:760px">
<tr><td bgcolor="#102A43" style="padding:26px 22px;color:#FFFFFF"><div style="font-size:12px;color:#9FD5FF;font-weight:800">{eyebrow}</div><div style="font-size:24px;font-weight:800;margin-top:6px">{heading}</div><div style="margin-top:8px;color:#D9E8F5">{header_meta}</div>{split_note}</td></tr>
{d1_banner}
<tr><td style="padding:20px 22px 8px;font-size:18px;font-weight:800">한눈에 보기</td></tr>
<tr><td style="padding:0 22px"><table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border:1px solid #D9E2EC">{roster_rows}{empty_row}</table></td></tr>
<tr><td style="padding:12px 0">{details}</td></tr>
{appendix_section}
{historical_section}
<tr><td style="padding:20px 22px;color:#687789;font-size:12px">데이터 hash {escape(read_model.evidence_hash[:12])}<br>{escape(DISCLAIMER)}</td></tr>
</table></td></tr></table></body></html>"""


def _historical_text(
    history: Optional[HistoricalEvidenceAvailability],
) -> list[str]:
    if history is None:
        return []
    lines = [
        "",
        "과거 기준판 (설명용 사실)",
        (
            f"DB gross 상장 결과: {history.gross_outcome_count}개 · "
            f"엄격 시점 일치 완전 표본: {history.strict_complete_pair_count}/"
            f"{history.minimum_complete_pairs} · 추천 게이트: {_history_gate_label(history)}"
        ),
    ]
    lines.extend(f"- {_history_distribution_text(item)}" for item in history.distributions)
    lines.append(HISTORICAL_LIMITATION)
    return lines


def _historical_html(history: Optional[HistoricalEvidenceAvailability]) -> str:
    if history is None:
        return ""
    rows = "".join(
        '<tr><td style="padding:6px 8px;border-bottom:1px solid #E8EDF2">'
        f"{escape(_history_distribution_text(item))}</td></tr>"
        for item in history.distributions
    )
    gate = escape(_history_gate_label(history))
    return (
        '<tr><td style="padding:22px 22px 8px;font-size:18px;font-weight:800">'
        "과거 기준판 (설명용 사실)</td></tr>"
        '<tr><td style="padding:0 22px 18px">'
        '<div style="padding:12px;background:#F7F9FC">'
        f"DB gross 상장 결과 {history.gross_outcome_count}개 · 엄격 시점 일치 완전 표본 "
        f"{history.strict_complete_pair_count}/{history.minimum_complete_pairs} · "
        f"추천 게이트 {gate}</div>"
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        f'style="margin-top:8px;border:1px solid #D9E2EC">{rows}</table>'
        '<div style="margin-top:8px;color:#805500;font-size:12px">'
        f"{escape(HISTORICAL_LIMITATION)}</div></td></tr>"
    )


def _history_distribution_text(item: HistoricalReturnDistribution) -> str:
    if item.sample_count == 0:
        return f"{item.label}: n=0 · 표본 없음"
    return (
        f"{item.label}: n={item.sample_count} · 중앙값 "
        f"{_signed_percent(item.median_return_pct)} · IQR "
        f"{_signed_percent(item.q1_return_pct)}~{_signed_percent(item.q3_return_pct)} · 범위 "
        f"{_signed_percent(item.min_return_pct)}~{_signed_percent(item.max_return_pct)} · "
        f"손실 빈도 {float(item.loss_rate or 0) * 100:.1f}%"
    )


def _history_gate_label(history: HistoricalEvidenceAvailability) -> str:
    labels = {
        "ready": "통과(이 facts-only 메일은 추천을 생성하지 않음)",
        "validation_required": "비활성(별도 검증 승인 필요)",
        "insufficient_sample": "비활성(표본 부족)",
    }
    return labels.get(history.validation_status, "비활성(검증 상태 확인 필요)")


def _html_roster(brief: MonthlyIpoBrief, appendix: bool) -> str:
    label = "부록" if appendix else STAGE_LABELS[brief.stage]
    summary_rows = "".join(
        '<tr><td style="padding:5px 6px;color:#526477;width:30%;font-size:12px">'
        f'{escape(item_label)}</td><td style="padding:5px 6px;font-size:13px"><strong>{escape(value)}</strong>'
        + (f'<div style="font-size:10px;color:#7B8794">{escape(meta)}</div>' if meta else "")
        + "</td></tr>"
        for item_label, value, meta in _summary_items(brief)
    )
    return f'<tr><td style="padding:14px;border-bottom:1px solid #E8EDF2"><strong>{escape(brief.corp_name)}</strong> <span style="color:#1677C8">[{escape(label)}]</span><div style="font-size:12px;color:#526477;margin-top:3px">{escape(_event_badges_text(brief.event_badges))} · 완성도 {brief.completeness_complete}/{brief.completeness_total} · {escape(brief.quality_label)}</div><table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin-top:8px;background:#F7F9FC">{summary_rows}</table></td></tr>'


def _html_brief(brief: MonthlyIpoBrief) -> str:
    leading_sections = "".join(
        _html_fact_section(title, facts)
        for title, facts in (
            ("기본", brief.identity),
            ("전체 일정", brief.schedule),
            ("공모", brief.offer),
            ("기관 수요예측", brief.demand),
        )
    )
    trailing_sections = "".join(
        _html_fact_section(title, facts)
        for title, facts in (
            ("전체 일반청약 결과", brief.retail),
            ("공모 구조", brief.structure),
            ("최근 재무", brief.financial),
            ("밸류에이션", brief.valuation),
            ("시장 반응", brief.outcomes),
        )
    )
    underwriters = (
        "".join(_html_underwriter(row) for row in brief.underwriters)
        if brief.underwriters
        else '<div style="padding:8px 0;color:#805500">미수집</div>'
    )
    missing = ", ".join(brief.missing_labels) if brief.missing_labels else "없음"
    distribution = _price_distribution_html(brief)
    return f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td style="padding:10px 22px"><table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border:1px solid #D9E2EC"><tr><td style="padding:18px"><div style="font-size:20px;font-weight:800">{escape(brief.corp_name)}</div><div style="margin-top:4px;color:#526477;font-size:13px">{escape(STAGE_LABELS[brief.stage])} · {escape(brief.status)} · stage as-of {escape(brief.stage_as_of)} · 대조 {escape(brief.reconciliation_state)}</div><div style="margin-top:5px;color:#1677C8;font-size:13px">포함 이유: {escape(_event_badges_text(brief.event_badges))}</div><div style="margin-top:7px;font-weight:700">완성도 {brief.completeness_complete}/{brief.completeness_total} · {escape(brief.quality_label)}</div>{leading_sections}{distribution}{trailing_sections}<div style="margin-top:14px;font-weight:800">모든 주관사·일반청약</div>{underwriters}<div style="margin-top:14px;padding:10px;background:#FFF7E6;color:#805500;font-size:12px"><strong>미수집 항목</strong><br>{escape(missing)}</div></td></tr></table></td></tr></table>'


def _summary_items(brief: MonthlyIpoBrief) -> tuple[tuple[str, str, str], ...]:
    subscription = _brief_fact(brief.schedule, "청약")
    offer_price = _brief_fact(brief.offer, "확정 공모가")
    final_position = _brief_fact(brief.demand, "확정가 위치")
    competition = _brief_fact(brief.demand, "기관 경쟁률")
    lockup = _brief_fact(brief.demand, "의무보유확약")
    offer_band, offer_band_meta = _offer_band_context(brief)
    top_value, top_meta = _combined_top_distribution_summary(brief.institutional_price_distribution)
    brokers = ", ".join(row.broker_name for row in brief.underwriters) or "미수집"
    return (
        ("일반청약", subscription.value, _fact_meta(subscription)),
        ("희망밴드", offer_band, offer_band_meta),
        ("확정 공모가", offer_price.value, _fact_meta(offer_price)),
        ("확정가 위치", final_position.value, _fact_meta(final_position)),
        ("기관 경쟁률", competition.value, _fact_meta(competition)),
        ("의무보유확약", lockup.value, _fact_meta(lockup)),
        ("최다 가격제시", top_value, top_meta),
        ("주관사", brokers, ""),
    )


def _brief_fact(facts: tuple[EvidenceFact, ...], label: str) -> EvidenceFact:
    return next((fact for fact in facts if fact.label == label), _missing_fact(label, "missing"))


def _offer_band_context(brief: MonthlyIpoBrief) -> tuple[str, str]:
    derived = _derived_distribution_offer_band(brief.institutional_price_distribution)
    fact = _brief_fact(brief.offer, "공모가 밴드")
    distribution_meta = _distribution_meta(brief.institutional_price_distribution)
    if derived and fact.available and fact.value != derived:
        return (
            "출처 충돌 — 확인 필요",
            f"분포 라벨 {derived} · 공모정보 {fact.value} · {distribution_meta}",
        )
    if derived:
        return derived, f"기관 분포의 하단·상단 라벨 기준 · {distribution_meta}"
    if fact.available:
        return fact.value, f"분포 라벨 기준 확인 불가 · {_fact_meta(fact)}"
    return "확인 불가", "희망밴드와 분포 하단·상단 라벨 미수집"


def _derived_distribution_offer_band(
    bands: tuple[InstitutionalPriceDistributionBand, ...],
) -> Optional[str]:
    edges: dict[str, set[int]] = {"하단": set(), "상단": set()}
    pattern = re.compile(r"^\s*([\d,]+)\s*원\s*\((하단|상단)\)\s*$")
    for band in bands:
        match = pattern.match(band.label)
        if match:
            edges[match.group(2)].add(int(match.group(1).replace(",", "")))
    if len(edges["하단"]) != 1 or len(edges["상단"]) != 1:
        return None
    low = next(iter(edges["하단"]))
    high = next(iter(edges["상단"]))
    return _price_band(low, high) if low <= high else None


def _top_distribution_bands(
    bands: tuple[InstitutionalPriceDistributionBand, ...],
) -> tuple[InstitutionalPriceDistributionBand, ...]:
    numeric = tuple(band for band in bands if band.participant_ratio_pct is not None)
    if not numeric:
        return ()
    maximum = max(float(band.participant_ratio_pct or 0) for band in numeric)
    if maximum <= 0:
        return ()
    return tuple(
        band
        for band in numeric
        if math.isclose(float(band.participant_ratio_pct or 0), maximum, abs_tol=1e-9)
    )


def _top_distribution_summary(
    bands: tuple[InstitutionalPriceDistributionBand, ...],
) -> tuple[str, str]:
    top = _top_distribution_bands(bands)
    if not top:
        if bands and any(band.participant_ratio_pct == 0 for band in bands):
            return "확인 불가(공시 비중이 모두 0%)", _distribution_meta(bands)
        return "분포 데이터 부족", _distribution_meta(bands)
    prefix = "참여기관 공동 최다" if len(top) > 1 else "참여기관 최다"
    values = " · ".join(
        f"{band.label} {_distribution_percent_label(band.participant_ratio_pct)} "
        f"({_distribution_count_label(band.participants_count)})"
        for band in top
    )
    return f"{prefix}: {values}", _distribution_meta(top)


def _top_requested_share_bands(
    bands: tuple[InstitutionalPriceDistributionBand, ...],
) -> tuple[InstitutionalPriceDistributionBand, ...]:
    numeric = tuple(band for band in bands if band.requested_shares_ratio_pct is not None)
    if not numeric:
        return ()
    maximum = max(float(band.requested_shares_ratio_pct or 0) for band in numeric)
    if maximum <= 0:
        return ()
    return tuple(
        band
        for band in numeric
        if math.isclose(float(band.requested_shares_ratio_pct or 0), maximum, abs_tol=1e-9)
    )


def _top_requested_shares_summary(
    bands: tuple[InstitutionalPriceDistributionBand, ...],
) -> tuple[str, str]:
    top = _top_requested_share_bands(bands)
    if not top:
        if bands and any(band.requested_shares_ratio_pct == 0 for band in bands):
            return "신청수량 최다 확인 불가(공시 비중이 모두 0%)", _distribution_meta(bands)
        return "신청수량 분포 데이터 부족", _distribution_meta(bands)
    prefix = "신청수량 공동 최다" if len(top) > 1 else "신청수량 최다"
    values = " · ".join(
        (
            f"{band.label} {_distribution_percent_label(band.requested_shares_ratio_pct)} "
            f"({band.requested_shares:,}주)"
            if band.requested_shares is not None
            else f"{band.label} {_distribution_percent_label(band.requested_shares_ratio_pct)} "
            "(신청수량 미공시)"
        )
        for band in top
    )
    return f"{prefix}: {values}", _distribution_meta(top)


def _combined_top_distribution_summary(
    bands: tuple[InstitutionalPriceDistributionBand, ...],
) -> tuple[str, str]:
    participant_text, participant_meta = _top_distribution_summary(bands)
    shares_text, shares_meta = _top_requested_shares_summary(bands)
    meta = participant_meta if participant_meta == shares_meta else _distribution_meta(bands)
    return f"{participant_text} · {shares_text}", meta


def _distribution_percent_label(value: Optional[float]) -> str:
    return f"{value:.2f}%" if value is not None else "비중 미공시"


def _distribution_count_label(value: Optional[int]) -> str:
    return f"{value:,}건" if value is not None else "참여건수 미공시"


def _distribution_ratio_basis(
    bands: tuple[InstitutionalPriceDistributionBand, ...],
) -> str:
    bases = {band.participant_ratio_basis for band in bands}
    if "participants_count_recalculated" in bases:
        return "참여기관 건수 합계(가격미제시 포함)로 재계산한 비중"
    if "reported_ratio" in bases:
        return "참여기관 건수 미공시로 공시 비중 사용"
    return "참여기관 비중 확인 불가"


def _requested_shares_ratio_basis(
    bands: tuple[InstitutionalPriceDistributionBand, ...],
) -> str:
    bases = {band.requested_shares_ratio_basis for band in bands}
    if "requested_shares_recalculated" in bases:
        return "신청수량 합계(가격미제시 포함)로 재계산한 비중"
    if "reported_ratio" in bases:
        return "신청수량 미공시로 공시 비중 사용"
    return "신청수량 비중 확인 불가"


def _distribution_meta(
    bands: tuple[InstitutionalPriceDistributionBand, ...],
) -> str:
    sources = tuple(dict.fromkeys(band.source for band in bands if band.source))
    as_ofs = tuple(dict.fromkeys(band.as_of for band in bands if band.as_of))
    refs = tuple(dict.fromkeys(band.source_ref for band in bands if band.source_ref))
    parts = []
    if sources:
        parts.append("출처 " + ", ".join(sources))
    if as_ofs:
        parts.append("as-of " + ", ".join(as_ofs))
    if refs:
        parts.append("근거 " + ", ".join(refs))
    return " · ".join(parts) or "출처·기준시각 미확인"


def _price_distribution_text(brief: MonthlyIpoBrief) -> list[str]:
    bands = brief.institutional_price_distribution
    offer_band, _offer_band_meta = _offer_band_context(brief)
    lines = [
        "기관 가격제시 분포 차트:",
        (
            f"독립 0~100% 막대 · 참여기관: {_distribution_ratio_basis(bands)} · "
            f"신청수량: {_requested_shares_ratio_basis(bands)} · 희망밴드 {offer_band} · "
            f"확정 공모가 {_brief_fact(brief.offer, '확정 공모가').value} · "
            f"확정가 위치 {_brief_fact(brief.demand, '확정가 위치').value}"
        ),
    ]
    numeric = tuple(
        band
        for band in bands
        if band.participant_ratio_pct is not None or band.requested_shares_ratio_pct is not None
    )
    if not numeric:
        reason = (
            "참여기관·신청수량 비중 미공시로 차트를 생성하지 않음"
            if bands
            else "기관 가격분포 원문 미수집으로 차트를 생성하지 않음"
        )
        lines.extend((f"- {reason}", f"- {_distribution_meta(bands)}"))
        return lines
    top = _top_distribution_bands(bands)
    shares_top = _top_requested_share_bands(bands)
    for band in bands:
        ratio = band.participant_ratio_pct
        participant_bar = _plain_distribution_bar(ratio)
        shares_bar = _plain_distribution_bar(band.requested_shares_ratio_pct)
        participant_top_label = (
            " · 참여기관 공동 최다"
            if len(top) > 1 and band in top
            else " · 참여기관 최다"
            if band in top
            else ""
        )
        shares_top_label = (
            " · 신청수량 공동 최다"
            if len(shares_top) > 1 and band in shares_top
            else " · 신청수량 최다"
            if band in shares_top
            else ""
        )
        lines.append(
            f"- {band.label}{participant_top_label}{shares_top_label}\n"
            f"  참여기관 비중 [{participant_bar}] {_distribution_percent_label(ratio)} · "
            f"{_distribution_count_label(band.participants_count)}\n"
            f"  신청수량 비중 [{shares_bar}] "
            f"{_distribution_percent_label(band.requested_shares_ratio_pct)} · "
            f"{band.requested_shares:,}주"
            if band.requested_shares is not None
            else f"- {band.label}{participant_top_label}{shares_top_label}\n"
            f"  참여기관 비중 [{participant_bar}] {_distribution_percent_label(ratio)} · "
            f"{_distribution_count_label(band.participants_count)}\n"
            f"  신청수량 비중 [{shares_bar}] "
            f"{_distribution_percent_label(band.requested_shares_ratio_pct)} · 미공시"
        )
    top_summary, _top_meta = _combined_top_distribution_summary(bands)
    lines.extend((f"- {top_summary}", f"- {_distribution_meta(bands)}"))
    return lines


def _plain_distribution_bar(value: Optional[float]) -> str:
    units = round((value or 0) / 5)
    return "#" * units + "-" * (20 - units)


def _price_distribution_html(brief: MonthlyIpoBrief) -> str:
    bands = brief.institutional_price_distribution
    offer_band, _offer_band_meta = _offer_band_context(brief)
    subtitle = (
        f"독립 0~100% 막대 · 참여기관: {_distribution_ratio_basis(bands)} · "
        f"신청수량: {_requested_shares_ratio_basis(bands)} · 희망밴드 {offer_band} · "
        f"확정 공모가 {_brief_fact(brief.offer, '확정 공모가').value} · "
        f"확정가 위치 {_brief_fact(brief.demand, '확정가 위치').value}"
    )
    numeric = tuple(
        band
        for band in bands
        if band.participant_ratio_pct is not None or band.requested_shares_ratio_pct is not None
    )
    if not numeric:
        reason = (
            "참여기관·신청수량 비중이 공시되지 않아 차트를 생성하지 않았습니다."
            if bands
            else "기관 가격분포 원문이 수집되지 않아 차트를 생성하지 않았습니다."
        )
        return f'<div style="margin-top:14px;font-weight:800">기관 가격제시 분포</div><div style="margin-top:3px;color:#526477;font-size:11px">{escape(subtitle)}</div><div style="margin-top:7px;padding:10px;background:#FFF7E6;color:#805500;font-size:12px">{escape(reason)}<br>{escape(_distribution_meta(bands))}</div>'
    top = _top_distribution_bands(bands)
    shares_top = _top_requested_share_bands(bands)
    rows = "".join(_price_distribution_html_row(band, top, shares_top) for band in bands)
    top_summary, _top_meta = _combined_top_distribution_summary(bands)
    return f'<div style="margin-top:14px;font-weight:800">기관 가격제시 분포</div><div style="margin-top:3px;color:#526477;font-size:11px">{escape(subtitle)}</div><table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin-top:7px;border:1px solid #D9E2EC">{rows}</table><div style="margin-top:7px;font-size:12px;font-weight:700">{escape(top_summary)}</div><div style="margin-top:3px;color:#7B8794;font-size:10px">{escape(_distribution_meta(bands))}</div>'


def _price_distribution_html_row(
    band: InstitutionalPriceDistributionBand,
    top: tuple[InstitutionalPriceDistributionBand, ...],
    shares_top: tuple[InstitutionalPriceDistributionBand, ...],
) -> str:
    ratio = band.participant_ratio_pct
    is_top = band in top
    top_labels = []
    if is_top:
        top_labels.append("참여기관 공동 최다" if len(top) > 1 else "참여기관 최다")
    if band in shares_top:
        top_labels.append("신청수량 공동 최다" if len(shares_top) > 1 else "신청수량 최다")
    label_suffix = " · " + " · ".join(top_labels) if top_labels else ""
    participant_bar = _html_distribution_bar(
        ratio,
        "#1677C8" if is_top else "#7DB7E8",
    )
    is_shares_top = band in shares_top
    shares_bar = _html_distribution_bar(
        band.requested_shares_ratio_pct,
        "#526477" if is_shares_top else "#A8B6C5",
    )
    shares = f"{band.requested_shares:,}주" if band.requested_shares is not None else "미공시"
    return f'<tr><td style="padding:8px;border-bottom:1px solid #E8EDF2;width:34%;font-size:12px"><strong>{escape(band.label)}</strong>{escape(label_suffix)}</td><td style="padding:8px;border-bottom:1px solid #E8EDF2"><div style="font-size:10px;color:#526477">참여기관 비중 · <strong>{escape(_distribution_percent_label(ratio))}</strong> · {escape(_distribution_count_label(band.participants_count))}</div>{participant_bar}<div style="margin-top:6px;font-size:10px;color:#526477">신청수량 비중 · <strong>{escape(_distribution_percent_label(band.requested_shares_ratio_pct))}</strong> · {escape(shares)}</div>{shares_bar}</td></tr>'


def _html_distribution_bar(value: Optional[float], color: str) -> str:
    if value is not None and value > 0:
        return f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" bgcolor="#E8EDF2"><tr><td bgcolor="{color}" style="width:{value:.6f}%;height:10px;font-size:0;line-height:0">&nbsp;</td><td style="height:10px;font-size:0;line-height:0">&nbsp;</td></tr></table>'
    return '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" bgcolor="#E8EDF2"><tr><td style="height:10px;font-size:0;line-height:0">&nbsp;</td></tr></table>'


def _subscription_start_subject(brief: MonthlyIpoBrief) -> str:
    value = _brief_fact(brief.schedule, "청약").value
    match = re.search(r"(\d{4})[.-](\d{2})[.-](\d{2})", value)
    if not match:
        return "청약일 미확인"
    return f"{int(match.group(2))}월 {int(match.group(3))}일"


def _event_badge_label(value: str) -> str:
    return EVENT_BADGE_LABELS.get(value, value)


def _event_badges_text(values: tuple[str, ...]) -> str:
    return ", ".join(_event_badge_label(value) for value in values)


def _html_fact_section(title: str, facts: tuple[EvidenceFact, ...]) -> str:
    rows = "".join(
        f'<tr><td style="padding:6px 8px;color:#526477;width:34%">{escape(fact.label)}</td><td style="padding:6px 8px">{escape(fact.value)}<div style="font-size:11px;color:#7B8794">{escape(_fact_meta(fact))}</div></td></tr>'
        for fact in facts
    )
    return f'<div style="margin-top:14px;font-weight:800">{escape(title)}</div><table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin-top:4px;background:#F7F9FC">{rows}</table>'


def _html_underwriter(row: UnderwriterBrief) -> str:
    facts = "".join(
        f'<div style="margin-top:4px">{escape(_fact_text(fact))}</div>'
        for fact in _underwriter_facts(row)
    )
    return f'<div style="margin-top:8px;padding:10px;background:#F0F7FF"><strong>{escape(row.broker_name)}</strong><div style="font-size:11px;color:#526477">출처 {escape(row.source)} · as-of {escape(row.as_of)}</div>{facts}</div>'


def _underwriter_facts(row: UnderwriterBrief) -> tuple[EvidenceFact, ...]:
    return (
        row.role,
        row.public_offer_shares,
        row.retail_allocated_shares,
        row.actual_competition,
        row.actual_applicants,
        row.subscription_fee,
        row.account_open_deadline,
    )


def _fact_text(fact: EvidenceFact) -> str:
    return f"{fact.label}: {fact.value} [{_fact_meta(fact)}]"


def _fact_meta(fact: EvidenceFact) -> str:
    parts = [STATE_LABELS[fact.state]]
    if fact.source:
        parts.append(f"출처 {fact.source}")
    if fact.as_of:
        parts.append(f"as-of {fact.as_of}")
    if fact.source_ref:
        parts.append(f"근거 {fact.source_ref}")
    return " · ".join(parts)


def _brief_fact_value(facts: tuple[EvidenceFact, ...], label: str) -> str:
    return next(fact.value for fact in facts if fact.label == label)


def _localized(value: datetime) -> datetime:
    return value.astimezone(KST) if value.tzinfo else value.replace(tzinfo=KST)


def _format_as_of(value: object) -> str:
    if isinstance(value, datetime):
        localized = _localized(value)
        return f"{localized:%Y.%m.%d %H:%M} KST"
    if isinstance(value, date):
        return f"{value:%Y.%m.%d}"
    return "미확인"


def _date_text(value: Optional[date]) -> Optional[str]:
    return f"{value:%Y.%m.%d}" if value else None


def _date_range(start: Optional[date], end: Optional[date]) -> Optional[str]:
    if start and end and start != end:
        return f"{start:%Y.%m.%d}–{end:%Y.%m.%d}"
    value = start or end
    return _date_text(value)


def _price_band(low: Optional[int], high: Optional[int]) -> Optional[str]:
    if low is not None and high is not None and low != high:
        return f"{low:,.0f}–{high:,.0f}원"
    value = low if low is not None else high
    return _money(value)


def _money(value: object) -> Optional[str]:
    return f"{float(value):,.0f}원" if value is not None else None


def _million_won(value: Optional[int]) -> Optional[str]:
    return f"{value:,.0f}백만원" if value is not None else None


def _shares(value: Optional[int]) -> Optional[str]:
    return f"{value:,}주" if value is not None else None


def _count(value: Optional[int], unit: str) -> Optional[str]:
    return f"{value:,}{unit}" if value is not None else None


def _ratio(value: Optional[float]) -> Optional[str]:
    return f"{float(value):,.2f}:1" if value is not None else None


def _percent(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    normalized = float(value)
    if -1 <= normalized <= 1:
        normalized *= 100
    return f"{normalized:.2f}%"


def _signed_percent(value: Optional[float]) -> Optional[str]:
    return f"{float(value):+.2f}%" if value is not None else None


def _json_value(value: object) -> Optional[str]:
    if value in (None, {}, []):
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _brief_evidence_payload(brief: MonthlyIpoBrief) -> dict[str, object]:
    payload = asdict(brief)
    payload.pop("stage_as_of")
    return payload


def _historical_evidence_payload(
    history: HistoricalEvidenceAvailability,
) -> dict[str, object]:
    payload = asdict(history)
    payload.pop("as_of")
    return payload


def _stable_hash(value: object) -> str:
    payload = json.dumps(
        asdict(value) if hasattr(value, "__dataclass_fields__") else value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
