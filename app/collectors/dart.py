from __future__ import annotations

import io
import json
import re
import time
import zipfile
from datetime import date, timedelta
from typing import Any, Dict, Iterable, Optional
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from app.collectors.http import ResilientHttpClient
from app.config import get_settings, load_yaml_config
from app.exceptions import ExternalSourceError, MissingCredentialError
from app.services.clock import local_today
from app.services.normalization import parse_float, parse_int

OPENDART_BASE = "https://opendart.fss.or.kr/api"


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
        rows = list(corp_codes) if corp_codes is not None else self.fetch_corp_codes()
        target = re.sub(r"\s+", "", corp_name)
        if stock_code:
            stock_matches = [row for row in rows if row.get("stock_code") == stock_code]
            if len(stock_matches) == 1:
                return stock_matches[0].get("corp_code")
        exact = [row for row in rows if re.sub(r"\s+", "", row.get("corp_name", "")) == target]
        if len(exact) == 1:
            return exact[0].get("corp_code")
        fuzzy = [row for row in rows if target in re.sub(r"\s+", "", row.get("corp_name", ""))]
        if len(fuzzy) == 1:
            return fuzzy[0].get("corp_code")
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
        keywords = ("증권신고서", "투자설명서", "발행조건확정", "정정")
        matched = [
            row
            for row in filings
            if any(keyword in row.get("report_nm", "") for keyword in keywords)
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
        base_name = re.sub(r"\[?정정\]?", "", report_name)
        base_name = re.sub(r"\s+", "", base_name)
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
                "receipt_nos": receipt_nos,
                "sequence": index + 1,
                "is_final": index == len(ordered) - 1,
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
