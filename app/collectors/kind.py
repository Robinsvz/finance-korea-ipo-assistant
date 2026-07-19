from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Optional, Sequence, Tuple

from bs4 import BeautifulSoup

from app.collectors.http import ResilientHttpClient
from app.config import get_settings, load_yaml_config
from app.exceptions import ExternalSourceError
from app.schemas import IpoScheduleRecord
from app.services.clock import local_today
from app.services.normalization import parse_date, parse_int

KIND_PUBOFR_URL = "https://kind.krx.co.kr/listinvstg/pubofrprogcom.do"
KIND_LISTING_URL = "https://kind.krx.co.kr/listinvstg/listingcompany.do"

_KIND_LISTING_DEFAULT_COLUMNS = ("02", "03", "05")
_KIND_LISTING_OPTIONAL_COLUMNS = (
    ("01", "액면가"),
    ("02", "공모가"),
    ("03", "공모금액"),
    ("04", "주요제품"),
    ("05", "최초상장주식수"),
)
_KIND_LISTING_BASE_HEADERS = (
    ("회사명",),
    ("상장일",),
    ("상장유형",),
    ("증권구분",),
    ("업종",),
    ("국적",),
    ("상장주선인", "지정자문인"),
)
_KIND_LISTING_ERROR_MARKERS = (
    "페이지 오류",
    "잠시 후 다시 이용해 주세요",
)
_KIND_LISTING_EMPTY_MARKERS = (
    "조회된 결과값이 없습니다",
    "검색된 자료가 없습니다",
)


@dataclass(frozen=True)
class KindListingRecord:
    """One official KIND new-listing row as it appeared at collection time."""

    corp_name: str
    listing_date: date
    listing_type: Optional[str]
    security_type: Optional[str]
    market: Optional[str]
    underwriter: Optional[str]
    offer_price: Optional[int]
    offer_amount_thousand: Optional[int]
    initial_shares: Optional[int]
    isur_cd: str
    bz_procs_no: str
    raw: dict[str, object]


