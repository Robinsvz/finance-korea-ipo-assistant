from __future__ import annotations

import io
import json
import re
import time
import zipfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any, Dict, Iterable, Optional
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from app.collectors.http import ResilientHttpClient
from app.config import get_settings, load_yaml_config
from app.exceptions import ExternalSourceError, MissingCredentialError
from app.services.clock import local_today
from app.services.normalization import normalize_broker_name, parse_float, parse_int

OPENDART_BASE = "https://opendart.fss.or.kr/api"
DART_IPO_PARSER_VERSION = "dart-ipo-evidence-v1"


class DartDocumentType(str, Enum):
    """The filing event represented by an OpenDART report name."""

    SECURITIES_REGISTRATION = "securities_registration"
    CORRECTION = "correction"
    INVESTMENT_PROSPECTUS = "investment_prospectus"
    TERMS_FINALIZATION = "terms_finalization"
    ISSUANCE_RESULT = "issuance_result"
    WITHDRAWAL = "withdrawal"
    UNKNOWN = "unknown"


class DartDocumentFamily(str, Enum):
    """The controlling document lineage, independent of correction markers."""

    SECURITIES_REGISTRATION = "securities_registration"
    INVESTMENT_PROSPECTUS = "investment_prospectus"
    ISSUANCE_RESULT = "issuance_result"
    WITHDRAWAL = "withdrawal"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RawEvidence:
    """One parser claim tied back to the exact OpenDART source excerpt."""

    field_name: str
    value: Any
    unit: str
    excerpt: str
    document_type: DartDocumentType
    receipt_no: Optional[str] = None
    published_at: Optional[datetime] = None
    parser_name: str = "dart-ipo-evidence"
    parser_version: str = DART_IPO_PARSER_VERSION
    method: str = "table"
    confidence: float = 0.95
    canonical_evidence_category: str = "reported_fact"

    def as_dict(self) -> dict[str, Any]:
        return _json_safe_value(asdict(self))


@dataclass(frozen=True)
class PriceDistributionEntry:
    label: str
    price_krw: Optional[int]
    participants_count: Optional[int]
    requested_shares: Optional[int]
    ratio: Optional[float]
    requested_shares_ratio: Optional[float] = None


@dataclass(frozen=True)
class InstitutionalDemandResult:
    competition_ratio: Optional[float]
    participants_count: Optional[int]
    lockup_commitment_ratio: Optional[float]
    offer_price_final: Optional[int]
    above_band_participation_ratio: Optional[float]
    price_distribution: tuple[PriceDistributionEntry, ...]
    evidence: tuple[RawEvidence, ...]


@dataclass(frozen=True)
class OfferingStructureResult:
    total_offered_shares: Optional[int]
    new_shares: Optional[int]
    secondary_shares: Optional[int]
    post_listing_shares: Optional[int]
    free_float_shares: Optional[int]
    free_float_ratio: Optional[float]
    evidence: tuple[RawEvidence, ...]


@dataclass(frozen=True)
class RetailBrokerResult:
    broker_name: str
    competition_ratio: Optional[float]
    applicants_count: Optional[int]
    subscribed_shares: Optional[int]
    allocated_shares: Optional[int]


@dataclass(frozen=True)
class RetailSubscriptionResult:
    competition_ratio: Optional[float]
    applicants_count: Optional[int]
    subscribed_shares: Optional[int]
    allocated_shares: Optional[int]
    brokers: tuple[RetailBrokerResult, ...]
    evidence: tuple[RawEvidence, ...]


@dataclass(frozen=True)
class DartDocumentParseResult:
    document_type: DartDocumentType
    document_family: DartDocumentFamily
    receipt_no: Optional[str]
    published_at: Optional[datetime]
    institutional_demand: Optional[InstitutionalDemandResult]
    offering_structure: Optional[OfferingStructureResult]
    retail_subscription: Optional[RetailSubscriptionResult]
    raw_evidence: tuple[RawEvidence, ...]
    warnings: tuple[str, ...]
    parser_version: str = DART_IPO_PARSER_VERSION

    @property
    def is_withdrawal(self) -> bool:
        return self.document_family is DartDocumentFamily.WITHDRAWAL

    def as_dict(self) -> dict[str, Any]:
        return _json_safe_dataclass(self)


def classify_dart_document(report_name: str) -> DartDocumentType:
    """Classify an IPO filing without losing correction/finalization semantics."""
    compact = re.sub(r"\s+", "", str(report_name or ""))
    family = classify_dart_document_family(compact)
    if family is DartDocumentFamily.WITHDRAWAL:
        return DartDocumentType.WITHDRAWAL
    if "발행조건확정" in compact:
        return DartDocumentType.TERMS_FINALIZATION
    if _has_correction_marker(compact):
        return DartDocumentType.CORRECTION
    if family is DartDocumentFamily.ISSUANCE_RESULT:
        return DartDocumentType.ISSUANCE_RESULT
    if family is DartDocumentFamily.INVESTMENT_PROSPECTUS:
        return DartDocumentType.INVESTMENT_PROSPECTUS
    if family is DartDocumentFamily.SECURITIES_REGISTRATION:
        return DartDocumentType.SECURITIES_REGISTRATION
    return DartDocumentType.UNKNOWN


def classify_dart_document_family(report_name: str) -> DartDocumentFamily:
    """Return the document family that corrections supersede."""
    compact = re.sub(r"\s+", "", str(report_name or ""))
    if re.search(r"철회|공모취소|상장취소", compact):
        return DartDocumentFamily.WITHDRAWAL
    if "증권발행실적보고서" in compact:
        return DartDocumentFamily.ISSUANCE_RESULT
    if "투자설명서" in compact:
        return DartDocumentFamily.INVESTMENT_PROSPECTUS
    if "증권신고서" in compact or "정정신고서" in compact or "발행조건확정" in compact:
        return DartDocumentFamily.SECURITIES_REGISTRATION
    return DartDocumentFamily.UNKNOWN


def _has_correction_marker(report_name: str) -> bool:
    compact = re.sub(r"\s+", "", report_name)
    return bool(re.search(r"(?:\[|\()?\s*(?:기재)?정정(?:\]|\))?", compact))


