from __future__ import annotations

import html as html_module
import re
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional

import httpx
from bs4 import BeautifulSoup

from app.collectors.http import ResilientHttpClient
from app.exceptions import ExternalSourceError

DART_PUBLIC_BASE = "https://dart.fss.or.kr"
DART_PUBLIC_USER_AGENT = "Mozilla/5.0 (compatible; KoreaIpoAssistant/1.0; +https://dart.fss.or.kr)"
IPO_REPORT_KEYWORDS = (
    "증권신고서(지분증권)",
    "투자설명서",
    "발행조건확정",
    "증권발행실적보고서",
    "철회신고서",
)
_NODE_BLOCK_RE = re.compile(
    r"var\s+node\d+\s*=\s*\{\};(?P<body>.*?)(?=var\s+node\d+\s*=\s*\{\};|</script>|$)",
    re.DOTALL,
)
_NODE_VALUE_RE = re.compile(r"node\d+\['(?P<key>[^']+)'\]\s*=\s*\"(?P<value>(?:\\.|[^\"])*)\";")


@dataclass(frozen=True)
class PublicDartFiling:
    corp_name: str
    corp_code: Optional[str]
    receipt_no: str
    report_name: str
    presenter: Optional[str]
    submitted_on: Optional[date]

    @property
    def url(self) -> str:
        return f"{DART_PUBLIC_BASE}/dsaf001/main.do?rcpNo={self.receipt_no}"


@dataclass(frozen=True)
class PublicDartSection:
    receipt_no: str
    document_no: str
    element_id: str
    title: str
    offset: int
    length: int
    dtd: str


@dataclass(frozen=True)
class DartHtmlTable:
    caption: Optional[str]
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]

    @property
    def searchable_text(self) -> str:
        return " ".join(
            value
            for value in (self.caption, *self.headers, *(cell for row in self.rows for cell in row))
            if value
        )