class KindScheduleCollector:
    """Collect every page of the KIND public offering schedule."""

    def __init__(self, timeout: Optional[float] = None) -> None:
        self.settings = get_settings()
        self.timeout = timeout or self.settings.http_timeout_sec
        source = (load_yaml_config("sources.yaml").get("sources") or {}).get("kind", {})
        self.request_interval_sec = float(source.get("request_interval_sec", 0))

    def fetch_page(
        self,
        *,
        start: date,
        end: date,
        page_size: int,
        page_index: int,
        client: ResilientHttpClient,
    ) -> str:
        if page_size <= 0 or page_index <= 0:
            raise ValueError("page_size and page_index must be positive")
        params = {
            "method": "searchPubofrProgComSub",
            "forward": "pubofrprogcom_sub",
            "currentPageSize": str(page_size),
            "pageIndex": str(page_index),
            "fromDate": start.isoformat(),
            "toDate": end.isoformat(),
            "marketType": "",
            "orderMode": "1",
            "orderStat": "D",
        }
        response = client.request("GET", KIND_PUBOFR_URL, source="KIND", params=params)
        if "페이지 오류" in response.text:
            raise ExternalSourceError("KIND returned an error page")
        return response.text

    def fetch(
        self,
        start: Optional[date] = None,
        end: Optional[date] = None,
        page_size: int = 100,
    ) -> str:
        """Backward-compatible first-page fetch used by diagnostics."""
        today = local_today()
        start = start or today - timedelta(days=self.settings.kind_lookback_days)
        end = end or today + timedelta(days=self.settings.kind_lookahead_days)
        with self._client() as client:
            return self.fetch_page(
                start=start,
                end=end,
                page_size=page_size,
                page_index=1,
                client=client,
            )

    def collect_with_raw(
        self,
        start: Optional[date] = None,
        end: Optional[date] = None,
        page_size: int = 100,
        max_pages: int = 100,
    ) -> tuple[list[IpoScheduleRecord], str]:
        today = local_today()
        start = start or today - timedelta(days=self.settings.kind_lookback_days)
        end = end or today + timedelta(days=self.settings.kind_lookahead_days)
        if start > end:
            raise ValueError("KIND start date must not be after end date")
        records: list[IpoScheduleRecord] = []
        raw_pages: list[str] = []
        with self._client() as client:
            for page_index in range(1, max_pages + 1):
                html = self.fetch_page(
                    start=start,
                    end=end,
                    page_size=page_size,
                    page_index=page_index,
                    client=client,
                )
                page_records = parse_kind_puboffering_html(html, allow_empty=page_index > 1)
                raw_pages.append(html)
                if not page_records:
                    break
                records.extend(page_records)
                if len(page_records) < page_size:
                    break
            else:
                raise ExternalSourceError(f"KIND pagination exceeded {max_pages} pages")
        deduped: dict[tuple[str, Optional[date], Optional[str]], IpoScheduleRecord] = {}
        for record in records:
            key = (record.corp_name, record.subscription_start_date, record.kind_biz_no)
            deduped[key] = record
        return list(deduped.values()), "\n<!-- KIND_PAGE -->\n".join(raw_pages)

    def collect(
        self,
        start: Optional[date] = None,
        end: Optional[date] = None,
        page_size: int = 100,
    ) -> list[IpoScheduleRecord]:
        return self.collect_with_raw(start=start, end=end, page_size=page_size)[0]

    def _client(self) -> ResilientHttpClient:
        headers = {
            "User-Agent": "Mozilla/5.0 korea-ipo-telegram-bot/0.2",
            "Referer": "https://kind.krx.co.kr/listinvstg/pubofrprogcom.do?method=searchPubofrProgComMain",
        }
        return ResilientHttpClient(
            timeout=self.timeout,
            headers=headers,
            min_interval_sec=self.request_interval_sec,
        )


def parse_kind_puboffering_html(
    html: str,
    *,
    allow_empty: bool = False,
) -> list[IpoScheduleRecord]:
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one("table")
    if table is None:
        raise ExternalSourceError("KIND response does not contain a schedule table")
    rows = table.select("tbody tr")
    if not rows:
        if allow_empty:
            return []
        raise ExternalSourceError("KIND schedule table is unexpectedly empty")
    _validate_headers(table)
    records = []
    malformed = 0
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 9:
            malformed += 1
            continue
        onclick = row.get("onclick") or ""
        biz_no_match = re.search(r"fnDetailView\('([^']+)'\)", onclick)
        biz_no = biz_no_match.group(1) if biz_no_match else None
        name_cell = cells[0]
        corp_name = (name_cell.get("title") or name_cell.get_text(" ", strip=True)).strip()
        if not corp_name:
            malformed += 1
            continue
        market_img = name_cell.find("img")
        demand_start, demand_end = _parse_date_range(cells[2].get_text(" ", strip=True))
        sub_start, sub_end = _parse_date_range(cells[3].get_text(" ", strip=True))
        records.append(
            IpoScheduleRecord(
                corp_name=corp_name,
                market=market_img.get("alt") if market_img else None,
                kind_biz_no=biz_no,
                report_filed_date=parse_date(cells[1].get_text(" ", strip=True)),
                demand_start_date=demand_start,
                demand_end_date=demand_end,
                subscription_start_date=sub_start,
                subscription_end_date=sub_end,
                payment_date=parse_date(cells[4].get_text(" ", strip=True)),
                offer_price_final=parse_int(cells[5].get_text(" ", strip=True)),
                offer_amount_million=parse_int(cells[6].get_text(" ", strip=True)),
                listing_date=parse_date(cells[7].get_text(" ", strip=True)),
                underwriters=_parse_underwriters(cells[8].get_text(" ", strip=True)),
                raw={
                    "kind_biz_no": biz_no,
                    "cells": [cell.get_text(" ", strip=True) for cell in cells],
                },
            )
        )
    if not records and malformed:
        raise ExternalSourceError("KIND table schema changed: no valid rows could be parsed")
    return records


