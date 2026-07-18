from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Iterable, Optional, Tuple

from bs4 import BeautifulSoup

from app.collectors.http import ResilientHttpClient
from app.config import get_settings, load_yaml_config
from app.exceptions import ExternalSourceError
from app.schemas import IpoScheduleRecord
from app.services.clock import local_today
from app.services.normalization import parse_date, parse_int

KIND_PUBOFR_URL = "https://kind.krx.co.kr/listinvstg/pubofrprogcom.do"


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