class DartPublicCollector:
    """Read public DART search/viewer pages without an OpenDART API credential.

    This is an official-web fallback. It preserves the public receipt number and
    viewer section coordinates so a stored fact can be traced back to the exact
    DART document revision. It must not be used to bypass rate limits or terms.
    """

    def __init__(
        self,
        *,
        timeout: Optional[float] = None,
        min_interval_sec: float = 0.2,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self.timeout = timeout
        self.min_interval_sec = min_interval_sec
        self.transport = transport

    def http_client(self) -> ResilientHttpClient:
        return ResilientHttpClient(
            timeout=self.timeout,
            min_interval_sec=self.min_interval_sec,
            headers={"User-Agent": DART_PUBLIC_USER_AGENT},
            transport=self.transport,
        )

    def search_filings(
        self,
        company_name: str,
        *,
        start: date,
        end: date,
        report_keywords: Iterable[str] = IPO_REPORT_KEYWORDS,
        final_only: bool = False,
    ) -> tuple[PublicDartFiling, ...]:
        name = company_name.strip()
        if not name:
            raise ValueError("company_name is required")
        if end < start:
            raise ValueError("end must be on or after start")
        form = {
            "currentPage": "1",
            "maxResults": "100",
            "maxLinks": "10",
            "textCrpNm": name,
            "textCrpNm2": name,
            "startDate": start.strftime("%Y%m%d"),
            "endDate": end.strftime("%Y%m%d"),
            "businessCode": "all",
            "corporationType": "all",
            "autoSearch": "N",
            "autoSearchCorp": "Y",
        }
        if final_only:
            form["finalReport"] = "recent"
        with self.http_client() as client:
            response = client.request(
                "POST",
                f"{DART_PUBLIC_BASE}/dsab007/detailSearch.ax",
                source="DART public disclosure search",
                data=form,
                headers={"Referer": f"{DART_PUBLIC_BASE}/dsab007/main.do"},
            )
        filings = parse_public_search_results(response.text)
        keywords = tuple(item.strip() for item in report_keywords if item.strip())
        filtered = tuple(
            filing
            for filing in filings
            if filing.corp_name == name
            and (not keywords or any(keyword in filing.report_name for keyword in keywords))
        )
        return tuple(
            sorted(
                filtered,
                key=lambda item: (item.submitted_on or date.min, item.receipt_no),
            )
        )

    def document_tree(self, receipt_no: str) -> tuple[PublicDartSection, ...]:
        _validate_receipt_no(receipt_no)
        with self.http_client() as client:
            response = client.request(
                "GET",
                f"{DART_PUBLIC_BASE}/dsaf001/main.do",
                source="DART public document tree",
                params={"rcpNo": receipt_no},
            )
        sections = parse_document_tree(response.text, receipt_no)
        if not sections:
            raise ExternalSourceError("DART public viewer returned no document sections")
        return sections

    def fetch_section(self, section: PublicDartSection) -> str:
        _validate_receipt_no(section.receipt_no)
        with self.http_client() as client:
            response = client.request(
                "GET",
                f"{DART_PUBLIC_BASE}/report/viewer.do",
                source="DART public document section",
                params={
                    "rcpNo": section.receipt_no,
                    "dcmNo": section.document_no,
                    "eleId": section.element_id,
                    "offset": str(section.offset),
                    "length": str(section.length),
                    "dtd": section.dtd,
                },
                headers={"Referer": f"{DART_PUBLIC_BASE}/dsaf001/main.do"},
            )
        if response.url.host not in {"dart.fss.or.kr", "www.dart.fss.or.kr"}:
            raise ExternalSourceError("DART public viewer redirected to an unexpected host")
        if "<html" not in response.text.lower() and "<table" not in response.text.lower():
            raise ExternalSourceError("DART public viewer returned an invalid section")
        return response.text

    def fetch_matching_sections(
        self,
        receipt_no: str,
        *title_keywords: str,
    ) -> tuple[tuple[PublicDartSection, str], ...]:
        keywords = tuple(keyword.strip() for keyword in title_keywords if keyword.strip())
        tree = self.document_tree(receipt_no)
        selected = tuple(
            section
            for section in tree
            if not keywords or any(keyword in section.title for keyword in keywords)
        )
        return tuple((section, self.fetch_section(section)) for section in selected)


def parse_public_search_results(html: str) -> tuple[PublicDartFiling, ...]:
    soup = BeautifulSoup(html, "lxml")
    filings: list[PublicDartFiling] = []
    seen: set[str] = set()
    for row in soup.select("tr"):
        report_link = row.select_one('a[href*="rcpNo="]')
        if report_link is None:
            continue
        match = re.search(r"rcpNo=(\d{14})", str(report_link.get("href") or ""))
        if match is None:
            continue
        receipt_no = match.group(1)
        cells = row.find_all("td")
        if len(cells) < 5 or receipt_no in seen:
            continue
        corp_link = row.select_one("a[href*='openCorpInfoNew']")
        corp_name = _clean_text(
            corp_link.get_text(" ", strip=True) if corp_link else cells[1].get_text(" ")
        )
        corp_match = re.search(
            r"openCorpInfoNew\('([^']+)'",
            str(corp_link.get("href") or "") if corp_link else "",
        )
        submitted_on = _parse_dot_date(_clean_text(cells[-2].get_text(" ")))
        filings.append(
            PublicDartFiling(
                corp_name=corp_name,
                corp_code=corp_match.group(1) if corp_match else None,
                receipt_no=receipt_no,
                report_name=_clean_text(report_link.get_text(" ", strip=True)),
                presenter=_clean_text(cells[-3].get_text(" ")) or None,
                submitted_on=submitted_on,
            )
        )
        seen.add(receipt_no)
    return tuple(filings)


def parse_document_tree(html: str, receipt_no: str) -> tuple[PublicDartSection, ...]:
    _validate_receipt_no(receipt_no)
    sections: list[PublicDartSection] = []
    for block_match in _NODE_BLOCK_RE.finditer(html):
        values = {
            match.group("key"): html_module.unescape(match.group("value").replace('\\"', '"'))
            for match in _NODE_VALUE_RE.finditer(block_match.group("body"))
        }
        if values.get("rcpNo") != receipt_no:
            continue
        required = ("dcmNo", "eleId", "text", "offset", "length", "dtd")
        if any(not values.get(key) for key in required):
            continue
        try:
            offset = int(values["offset"])
            length = int(values["length"])
        except ValueError:
            continue
        if offset < 0 or length <= 0:
            continue
        sections.append(
            PublicDartSection(
                receipt_no=receipt_no,
                document_no=values["dcmNo"],
                element_id=values["eleId"],
                title=_clean_text(values["text"]),
                offset=offset,
                length=length,
                dtd=values["dtd"],
            )
        )
    return tuple(sections)


def extract_html_tables(html: str) -> tuple[DartHtmlTable, ...]:
    soup = BeautifulSoup(html, "lxml")
    tables: list[DartHtmlTable] = []
    for table in soup.find_all("table"):
        rows: list[tuple[str, ...]] = []
        headers: tuple[str, ...] = ()
        for row in table.find_all("tr"):
            cells = tuple(
                _clean_text(cell.get_text(" ", strip=True)) for cell in row.find_all(["th", "td"])
            )
            if not cells:
                continue
            if row.find("th") and not headers:
                headers = cells
            else:
                rows.append(cells)
        caption_node = table.find("caption")
        caption = _clean_text(caption_node.get_text(" ")) if caption_node else None
        if headers or rows:
            tables.append(DartHtmlTable(caption=caption or None, headers=headers, rows=tuple(rows)))
    return tuple(tables)


def section_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    return _clean_text(soup.get_text(" ", strip=True))


def tables_containing(
    tables: Iterable[DartHtmlTable],
    *keywords: str,
) -> tuple[DartHtmlTable, ...]:
    normalized = tuple(_clean_text(keyword) for keyword in keywords if _clean_text(keyword))
    return tuple(
        table for table in tables if all(keyword in table.searchable_text for keyword in normalized)
    )


def _validate_receipt_no(value: str) -> None:
    if not re.fullmatch(r"\d{14}", value):
        raise ValueError("receipt_no must contain exactly 14 digits")


def _parse_dot_date(value: str) -> Optional[date]:
    match = re.search(r"(\d{4})[.-](\d{2})[.-](\d{2})", value)
    if match is None:
        return None
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError:
        return None


def _clean_text(value: str) -> str:
    return " ".join(html_module.unescape(value).replace("\xa0", " ").split())