def _parse_date_range(text: str) -> Tuple[Optional[date], Optional[date]]:
    dates = re.findall(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}", text)
    if not dates:
        return None, None
    if len(dates) == 1:
        parsed = parse_date(dates[0])
        return parsed, parsed
    return parse_date(dates[0]), parse_date(dates[1])


def _parse_underwriters(text: str) -> list[str]:
    normalized = text.replace("/", ",")
    parts = [part.strip() for part in normalized.split(",")]
    return [part for part in parts if part and part != "-"]


def _validate_headers(table) -> None:
    headers = [
        re.sub(r"\s+", "", cell.get_text(" ", strip=True)) for cell in table.select("thead th")
    ]
    if not headers:
        return
    expected = (
        ("기업", "회사"),
        ("신고", "제출"),
        ("수요예측",),
        ("청약",),
        ("납입",),
        ("공모가격", "공모가"),
        ("공모금액",),
        ("상장",),
        ("주간", "주관"),
    )
    if len(headers) < len(expected) or any(
        not any(alias in headers[index] for alias in aliases)
        for index, aliases in enumerate(expected)
    ):
        raise ExternalSourceError("KIND table schema changed: unexpected column headers")


def filter_records_for_week(
    records: Iterable[IpoScheduleRecord],
    start: date,
    end: date,
) -> list[IpoScheduleRecord]:
    return [
        record
        for record in records
        if (record.subscription_start_date and start <= record.subscription_start_date <= end)
        or (record.listing_date and start <= record.listing_date <= end)
    ]