class DartCollector:
    def __init__(self, api_key: Optional[str] = None, timeout: Optional[float] = None) -> None:
        settings = get_settings()
        self.settings = settings
        self.api_key = api_key or settings.dart_api_key
        self.timeout = timeout or settings.http_timeout_sec
        if not self.api_key:
            raise MissingCredentialError("DART_API_KEY is required for OpenDART collection")
        source = (load_yaml_config("sources.yaml").get("sources") or {}).get("dart", {})
        self.request_interval_sec = float(source.get("request_interval_sec", 0))
        self.corp_code_cache_hours = float(source.get("corp_code_cache_hours", 24))
        self.corp_code_cache_path = settings.data_dir / "raw" / "dart_corp_codes.json"

    def _get_json(
        self,
        path: str,
        params: Dict[str, Any],
        client: Optional[ResilientHttpClient] = None,
    ) -> Dict[str, Any]:
        params = dict(params)
        params["crtfc_key"] = self.api_key
        if client is None:
            with ResilientHttpClient(
                timeout=self.timeout, min_interval_sec=self.request_interval_sec
            ) as owned_client:
                data = owned_client.get_json(
                    f"{OPENDART_BASE}/{path}", source="OpenDART", params=params
                )
        else:
            data = client.get_json(f"{OPENDART_BASE}/{path}", source="OpenDART", params=params)
        if data.get("status") not in {None, "000", "013"}:
            raise ExternalSourceError(f"OpenDART error {data.get('status')}: {data.get('message')}")
        return data

    def fetch_corp_codes(self) -> list[dict[str, str]]:
        cached = self._read_corp_code_cache()
        if cached is not None:
            return cached
        with ResilientHttpClient(
            timeout=self.timeout, min_interval_sec=self.request_interval_sec
        ) as client:
            response = client.request(
                "GET",
                f"{OPENDART_BASE}/corpCode.xml",
                source="OpenDART corp codes",
                params={"crtfc_key": self.api_key},
            )
        xml_bytes = _safe_zip_members(response.content, max_total_bytes=50_000_000)[0][1]
        root = ElementTree.fromstring(xml_bytes)
        rows = []
        for item in root.findall("list"):
            rows.append(
                {
                    "corp_code": _xml_text(item, "corp_code"),
                    "corp_name": _xml_text(item, "corp_name"),
                    "corp_eng_name": _xml_text(item, "corp_eng_name"),
                    "stock_code": _xml_text(item, "stock_code"),
                    "modify_date": _xml_text(item, "modify_date"),
                }
            )
        self.corp_code_cache_path.write_text(
            json.dumps(rows, ensure_ascii=False),
            encoding="utf-8",
        )
        return rows

    def _read_corp_code_cache(self) -> Optional[list[dict[str, str]]]:
        path = self.corp_code_cache_path
        if not path.exists():
            return None
        age_hours = (time.time() - path.stat().st_mtime) / 3600
        if age_hours > self.corp_code_cache_hours:
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, list) else None

    def find_corp_code(
        self,
        corp_name: str,
        corp_codes: Optional[Iterable[dict[str, str]]] = None,
        stock_code: Optional[str] = None,
    ) -> Optional[str]:
        row = self.find_corp(corp_name, corp_codes, stock_code=stock_code)
        return row.get("corp_code") if row else None

    def find_corp(
        self,
        corp_name: str,
        corp_codes: Optional[Iterable[dict[str, str]]] = None,
        stock_code: Optional[str] = None,
    ) -> Optional[dict[str, str]]:
        """Return the unambiguous OpenDART identity without discarding stock_code."""
        rows = list(corp_codes) if corp_codes is not None else self.fetch_corp_codes()
        target = re.sub(r"\s+", "", corp_name)
        if stock_code:
            stock_matches = [row for row in rows if row.get("stock_code") == stock_code]
            if len(stock_matches) == 1:
                return stock_matches[0]
        exact = [row for row in rows if re.sub(r"\s+", "", row.get("corp_name", "")) == target]
        if len(exact) == 1:
            return exact[0]
        fuzzy = [row for row in rows if target in re.sub(r"\s+", "", row.get("corp_name", ""))]
        if len(fuzzy) == 1:
            return fuzzy[0]
        return None

    def search_filings(
        self,
        corp_code: Optional[str] = None,
        bgn_de: Optional[date] = None,
        end_de: Optional[date] = None,
        pblntf_ty: str = "C",
    ) -> list[dict[str, Any]]:
        today = local_today()
        bgn_de = bgn_de or today - timedelta(days=365)
        end_de = end_de or today
        params = {
            "bgn_de": bgn_de.strftime("%Y%m%d"),
            "end_de": end_de.strftime("%Y%m%d"),
            "pblntf_ty": pblntf_ty,
            "page_count": "100",
        }
        if corp_code:
            params["corp_code"] = corp_code
        rows: list[dict[str, Any]] = []
        page_no = 1
        with ResilientHttpClient(
            timeout=self.timeout, min_interval_sec=self.request_interval_sec
        ) as client:
            while True:
                page_params = {**params, "page_no": str(page_no)}
                data = self._get_json("list.json", page_params, client=client)
                page_rows = data.get("list", []) or []
                rows.extend(page_rows)
                total_count = int(data.get("total_count") or len(rows))
                if not page_rows or len(rows) >= total_count:
                    break
                page_no += 1
                if page_no > 100:
                    raise ExternalSourceError("OpenDART filing pagination exceeded 100 pages")
        return rows

    def search_ipo_related_filings(self, corp_code: str) -> list[dict[str, Any]]:
        filings = self.search_filings(corp_code=corp_code, pblntf_ty="C")
        matched = [
            row
            for row in filings
            if classify_dart_document_family(str(row.get("report_nm") or ""))
            is not DartDocumentFamily.UNKNOWN
        ]
        return build_filing_lineage(matched)

    def fetch_document_text(self, receipt_no: str) -> str:
        with ResilientHttpClient(
            timeout=self.timeout, min_interval_sec=self.request_interval_sec
        ) as client:
            response = client.request(
                "GET",
                f"{OPENDART_BASE}/document.xml",
                source="OpenDART document",
                params={"crtfc_key": self.api_key, "rcept_no": receipt_no},
            )
        if response.content.startswith(b"PK"):
            chunks = [_bytes_to_text(data) for _name, data in _safe_zip_members(response.content)]
            text = "\n".join(chunks)
        else:
            text = response.text
        if "사용한도" in text or "정의되지 않은 오류" in text:
            raise ExternalSourceError("OpenDART document endpoint returned an error payload")
        soup = BeautifulSoup(text, "lxml")
        for row in soup.select("tr"):
            cells = [cell.get_text(" ", strip=True) for cell in row.select("th, td")]
            if cells:
                row.replace_with(" | ".join(cells) + "\n")
        return soup.get_text("\n", strip=True)