class KindListingCollector:
    """Collect every page of the official KIND new-listing result."""

    def __init__(self, timeout: Optional[float] = None) -> None:
        self.settings = get_settings()
        self.timeout = timeout or self.settings.http_timeout_sec
        source = (load_yaml_config("sources.yaml").get("sources") or {}).get("kind", {})
        self.request_interval_sec = float(source.get("request_interval_sec", 0))

    def fetch_page(
        self,
        *,
        start: date,
        end: date,
        page_size: int,
        page_index: int,
        client: ResilientHttpClient,
        selected_columns: Sequence[str] = _KIND_LISTING_DEFAULT_COLUMNS,
    ) -> str:
        if start > end:
            raise ValueError("KIND listing start date must not be after end date")
        if page_size <= 0 or page_index <= 0:
            raise ValueError("page_size and page_index must be positive")
        columns = _normalize_listing_columns(selected_columns)
        data = {
            "method": "searchListingTypeSub",
            "forward": "listingtype_sub",
            "currentPageSize": str(page_size),
            "pageIndex": str(page_index),
            "orderMode": "1",
            "orderStat": "D",
            "repIsuSrtCd": "",
            "isurCd": "",
            "listTypeArrStr": "01|",
            "choicTypeArrStr": _listing_column_form_value(columns),
            "searchCodeType": "",
            "searchCorpName": "",
            "secuGrpArrStr": "0|ST|FS|MF|SC|RT|IF|DR|",
            "marketType": "",
            "searchCorpNameTmp": "",
            "country": "",
            "industry": "",
            "repMajAgntDesignAdvserComp": "",
            "repMajAgntComp": "",
            "designAdvserComp": "",
            "fromDate": start.isoformat(),
            "toDate": end.isoformat(),
        }
        response = client.request(
            "POST",
            KIND_LISTING_URL,
            source="KIND listing",
            data=data,
        )
        _raise_for_listing_error(response.text)
        return response.text

    def collect_with_raw(
        self,
        start: date,
        end: date,
        page_size: int = 100,
        max_pages: int = 100,
        selected_columns: Sequence[str] = _KIND_LISTING_DEFAULT_COLUMNS,
    ) -> tuple[list[KindListingRecord], str]:
        if start > end:
            raise ValueError("KIND listing start date must not be after end date")
        if page_size <= 0 or max_pages <= 0:
            raise ValueError("page_size and max_pages must be positive")
        columns = _normalize_listing_columns(selected_columns)
        records: list[KindListingRecord] = []
        raw_pages: list[str] = []
        with self._listing_client() as client:
            for page_index in range(1, max_pages + 1):
                html = self.fetch_page(
                    start=start,
                    end=end,
                    page_size=page_size,
                    page_index=page_index,
                    client=client,
                    selected_columns=columns,
                )
                page_records = parse_kind_listing_html(
                    html,
                    selected_columns=columns,
                    allow_empty=True,
                )
                raw_pages.append(html)
                if not page_records:
                    break
                records.extend(page_records)
                if len(page_records) < page_size:
                    break
            else:
                raise ExternalSourceError(f"KIND listing pagination exceeded {max_pages} pages")

        deduped: dict[tuple[str, str], KindListingRecord] = {}
        for record in records:
            deduped.setdefault((record.isur_cd, record.bz_procs_no), record)
        return (
            list(deduped.values()),
            "\n<!-- KIND_LISTING_PAGE -->\n".join(raw_pages),
        )

    def collect(
        self,
        start: date,
        end: date,
        page_size: int = 100,
        max_pages: int = 100,
        selected_columns: Sequence[str] = _KIND_LISTING_DEFAULT_COLUMNS,
    ) -> list[KindListingRecord]:
        return self.collect_with_raw(
            start=start,
            end=end,
            page_size=page_size,
            max_pages=max_pages,
            selected_columns=selected_columns,
        )[0]

    def _listing_client(self) -> ResilientHttpClient:
        headers = {
            "User-Agent": "Mozilla/5.0 korea-ipo-telegram-bot/0.2",
            "Referer": (
                "https://kind.krx.co.kr/listinvstg/listingcompany.do?method=searchListingTypeMain"
            ),
            "X-Requested-With": "XMLHttpRequest",
        }
        return ResilientHttpClient(
            timeout=self.timeout,
            headers=headers,
            min_interval_sec=self.request_interval_sec,
        )


def parse_kind_listing_html(
    html: str,
    *,
    selected_columns: Sequence[str] = _KIND_LISTING_DEFAULT_COLUMNS,
    allow_empty: bool = False,
) -> list[KindListingRecord]:
    """Parse a KIND listing fragment and fail closed on a changed table schema."""

    _raise_for_listing_error(html)
    columns = _normalize_listing_columns(selected_columns)
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one("table.list")
    if table is None:
        raise ExternalSourceError("KIND response does not contain a new-listing table")
    _validate_listing_headers(html, table, columns)

    rows = table.select("tbody tr")
    if not rows:
        if allow_empty:
            return []
        raise ExternalSourceError("KIND new-listing table is unexpectedly empty")
    if len(rows) == 1 and _is_empty_listing_row(rows[0]):
        if allow_empty:
            return []
        raise ExternalSourceError("KIND new-listing table is unexpectedly empty")

    expected_cell_count = len(_KIND_LISTING_BASE_HEADERS) + len(columns)
    records: list[KindListingRecord] = []
    malformed = 0
    for row in rows:
        cells = row.find_all("td")
        if len(cells) != expected_cell_count:
            malformed += 1
            continue

        name_cell = cells[0]
        corp_name = (name_cell.get("title") or name_cell.get_text(" ", strip=True)).strip()
        if not corp_name:
            malformed += 1
            continue
        listing_date = parse_date(cells[1].get_text(" ", strip=True))
        if listing_date is None:
            malformed += 1
            continue
        identifiers = _parse_listing_identifiers(row.get("onclick") or "")
        if identifiers is None:
            malformed += 1
            continue

        optional_cells = dict(zip(columns, cells[len(_KIND_LISTING_BASE_HEADERS) :]))
        market_img = name_cell.find("img")
        isur_cd, bz_procs_no = identifiers
        records.append(
            KindListingRecord(
                corp_name=corp_name,
                listing_date=listing_date,
                listing_type=_none_if_blank(cells[2].get_text(" ", strip=True)),
                security_type=_none_if_blank(cells[3].get_text(" ", strip=True)),
                market=_none_if_blank(market_img.get("alt") if market_img else None),
                underwriter=_none_if_blank(cells[6].get_text(" ", strip=True)),
                offer_price=_listing_optional_int(optional_cells, "02"),
                offer_amount_thousand=_listing_optional_int(optional_cells, "03"),
                initial_shares=_listing_optional_int(optional_cells, "05"),
                isur_cd=isur_cd,
                bz_procs_no=bz_procs_no,
                raw={
                    "isur_cd": isur_cd,
                    "bz_procs_no": bz_procs_no,
                    "industry": _none_if_blank(cells[4].get_text(" ", strip=True)),
                    "country": _none_if_blank(cells[5].get_text(" ", strip=True)),
                    "selected_columns": list(columns),
                    "cells": [cell.get_text(" ", strip=True) for cell in cells],
                },
            )
        )
    if not records and malformed:
        raise ExternalSourceError("KIND new-listing table schema changed: no valid rows")
    return records


def _normalize_listing_columns(selected_columns: Sequence[str]) -> tuple[str, ...]:
    supported = {column for column, _header in _KIND_LISTING_OPTIONAL_COLUMNS}
    requested = set(selected_columns)
    unknown = requested - supported
    if unknown:
        raise ValueError(f"unsupported KIND listing columns: {sorted(unknown)}")
    return tuple(
        column for column, _header in _KIND_LISTING_OPTIONAL_COLUMNS if column in requested
    )


def _listing_column_form_value(columns: Sequence[str]) -> str:
    if not columns:
        return ""
    return "|".join(columns) + "|"


def _raise_for_listing_error(html: str) -> None:
    if any(marker in html for marker in _KIND_LISTING_ERROR_MARKERS):
        raise ExternalSourceError("KIND returned a new-listing error page")


def _validate_listing_headers(html: str, table, columns: Sequence[str]) -> None:
    dynamic_match = re.search(
        r"fn_InitTitle\(\s*(['\"])(.*?)\1",
        html,
        flags=re.DOTALL,
    )
    if dynamic_match:
        headers = dynamic_match.group(2).split(",")
    else:
        headers = [cell.get_text(" ", strip=True) for cell in table.select("thead th")]
    normalized = [_normalize_listing_header(header) for header in headers if header.strip()]
    expected = list(_KIND_LISTING_BASE_HEADERS)
    optional_labels = dict(_KIND_LISTING_OPTIONAL_COLUMNS)
    expected.extend((optional_labels[column],) for column in columns)
    if len(normalized) != len(expected) or any(
        not all(token in normalized[index] for token in tokens)
        for index, tokens in enumerate(expected)
    ):
        raise ExternalSourceError("KIND new-listing table has unexpected column headers")


def _normalize_listing_header(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", "", value)
    return re.sub(r"\s+", "", without_tags)


def _is_empty_listing_row(row) -> bool:
    text = row.get_text(" ", strip=True)
    return any(marker in text for marker in _KIND_LISTING_EMPTY_MARKERS)


def _parse_listing_identifiers(value: str) -> Optional[tuple[str, str]]:
    match = re.search(
        r"fnDetailView\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)",
        value,
    )
    if match is None:
        return None
    return match.group(1).strip(), match.group(2).strip()


def _none_if_blank(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized or normalized == "-":
        return None
    return normalized


def _listing_optional_int(cells: dict[str, object], column: str) -> Optional[int]:
    cell = cells.get(column)
    if cell is None:
        return None
    return parse_int(cell.get_text(" ", strip=True))