class DartFinancialCollector(DartCollector):
    def fetch_single_company_financials(
        self,
        corp_code: str,
        years: Iterable[int],
        reprt_code: str = "11011",
        fs_div: str = "CFS",
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for year in years:
            data = self._get_json(
                "fnlttSinglAcnt.json",
                {
                    "corp_code": corp_code,
                    "bsns_year": str(year),
                    "reprt_code": reprt_code,
                    "fs_div": fs_div,
                },
            )
            year_rows = data.get("list", []) or []
            used_div = fs_div
            if not year_rows and fs_div == "CFS":
                fallback = self._get_json(
                    "fnlttSinglAcnt.json",
                    {
                        "corp_code": corp_code,
                        "bsns_year": str(year),
                        "reprt_code": reprt_code,
                        "fs_div": "OFS",
                    },
                )
                year_rows = fallback.get("list", []) or []
                used_div = "OFS"
            for row in year_rows:
                row = dict(row)
                row["fiscal_year"] = int(year)
                row["fs_div"] = used_div
                rows.append(row)
        return rows


def extract_demand_forecast_fields(text: str) -> Dict[str, Any]:
    compact = re.sub(r"\s+", " ", text)
    table_values = _extract_table_values(text)
    ratio = table_values.get("institutional_competition_ratio", {}).get("value")
    ratio = (
        ratio
        if ratio is not None
        else _search_float(
            compact,
            [
                r"기관[^.]{0,80}?경쟁률[^0-9]{0,20}([0-9,]+(?:\.[0-9]+)?)\s*[:대]\s*1",
                r"수요예측[^.]{0,80}?경쟁률[^0-9]{0,20}([0-9,]+(?:\.[0-9]+)?)\s*[:대]\s*1",
            ],
        )
    )
    lockup = table_values.get("lockup_commitment_ratio", {}).get("value")
    lockup = (
        lockup
        if lockup is not None
        else _search_float(
            compact,
            [
                r"의무보유확약[^0-9]{0,30}([0-9,]+(?:\.[0-9]+)?)\s*%",
                r"확약[^0-9]{0,30}([0-9,]+(?:\.[0-9]+)?)\s*%",
            ],
        )
    )
    final_price = table_values.get("offer_price_final", {}).get("value")
    final_price = (
        final_price
        if final_price is not None
        else _search_int(
            compact,
            [
                r"확정\s*공모가[^0-9]{0,30}([0-9,]+)\s*원",
                r"공모가\s*확정[^0-9]{0,30}([0-9,]+)\s*원",
            ],
        )
    )
    participants = table_values.get("participants_count", {}).get("value")
    participants = (
        participants
        if participants is not None
        else _search_int(
            compact,
            [r"참여\s*기관[^0-9]{0,20}([0-9,]+)\s*개", r"기관\s*투자자[^0-9]{0,20}([0-9,]+)\s*개"],
        )
    )
    above_band = table_values.get("above_band_participation_ratio", {}).get("value")
    above_band = (
        above_band
        if above_band is not None
        else _search_float(
            compact,
            [r"밴드[^.]{0,80}?상단\s*초과[^0-9]{0,30}([0-9,]+(?:\.[0-9]+)?)\s*%"],
        )
    )
    evidence = {}
    units = {
        "institutional_competition_ratio": "ratio_to_one",
        "lockup_commitment_ratio": "percent",
        "offer_price_final": "KRW",
        "participants_count": "institutions",
        "above_band_participation_ratio": "percent",
    }
    for key, value in {
        "institutional_competition_ratio": ratio,
        "lockup_commitment_ratio": lockup,
        "offer_price_final": final_price,
        "participants_count": participants,
        "above_band_participation_ratio": above_band,
    }.items():
        if value is not None:
            table_evidence = table_values.get(key) or {}
            evidence[key] = {
                "value": value,
                "parser": "dart-tables-v1",
                "method": "table" if table_evidence else "text_fallback",
                "excerpt": table_evidence.get("excerpt") or _evidence_excerpt(compact, value),
                "unit": units[key],
                "confidence": 0.95 if table_evidence else 0.7,
            }
    return {
        "institutional_competition_ratio": ratio,
        "lockup_commitment_ratio": lockup,
        "offer_price_final": final_price,
        "participants_count": participants,
        "above_band_participation_ratio": above_band / 100
        if above_band and above_band > 1
        else above_band,
        "evidence": evidence,
        "parser_version": "dart-tables-v1",
    }


def parse_dart_document(
    text: str,
    report_name: str,
    *,
    receipt_no: Optional[str] = None,
    published_at: Optional[datetime] = None,
) -> DartDocumentParseResult:
    """Parse one preserved OpenDART document into typed, source-backed claims.

    This function is deliberately offline: callers fetch and retain the raw document first,
    then pass its text here. Unknown or ambiguous values remain absent instead of estimated.
    """
    text = _prepare_parser_text(text)
    document_type = classify_dart_document(report_name)
    family = classify_dart_document_family(report_name)
    demand: Optional[InstitutionalDemandResult] = None
    structure: Optional[OfferingStructureResult] = None
    retail: Optional[RetailSubscriptionResult] = None
    warnings: list[str] = []

    if family in {
        DartDocumentFamily.SECURITIES_REGISTRATION,
        DartDocumentFamily.INVESTMENT_PROSPECTUS,
    }:
        demand = _parse_institutional_demand(
            text,
            document_type,
            receipt_no=receipt_no,
            published_at=published_at,
        )
        structure = _parse_offering_structure(
            text,
            document_type,
            receipt_no=receipt_no,
            published_at=published_at,
        )
        warnings.extend(_structure_consistency_warnings(structure))
    elif family is DartDocumentFamily.ISSUANCE_RESULT:
        retail = _parse_retail_subscription(
            text,
            document_type,
            receipt_no=receipt_no,
            published_at=published_at,
        )

    evidence = tuple(
        item
        for result in (demand, structure, retail)
        if result is not None
        for item in result.evidence
    )
    if family is DartDocumentFamily.UNKNOWN:
        warnings.append("unknown_document_type")
    elif family is not DartDocumentFamily.WITHDRAWAL and not evidence:
        warnings.append("no_supported_ipo_fields")
    return DartDocumentParseResult(
        document_type=document_type,
        document_family=family,
        receipt_no=receipt_no,
        published_at=published_at,
        institutional_demand=demand,
        offering_structure=structure,
        retail_subscription=retail,
        raw_evidence=evidence,
        warnings=tuple(warnings),
    )


def _parse_institutional_demand(
    text: str,
    document_type: DartDocumentType,
    *,
    receipt_no: Optional[str],
    published_at: Optional[datetime],
) -> Optional[InstitutionalDemandResult]:
    compact = re.sub(r"\s+", " ", text).strip()
    values: dict[str, Any] = {}
    evidence: list[RawEvidence] = []

    summary = _extract_demand_summary(compact)
    if summary is not None:
        participants, requested_shares, competition, excerpt = summary
        values["participants_count"] = participants
        values["institutional_competition_ratio"] = competition
        for field_name, value, unit in (
            ("participants_count", participants, "institutions"),
            ("institutional_requested_shares", requested_shares, "shares"),
            ("institutional_competition_ratio", competition, "ratio_to_one"),
        ):
            evidence.append(
                _raw_evidence(
                    field_name,
                    value,
                    unit,
                    excerpt,
                    "summary_table_flat",
                    document_type,
                    receipt_no,
                    published_at,
                    confidence=0.98,
                )
            )
    else:
        direct_ratio = _extract_labeled_value(
            text,
            ("기관 수요예측 경쟁률", "수요예측 경쟁률"),
            r"([0-9,]+(?:\.[0-9]+)?)\s*[:대]\s*1",
            parse_float,
        )
        direct_participants = _extract_labeled_value(
            text,
            ("참여 기관 수", "참여기관 수", "참여 기관"),
            r"([0-9,]+)\s*개",
            parse_int,
        )
        for field_name, found, unit in (
            ("institutional_competition_ratio", direct_ratio, "ratio_to_one"),
            ("participants_count", direct_participants, "institutions"),
        ):
            if found is None:
                continue
            value, excerpt, method = found
            values[field_name] = value
            evidence.append(
                _raw_evidence(
                    field_name,
                    value,
                    unit,
                    excerpt,
                    method,
                    document_type,
                    receipt_no,
                    published_at,
                )
            )

    lockup = _extract_demand_lockup(compact, values.get("participants_count"))
    if lockup is not None:
        lockup_ratio, excerpt = lockup
        values["lockup_commitment_ratio"] = lockup_ratio
        evidence.append(
            _raw_evidence(
                "lockup_commitment_ratio",
                lockup_ratio,
                "ratio",
                excerpt,
                "derived_from_commitment_table",
                document_type,
                receipt_no,
                published_at,
                confidence=0.95,
                category="derived_reported_fact",
            )
        )
    else:
        direct_lockup = _extract_labeled_value(
            text,
            ("의무보유확약 비율", "의무보유확약 신청비율", "의무보유확약 수량비율"),
            r"([0-9,]+(?:\.[0-9]+)?)\s*%",
            parse_float,
        )
        if direct_lockup is not None:
            percent, excerpt, method = direct_lockup
            values["lockup_commitment_ratio"] = percent / 100
            evidence.append(
                _raw_evidence(
                    "lockup_commitment_ratio",
                    percent / 100,
                    "ratio",
                    excerpt,
                    method,
                    document_type,
                    receipt_no,
                    published_at,
                )
            )

    final_price = _extract_labeled_value(
        text,
        ("1주당 확정공모가액", "1주당 확정 공모가액", "확정 공모가", "확정공모가액"),
        r"([0-9,]+)\s*원",
        parse_int,
    )
    if final_price is not None:
        value, excerpt, method = final_price
        values["offer_price_final"] = value
        evidence.append(
            _raw_evidence(
                "offer_price_final",
                value,
                "KRW",
                excerpt,
                method,
                document_type,
                receipt_no,
                published_at,
            )
        )

    price_distribution, distribution_evidence = _extract_price_distribution(
        text,
        document_type,
        receipt_no=receipt_no,
        published_at=published_at,
    )
    evidence.extend(distribution_evidence)
    above_band_entries = [
        entry
        for entry in price_distribution
        if "초과" in entry.label and "미만" not in entry.label and entry.ratio is not None
    ]
    if len(above_band_entries) == 1:
        entry = above_band_entries[0]
        values["above_band_participation_ratio"] = entry.ratio
        source = next(item for item in distribution_evidence if item.value == asdict(entry))
        evidence.append(
            _raw_evidence(
                "above_band_participation_ratio",
                entry.ratio,
                "ratio",
                source.excerpt,
                "price_distribution_table",
                document_type,
                receipt_no,
                published_at,
            )
        )
    if not evidence:
        return None
    return InstitutionalDemandResult(
        competition_ratio=values.get("institutional_competition_ratio"),
        participants_count=values.get("participants_count"),
        lockup_commitment_ratio=values.get("lockup_commitment_ratio"),
        offer_price_final=values.get("offer_price_final"),
        above_band_participation_ratio=values.get("above_band_participation_ratio"),
        price_distribution=price_distribution,
        evidence=tuple(evidence),
    )


def _parse_offering_structure(
    text: str,
    document_type: DartDocumentType,
    *,
    receipt_no: Optional[str],
    published_at: Optional[datetime],
) -> Optional[OfferingStructureResult]:
    compact = re.sub(r"\s+", " ", text).strip()
    values: dict[str, Any] = {}
    evidence: list[RawEvidence] = []

    narrative = _extract_offering_narrative(compact)
    if narrative is not None:
        new_shares, secondary_shares, excerpt = narrative
        values["new_shares"] = new_shares
        for field_name, value in (
            ("new_shares", new_shares),
            ("secondary_shares", secondary_shares),
        ):
            if value is None:
                continue
            values[field_name] = value
            evidence.append(
                _raw_evidence(
                    field_name,
                    value,
                    "shares",
                    excerpt,
                    "offering_narrative",
                    document_type,
                    receipt_no,
                    published_at,
                    confidence=0.98,
                )
            )

    total_match = _extract_unique_match(
        compact,
        r"모집\s*또는\s*매출주식의\s*수\s+기명식\s*보통주\s+([0-9,]+)\s*주",
        parse_int,
    )
    if total_match is not None:
        total, excerpt = total_match
        values["total_offered_shares"] = total
        evidence.append(
            _raw_evidence(
                "total_offered_shares",
                total,
                "shares",
                excerpt,
                "terms_table_flat",
                document_type,
                receipt_no,
                published_at,
                confidence=0.98,
            )
        )

    post_listing = _extract_unique_match(
        compact,
        r"상장예정주식수\s+([0-9,]+)\s*주",
        parse_int,
    )
    if post_listing is not None:
        value, excerpt = post_listing
        values["post_listing_shares"] = value
        evidence.append(
            _raw_evidence(
                "post_listing_shares",
                value,
                "shares",
                excerpt,
                "listing_share_statement",
                document_type,
                receipt_no,
                published_at,
                confidence=0.97,
            )
        )

    free_float = _extract_free_float_statement(compact)
    if free_float is not None:
        free_shares, free_ratio, excerpt = free_float
        values["free_float_shares"] = free_shares
        evidence.append(
            _raw_evidence(
                "free_float_shares",
                free_shares,
                "shares",
                excerpt,
                "free_float_statement",
                document_type,
                receipt_no,
                published_at,
                confidence=0.98,
            )
        )
        if free_ratio is not None:
            values["free_float_ratio"] = free_ratio
            evidence.append(
                _raw_evidence(
                    "free_float_ratio",
                    free_ratio,
                    "ratio",
                    excerpt,
                    "free_float_statement",
                    document_type,
                    receipt_no,
                    published_at,
                    confidence=0.98,
                )
            )

    if "total_offered_shares" not in values and {
        "new_shares",
        "secondary_shares",
    }.issubset(values):
        values["total_offered_shares"] = values["new_shares"] + values["secondary_shares"]
        evidence.append(
            _derived_evidence(
                "total_offered_shares",
                values["total_offered_shares"],
                "shares",
                evidence,
                document_type,
                receipt_no,
                published_at,
            )
        )
    if (
        "free_float_ratio" not in values
        and values.get("post_listing_shares")
        and values.get("free_float_shares") is not None
    ):
        values["free_float_ratio"] = values["free_float_shares"] / values["post_listing_shares"]
        evidence.append(
            _derived_evidence(
                "free_float_ratio",
                values["free_float_ratio"],
                "ratio",
                evidence,
                document_type,
                receipt_no,
                published_at,
            )
        )
    if not evidence:
        return None
    return OfferingStructureResult(
        total_offered_shares=values.get("total_offered_shares"),
        new_shares=values.get("new_shares"),
        secondary_shares=values.get("secondary_shares"),
        post_listing_shares=values.get("post_listing_shares"),
        free_float_shares=values.get("free_float_shares"),
        free_float_ratio=values.get("free_float_ratio"),
        evidence=tuple(evidence),
    )


def _parse_retail_subscription(
    text: str,
    document_type: DartDocumentType,
    *,
    receipt_no: Optional[str],
    published_at: Optional[datetime],
) -> Optional[RetailSubscriptionResult]:
    values: dict[str, Any] = {}
    evidence: list[RawEvidence] = []
    aggregate = _extract_retail_aggregate(re.sub(r"\s+", " ", text).strip())
    if aggregate is not None:
        applicants, subscribed, allocated, competition, excerpt = aggregate
        values.update(
            retail_applicants_count=applicants,
            retail_subscribed_shares=subscribed,
            retail_allocated_shares=allocated,
            retail_competition_ratio=competition,
        )
        for field_name, value, unit in (
            ("retail_applicants_count", applicants, "applicants"),
            ("retail_subscribed_shares", subscribed, "shares"),
            ("retail_allocated_shares", allocated, "shares"),
        ):
            evidence.append(
                _raw_evidence(
                    field_name,
                    value,
                    unit,
                    excerpt,
                    "issuance_result_table_flat",
                    document_type,
                    receipt_no,
                    published_at,
                    confidence=0.99,
                )
            )
        evidence.append(
            _raw_evidence(
                "retail_competition_ratio",
                competition,
                "ratio_to_one",
                excerpt,
                "derived_from_issuance_result_table",
                document_type,
                receipt_no,
                published_at,
                confidence=0.98,
                category="derived_reported_fact",
            )
        )
    else:
        definitions = (
            (
                "retail_competition_ratio",
                ("일반청약 경쟁률", "일반투자자 경쟁률"),
                r"([0-9,]+(?:\.[0-9]+)?)\s*[:대]\s*1",
                parse_float,
                "ratio_to_one",
            ),
            (
                "retail_applicants_count",
                ("일반청약자수", "일반청약건수"),
                r"([0-9,]+)\s*(?:명|건)",
                parse_int,
                "applicants",
            ),
            (
                "retail_subscribed_shares",
                ("일반청약 신청주식수",),
                r"([0-9,]+)\s*주",
                parse_int,
                "shares",
            ),
            (
                "retail_allocated_shares",
                ("일반투자자 배정주식수", "일반청약 배정주식수"),
                r"([0-9,]+)\s*주",
                parse_int,
                "shares",
            ),
        )
        for field_name, labels, pattern, parser, unit in definitions:
            found = _extract_labeled_value(text, labels, pattern, parser)
            if found is None:
                continue
            value, excerpt, method = found
            values[field_name] = value
            evidence.append(
                _raw_evidence(
                    field_name,
                    value,
                    unit,
                    excerpt,
                    method,
                    document_type,
                    receipt_no,
                    published_at,
                )
            )
    brokers, broker_evidence = _extract_retail_brokers(
        text,
        document_type,
        aggregate=aggregate,
        receipt_no=receipt_no,
        published_at=published_at,
    )
    evidence.extend(broker_evidence)
    if not evidence:
        return None
    return RetailSubscriptionResult(
        competition_ratio=values.get("retail_competition_ratio"),
        applicants_count=values.get("retail_applicants_count"),
        subscribed_shares=values.get("retail_subscribed_shares"),
        allocated_shares=values.get("retail_allocated_shares"),
        brokers=brokers,
        evidence=tuple(evidence),
    )


def _prepare_parser_text(text: str) -> str:
    """Preserve HTML table row boundaries while accepting already flattened text."""
    if not re.search(r"<\s*(?:html|body|table|tr)\b", text, flags=re.IGNORECASE):
        return text
    soup = BeautifulSoup(text, "lxml")
    for row in soup.select("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in row.select("th, td")]
        if cells:
            row.replace_with(" | ".join(cells) + "\n")
    return soup.get_text("\n", strip=True)


def _extract_demand_summary(
    compact: str,
) -> Optional[tuple[int, int, float, str]]:
    pattern = re.compile(
        r"수요예측\s*참여\s*내역.{0,800}?\s건수\s+(?P<counts>.{1,500}?)\s+"
        r"수량\s+(?P<shares>.{1,500}?)\s+경쟁률\s*(?:\(주\s*2\))?\s+"
        r"(?P<ratios>.{1,500}?)(?=\s+주\s*1\)|\s+주\s*2\)|\s+\(나\))"
    )
    candidates: list[tuple[int, int, float, str]] = []
    for match in pattern.finditer(compact):
        counts = _numeric_tokens(match.group("counts"))
        shares = _numeric_tokens(match.group("shares"))
        ratios = _numeric_tokens(match.group("ratios"))
        if counts and shares and ratios:
            participants = int(counts[-1].replace(",", ""))
            requested = int(shares[-1].replace(",", ""))
            competition = float(ratios[-1].replace(",", ""))
            if participants > 0 and requested >= participants and competition > 0:
                candidates.append((participants, requested, competition, match.group(0)[:1000]))
    unique = {(item[0], item[1], item[2]) for item in candidates}
    return candidates[-1] if len(unique) == 1 else None


def _extract_demand_lockup(
    compact: str,
    participants_count: Optional[int],
) -> Optional[tuple[float, str]]:
    compact = re.sub(r"\s*\|\s*", " ", compact)
    candidates: list[tuple[float, str]] = []
    pattern = re.compile(r"구분\s+외국\s+기관투자자\s+합계(?P<body>.{1,4000}?)(?=\s+\(라\))")
    for match in pattern.finditer(compact):
        body = match.group("body")
        if "건수 수량 신청가격" in body and "6개월 확약" in body:
            row_match = re.search(
                r"미확약\s+(?P<uncommitted>.{1,800}?)\s+(?:계|합계)\s+"
                r"(?P<total>.{1,500}?)(?=\s+주\s*1\)|$)",
                body,
            )
            if row_match is not None:
                uncommitted = _numeric_tokens(row_match.group("uncommitted"))
                total = _numeric_tokens(row_match.group("total"))
                if len(uncommitted) >= 3 and len(total) >= 3:
                    uncommitted_shares = int(uncommitted[-2].replace(",", ""))
                    total_count = int(total[-3].replace(",", ""))
                    total_shares = int(total[-2].replace(",", ""))
                    count_matches = participants_count is None or total_count == participants_count
                    if (
                        total_shares > 0
                        and 0 <= uncommitted_shares <= total_shares
                        and count_matches
                    ):
                        ratio = (total_shares - uncommitted_shares) / total_shares
                        excerpt = (
                            f"미확약 {row_match.group('uncommitted')} 계 {row_match.group('total')}"
                        )
                        candidates.append((ratio, excerpt[:1000]))
    unique = {round(item[0], 12) for item in candidates}
    return candidates[-1] if len(unique) == 1 else None


def _extract_offering_narrative(
    compact: str,
) -> Optional[tuple[int, Optional[int], str]]:
    pattern = re.compile(
        r"상장공모는\s+신주모집\s+(?P<new>[0-9,]+)\s*주?(?P<tail>.{0,240}?)"
        r"(?:일반공모\s*방식|방식에\s*의합니다)"
    )
    candidates: list[tuple[int, Optional[int], str]] = []
    for match in pattern.finditer(compact):
        new_shares = parse_int(match.group("new"))
        if new_shares is None or new_shares <= 0:
            continue
        tail = match.group("tail")
        secondary_match = re.search(r"구주매출\s+([0-9,]+)\s*주", tail)
        if secondary_match is not None:
            secondary = parse_int(secondary_match.group(1))
        elif re.search(r"공모주식의\s*100(?:\.0+)?%|구주매출.{0,20}(?:없|해당사항\s*없)", tail):
            secondary = 0
        else:
            secondary = None
        candidates.append((new_shares, secondary, match.group(0)[:1000]))
    unique = {(item[0], item[1]) for item in candidates}
    return candidates[-1] if len(unique) == 1 else None


def _extract_unique_match(text: str, pattern: str, parser) -> Optional[tuple[Any, str]]:
    candidates: list[tuple[Any, str]] = []
    for match in re.finditer(pattern, text):
        value = parser(match.group(1))
        if value is not None:
            candidates.append((value, match.group(0)[:500]))
    return candidates[-1] if candidates and len({item[0] for item in candidates}) == 1 else None


def _extract_free_float_statement(compact: str) -> Optional[tuple[int, Optional[float], str]]:
    prefix = re.compile(
        r"의무보유(?:예수)?\s*수량을\s*제외한\s*주식수\s+([0-9,]+)\s*주"
        r"(?:\s*\(([0-9,]+(?:\.[0-9]+)?)%\))?"
    )
    candidates: list[tuple[int, Optional[float], str, int]] = []
    for match in prefix.finditer(compact):
        window = compact[match.start() : match.end() + 350]
        if not re.search(r"상장\s*직후.{0,100}?유통가능", window):
            continue
        shares = parse_int(match.group(1))
        if shares is None or shares <= 0:
            continue
        ratio_text = match.group(2)
        if ratio_text is None:
            ratio_match = re.search(
                r"상장예정주식수\s*기준으로\s*([0-9,]+(?:\.[0-9]+)?)%",
                window,
            )
            ratio_text = ratio_match.group(1) if ratio_match else None
        ratio = parse_float(ratio_text) / 100 if ratio_text is not None else None
        precision = len(ratio_text.rsplit(".", 1)[1]) if ratio_text and "." in ratio_text else 0
        candidates.append((shares, ratio, window[:1000], precision))
    if not candidates or len({item[0] for item in candidates}) != 1:
        return None
    ratios = [item[1] for item in candidates if item[1] is not None]
    if ratios and max(ratios) - min(ratios) > 0.001:
        return None
    chosen = max(candidates, key=lambda item: item[3])
    return chosen[0], chosen[1], chosen[2]


def _extract_retail_aggregate(
    compact: str,
) -> Optional[tuple[int, int, int, float, str]]:
    compact = re.sub(r"\s*\|\s*", " ", compact)
    header = re.search(
        r"구\s*분\s+최초\s*배정\s+청약\s*현황\s+최종\s*배정\s*현황",
        compact,
    )
    if header is None:
        return None
    body = compact[header.end() : header.end() + 2500]
    token = r"(?:-|[0-9][0-9,]*(?:\.[0-9]+)?)"
    row_pattern = re.compile(rf"일반(?:투자자|청약자)\s+(?P<values>{token}(?:\s+{token}){{9}})")
    candidates: list[tuple[int, int, int, float, str]] = []
    for match in row_pattern.finditer(body):
        tokens = match.group("values").split()
        initial_allocated = parse_int(tokens[0])
        applicants = parse_int(tokens[2])
        subscribed = parse_int(tokens[3])
        allocated = parse_int(tokens[7])
        if (
            initial_allocated is None
            or applicants is None
            or subscribed is None
            or allocated is None
            or min(initial_allocated, applicants, subscribed, allocated) <= 0
        ):
            continue
        competition = subscribed / initial_allocated
        candidates.append((applicants, subscribed, allocated, competition, match.group(0)[:1000]))
    unique = {(item[0], item[1], item[2]) for item in candidates}
    return candidates[-1] if len(unique) == 1 else None


def _numeric_tokens(text: str) -> list[str]:
    return re.findall(r"(?<![가-힣A-Za-z])\d[\d,]*(?:\.\d+)?", text)


def _extract_price_distribution(
    text: str,
    document_type: DartDocumentType,
    *,
    receipt_no: Optional[str],
    published_at: Optional[datetime],
) -> tuple[tuple[PriceDistributionEntry, ...], list[RawEvidence]]:
    entries: list[PriceDistributionEntry] = []
    evidence: list[RawEvidence] = []
    compact = re.sub(r"\s+", " ", text).strip()
    compact = re.sub(r"\s*\|\s*", " ", compact)
    marker = re.search(
        r"구분\s+참여건수\s*기준\s+신청수량\s*기준\s+참여건수\s*\(건\)\s+"
        r"비율\s+신청수량\s*\(주\)\s+비율",
        compact,
    )
    if marker is None:
        return (), []
    end = re.search(r"\s+합계\s+[0-9,]+\s+100(?:\.0)?%", compact[marker.end() :])
    body = compact[marker.end() : marker.end() + end.start()] if end else compact[marker.end() :]
    label = (
        r"(?:가격\s*미제시|[0-9,]+원\s+초과\s*(?:~|～)?\s*[0-9,]+원\s+미만|"
        r"[0-9,]+원\s*\([^)]{1,20}\)|[0-9,]+원\s+(?:초과|미만))"
    )
    number_or_dash = r"(?:-|[0-9][0-9,]*(?:\.[0-9]+)?)"
    row_pattern = re.compile(
        rf"(?P<label>{label})\s+(?P<count>{number_or_dash})\s+"
        rf"(?P<count_ratio>{number_or_dash})(?:\s*%)?\s+"
        rf"(?P<shares>{number_or_dash})\s+"
        rf"(?P<share_ratio>{number_or_dash})(?:\s*%)?"
    )
    for match in row_pattern.finditer(body):
        line = match.group(0)
        count_token = match.group("count")
        shares_token = match.group("shares")
        count_ratio_token = match.group("count_ratio")
        share_ratio_token = match.group("share_ratio")
        # In the official price-distribution table a dash is the explicit zero
        # representation for a price bucket, not an unknown value. Preserve that
        # distinction so a complete six-row distribution can be charted while an
        # actually absent or malformed field still remains ``None``.
        participants = 0 if count_token == "-" else parse_int(count_token)
        shares = 0 if shares_token == "-" else parse_int(shares_token)
        ratio_percent = 0.0 if count_ratio_token == "-" else parse_float(count_ratio_token)
        share_ratio_percent = 0.0 if share_ratio_token == "-" else parse_float(share_ratio_token)
        price = _search_int(match.group("label"), [r"([0-9,]+)\s*원"])
        entry = PriceDistributionEntry(
            label=re.sub(r"\s+", " ", match.group("label")).strip(),
            price_krw=price,
            participants_count=participants,
            requested_shares=shares,
            ratio=ratio_percent / 100 if ratio_percent is not None else None,
            requested_shares_ratio=(
                share_ratio_percent / 100 if share_ratio_percent is not None else None
            ),
        )
        entries.append(entry)
        evidence.append(
            RawEvidence(
                field_name=f"institutional_price_distribution.{len(entries) - 1}",
                value=asdict(entry),
                unit="mixed",
                excerpt=line[:500],
                document_type=document_type,
                receipt_no=receipt_no,
                published_at=published_at,
                method="price_distribution_table",
                confidence=0.98,
            )
        )
    return tuple(entries), evidence


def _extract_retail_brokers(
    text: str,
    document_type: DartDocumentType,
    *,
    aggregate: Optional[tuple[int, int, int, float, str]] = None,
    receipt_no: Optional[str],
    published_at: Optional[datetime],
) -> tuple[tuple[RetailBrokerResult, ...], list[RawEvidence]]:
    brokers: list[RetailBrokerResult] = []
    evidence: list[RawEvidence] = []
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if "|" not in line:
            continue
        cells = [cell.strip() for cell in line.split("|") if cell.strip()]
        if not cells or "증권" not in cells[0]:
            continue
        broker_name = normalize_broker_name(cells[0])
        competition = _search_float(
            line,
            [r"경쟁률[^0-9]{0,20}([0-9,]+(?:\.[0-9]+)?)\s*[:대]\s*1"],
        )
        applicants = _search_int(line, [r"(?:청약자|청약건수)[^0-9]{0,20}([0-9,]+)\s*(?:명|건)"])
        subscribed = _search_int(line, [r"(?:신청|청약)주식수[^0-9]{0,20}([0-9,]+)\s*주"])
        allocated = _search_int(line, [r"배정주식수[^0-9]{0,20}([0-9,]+)\s*주"])
        if all(value is None for value in (competition, applicants, subscribed, allocated)):
            continue
        result = RetailBrokerResult(
            broker_name=broker_name,
            competition_ratio=competition,
            applicants_count=applicants,
            subscribed_shares=subscribed,
            allocated_shares=allocated,
        )
        brokers.append(result)
        evidence.append(
            RawEvidence(
                field_name=f"retail_brokers.{broker_name}",
                value=asdict(result),
                unit="mixed",
                excerpt=line[:500],
                document_type=document_type,
                receipt_no=receipt_no,
                published_at=published_at,
            )
        )
    seen = {item.broker_name for item in brokers}
    sections = list(
        re.finditer(
            r"(?m)^[ \t]*\[(?P<broker>[^\]\n]*증권[^\]\n]*)\][ \t]*$",
            text,
        )
    )
    for index, match in enumerate(sections):
        broker_name = normalize_broker_name(match.group("broker"))
        if broker_name in seen:
            continue
        end = sections[index + 1].start() if index + 1 < len(sections) else len(text)
        section = text[match.end() : end]
        summary = re.search(r"(?m)^[ \t]*합계\s*\|\s*(?P<values>[^\n]+)$", section)
        if summary is not None:
            cells = [cell.strip() for cell in summary.group("values").split("|")]
            applicants = parse_int(cells[0]) if cells else None
            subscribed = parse_int(cells[1]) if len(cells) > 1 else None
            allocated = parse_int(cells[-2]) if len(cells) > 2 else None
            excerpt = f"[{match.group('broker')}] 합계 | {summary.group('values')}"
        elif len(sections) == 1 and aggregate is not None:
            applicants, subscribed, allocated, _competition, excerpt = aggregate
            excerpt = f"[{match.group('broker')}] {excerpt}"
        else:
            applicants = subscribed = allocated = None
            excerpt = f"[{match.group('broker')}] 합계 없음"
        if (
            applicants is None
            or subscribed is None
            or allocated is None
            or min(applicants, subscribed, allocated) <= 0
        ):
            continue
        result = RetailBrokerResult(
            broker_name=broker_name,
            competition_ratio=subscribed / allocated,
            applicants_count=applicants,
            subscribed_shares=subscribed,
            allocated_shares=allocated,
        )
        brokers.append(result)
        seen.add(broker_name)
        evidence.append(
            RawEvidence(
                field_name=f"retail_brokers.{broker_name}",
                value=asdict(result),
                unit="mixed",
                excerpt=excerpt[:1000],
                document_type=document_type,
                receipt_no=receipt_no,
                published_at=published_at,
                method="broker_allocation_summary",
                confidence=0.98,
                canonical_evidence_category="derived_reported_fact",
            )
        )
    return tuple(brokers), evidence


def _extract_labeled_value(
    text: str,
    labels: Iterable[str],
    pattern: str,
    parser,
) -> Optional[tuple[Any, str, str]]:
    normalized_lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    for line in normalized_lines:
        for label in labels:
            match = re.search(rf"{re.escape(label)}.{{0,120}}?{pattern}", line)
            if match:
                value = parser(match.group(1))
                if value is not None:
                    excerpt = line[max(match.start() - 80, 0) : match.end() + 80]
                    return value, excerpt[:500], "table" if "|" in line else "text"
    compact = re.sub(r"\s+", " ", text)
    for label in labels:
        match = re.search(rf"{re.escape(label)}.{{0,120}}?{pattern}", compact)
        if match:
            value = parser(match.group(1))
            if value is not None:
                excerpt = compact[max(match.start() - 80, 0) : match.end() + 80]
                return value, excerpt[:500], "text_fallback"
    return None


def _raw_evidence(
    field_name: str,
    value: Any,
    unit: str,
    excerpt: str,
    method: str,
    document_type: DartDocumentType,
    receipt_no: Optional[str],
    published_at: Optional[datetime],
    *,
    confidence: Optional[float] = None,
    category: str = "reported_fact",
) -> RawEvidence:
    if confidence is None:
        confidence = 0.95 if "table" in method else 0.75
    return RawEvidence(
        field_name=field_name,
        value=value,
        unit=unit,
        excerpt=excerpt,
        document_type=document_type,
        receipt_no=receipt_no,
        published_at=published_at,
        method=method,
        confidence=confidence,
        canonical_evidence_category=category,
    )


def _derived_evidence(
    field_name: str,
    value: Any,
    unit: str,
    inputs: Iterable[RawEvidence],
    document_type: DartDocumentType,
    receipt_no: Optional[str],
    published_at: Optional[datetime],
) -> RawEvidence:
    excerpt = " | ".join(item.excerpt for item in inputs if item.excerpt)[:500]
    return RawEvidence(
        field_name=field_name,
        value=value,
        unit=unit,
        excerpt=excerpt,
        document_type=document_type,
        receipt_no=receipt_no,
        published_at=published_at,
        method="derived",
        confidence=0.8,
        canonical_evidence_category="derived_reported_fact",
    )


def _structure_consistency_warnings(
    structure: Optional[OfferingStructureResult],
) -> tuple[str, ...]:
    if structure is None:
        return ()
    warnings: list[str] = []
    if (
        structure.total_offered_shares is not None
        and structure.new_shares is not None
        and structure.secondary_shares is not None
        and structure.total_offered_shares != structure.new_shares + structure.secondary_shares
    ):
        warnings.append("offering_share_total_mismatch")
    if (
        structure.free_float_ratio is not None
        and structure.free_float_shares is not None
        and structure.post_listing_shares
        and abs(
            structure.free_float_ratio - structure.free_float_shares / structure.post_listing_shares
        )
        > 0.01
    ):
        warnings.append("free_float_ratio_mismatch")
    return tuple(warnings)


def _json_safe_dataclass(value: Any) -> Any:
    return _json_safe_value(asdict(value))


def _json_safe_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_value(item) for item in value]
    return value


def summarize_financial_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_year: dict[int, dict[str, Any]] = {}
    for row in rows:
        fiscal_year = int(row.get("fiscal_year") or row.get("bsns_year"))
        bucket = by_year.setdefault(fiscal_year, {"fiscal_year": fiscal_year})
        account = str(row.get("account_nm", ""))
        account_id = str(row.get("account_id", ""))
        amount = parse_float(row.get("thstrm_amount"))
        if amount is None:
            continue
        if (
            account_id in {"ifrs-full_Revenue", "ifrs_Revenue"}
            or "매출" in account
            or "영업수익" in account
        ):
            bucket.setdefault("revenue", amount)
        elif account == "영업이익" or "영업이익" in account:
            bucket.setdefault("operating_profit", amount)
        elif "당기순이익" in account or "분기순이익" in account:
            bucket.setdefault("net_income", amount)
        elif "자산총계" in account:
            bucket.setdefault("total_assets", amount)
        elif "부채총계" in account:
            bucket.setdefault("total_liabilities", amount)
        elif "자본총계" in account:
            bucket.setdefault("total_equity", amount)
        elif "영업활동" in account and "현금흐름" in account:
            bucket.setdefault("operating_cash_flow", amount)

    years = sorted(by_year)
    previous_revenue: Optional[float] = None
    result = []
    for year in years:
        item = by_year[year]
        revenue = item.get("revenue")
        operating_profit = item.get("operating_profit")
        net_income = item.get("net_income")
        total_liabilities = item.get("total_liabilities")
        total_equity = item.get("total_equity")
        total_assets = item.get("total_assets")
        ocf = item.get("operating_cash_flow")
        item["revenue_yoy_growth"] = (
            (revenue - previous_revenue) / abs(previous_revenue)
            if revenue is not None and previous_revenue not in {None, 0}
            else None
        )
        if previous_revenue == 0 and revenue is not None:
            item["revenue_yoy_status"] = "prior_zero"
        elif previous_revenue is None:
            item["revenue_yoy_status"] = "missing_prior"
        elif revenue is None:
            item["revenue_yoy_status"] = "missing_current"
        else:
            item["revenue_yoy_status"] = "calculated"
        item["operating_margin"] = (
            operating_profit / revenue
            if revenue not in {None, 0} and operating_profit is not None
            else None
        )
        item["net_margin"] = (
            net_income / revenue if revenue not in {None, 0} and net_income is not None else None
        )
        item["debt_ratio"] = (
            total_liabilities / total_equity
            if total_equity not in {None, 0} and total_liabilities is not None
            else None
        )
        item["roe"] = (
            net_income / total_equity
            if total_equity not in {None, 0} and net_income is not None
            else None
        )
        item["roa"] = (
            net_income / total_assets
            if total_assets not in {None, 0} and net_income is not None
            else None
        )
        item["is_profitable"] = net_income is not None and net_income > 0
        item["has_negative_ocf"] = ocf is not None and ocf < 0
        if revenue is not None:
            previous_revenue = revenue
        result.append(item)
    return result


def _xml_text(item, tag: str) -> str:
    child = item.find(tag)
    return child.text if child is not None and child.text is not None else ""


def _bytes_to_text(data: bytes) -> str:
    for encoding in ("utf-8", "cp949", "euc-kr"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def _safe_zip_members(
    content: bytes,
    *,
    max_members: int = 100,
    max_total_bytes: int = 100_000_000,
) -> list[tuple[str, bytes]]:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > max_members:
                raise ExternalSourceError("OpenDART ZIP member count is invalid")
            if sum(info.file_size for info in infos) > max_total_bytes:
                raise ExternalSourceError("OpenDART ZIP is too large")
            members = [(info.filename, archive.read(info)) for info in infos if not info.is_dir()]
            if not members:
                raise ExternalSourceError("OpenDART ZIP contains no files")
            return members
    except zipfile.BadZipFile as exc:
        raise ExternalSourceError("OpenDART returned an invalid ZIP archive") from exc


def build_filing_lineage(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for raw in rows:
        row = dict(raw)
        report_name = str(row.get("report_nm") or "")
        document_type = classify_dart_document(report_name)
        document_family = classify_dart_document_family(report_name)
        base_name = re.sub(r"[\[(]?(?:기재)?정정[\])]?", "", report_name)
        base_name = re.sub(
            r"[\[(]?발행조건확정[\])]?",
            "",
            base_name.replace("\u200b", ""),
        )
        base_name = re.sub(r"\s+", "", base_name)
        if not base_name:
            base_name = document_family.value
        row["document_type"] = document_type.value
        row["document_family"] = document_family.value
        grouped.setdefault(base_name, []).append(row)

    enriched: list[dict[str, Any]] = []
    for base_name, lineage_rows in grouped.items():
        ordered = sorted(
            lineage_rows,
            key=lambda row: (str(row.get("rcept_dt") or ""), str(row.get("rcept_no") or "")),
        )
        receipt_nos = [str(row.get("rcept_no") or "") for row in ordered]
        for index, row in enumerate(ordered):
            row["filing_lineage"] = {
                "base_report_name": base_name,
                "document_family": row["document_family"],
                "lineage_key": base_name,
                "receipt_nos": receipt_nos,
                "sequence": index + 1,
                "is_final": index == len(ordered) - 1,
                "is_correction": row["document_type"]
                in {
                    DartDocumentType.CORRECTION.value,
                    DartDocumentType.TERMS_FINALIZATION.value,
                },
                "supersedes_receipt_no": receipt_nos[index - 1] if index else None,
            }
            enriched.append(row)
    return sorted(
        enriched,
        key=lambda row: (str(row.get("rcept_dt") or ""), str(row.get("rcept_no") or "")),
        reverse=True,
    )


def _search_float(text: str, patterns: Iterable[str]) -> Optional[float]:
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return parse_float(match.group(1))
    return None


def _search_int(text: str, patterns: Iterable[str]) -> Optional[int]:
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return parse_int(match.group(1))
    return None


def _evidence_excerpt(text: str, value: Any, radius: int = 80) -> str:
    variants = {str(value), f"{value:,}" if isinstance(value, int) else str(value)}
    positions = [text.find(variant) for variant in variants if text.find(variant) >= 0]
    if not positions:
        return ""
    start = max(min(positions) - radius, 0)
    end = min(min(positions) + radius, len(text))
    return text[start:end]


def _extract_table_values(text: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if "|" not in line:
            continue
        definitions = (
            (
                "institutional_competition_ratio",
                ("경쟁률",),
                r"([0-9,]+(?:\.[0-9]+)?)\s*[:대]\s*1",
                parse_float,
            ),
            (
                "lockup_commitment_ratio",
                ("의무보유확약", "확약"),
                r"([0-9,]+(?:\.[0-9]+)?)\s*%",
                parse_float,
            ),
            (
                "offer_price_final",
                ("확정 공모가", "공모가 확정"),
                r"([0-9,]+)\s*원",
                parse_int,
            ),
            (
                "participants_count",
                ("참여 기관", "기관 투자자"),
                r"([0-9,]+)\s*개",
                parse_int,
            ),
            (
                "above_band_participation_ratio",
                ("상단 초과",),
                r"([0-9,]+(?:\.[0-9]+)?)\s*%",
                parse_float,
            ),
        )
        for key, labels, pattern, parser in definitions:
            if key in result or not any(label in line for label in labels):
                continue
            match = re.search(pattern, line)
            if match:
                result[key] = {"value": parser(match.group(1)), "excerpt": line[:500]}
    return result
