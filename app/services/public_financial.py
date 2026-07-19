from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from bs4 import BeautifulSoup, Tag

_ACTUAL_PERIOD = re.compile(r"(20\d{2})")
_FORECAST_MARKERS = ("(E)", "[E]", "（E）", "［E］", "추정", "예상", "전망")
_UNIT_PATTERN = re.compile(r"단위\s*[:：]\s*(백만원|천원|원)")
_REVENUE_LABELS = ("매출액", "영업수익")


@dataclass(frozen=True)
class PublicFinancialSummary:
    period: str
    revenue: Decimal
    operating_profit: Decimal
    net_income: Decimal
    unit: str
    statement_basis: Optional[str] = None


@dataclass(frozen=True)
class _Candidate:
    score: tuple[int, int, int, int, int]
    summary: PublicFinancialSummary


def parse_public_financial_summary(raw_html: str) -> Optional[PublicFinancialSummary]:
    """Return the latest actual issuer results from a public DART filing.

    IPO filings repeat issuer, peer, and forecast tables.  This parser deliberately
    accepts only compact tables whose first column is a generic issuer heading,
    whose periods contain no forecast marker, and which contain all three required
    P&amp;L rows.  When multiple copies exist, the newest and most detailed actual
    period wins deterministically.
    """

    if not raw_html.strip():
        return None
    soup = BeautifulSoup(raw_html, "lxml")
    candidates = tuple(
        candidate
        for table_index, table in enumerate(soup.select("table"))
        if (candidate := _candidate(table, table_index)) is not None
    )
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate.score).summary


def _candidate(table: Tag, table_index: int) -> Optional[_Candidate]:
    rows = _table_rows(table)
    if len(rows) < 4:
        return None
    metric_indexes = {
        "revenue": _metric_index(rows, _REVENUE_LABELS),
        "operating_profit": _metric_index(rows, ("영업이익", "영업손실")),
        "net_income": _metric_index(rows, ("당기순이익", "당기순손실")),
    }
    if any(value is None for value in metric_indexes.values()):
        return None
    first_metric = min(int(value) for value in metric_indexes.values() if value is not None)
    header_index = _header_index(rows, first_metric)
    if header_index is None:
        return None
    header = rows[header_index]
    periods = tuple(_period(cell, column) for column, cell in enumerate(header[1:], 1))
    actual_periods = tuple(item for item in periods if item is not None)
    if not actual_periods:
        return None
    selected = max(actual_periods, key=lambda item: item[0])
    _period_score, column, period_label = selected
    values: dict[str, Decimal] = {}
    for field_name, row_index in metric_indexes.items():
        assert row_index is not None
        row = rows[row_index]
        if column >= len(row):
            return None
        parsed = _number(row[column])
        if parsed is None:
            return None
        values[field_name] = parsed

    unit = _table_unit(table)
    if unit is None:
        return None
    basis = _statement_basis(table)
    period_score = selected[0]
    summary = PublicFinancialSummary(
        period=period_label,
        revenue=values["revenue"],
        operating_profit=values["operating_profit"],
        net_income=values["net_income"],
        unit=unit,
        statement_basis=basis,
    )
    revenue_index = metric_indexes["revenue"]
    assert revenue_index is not None
    direct_revenue = int(_compact(rows[revenue_index][0]).startswith("매출액"))
    return _Candidate(
        score=(
            period_score[0],
            period_score[1],
            direct_revenue,
            len(actual_periods),
            -table_index,
        ),
        summary=summary,
    )


def _table_rows(table: Tag) -> tuple[tuple[str, ...], ...]:
    return tuple(
        tuple(cell.get_text(" ", strip=True) for cell in row.select("th, td"))
        for row in table.select("tr")
        if row.select("th, td")
    )


def _metric_index(
    rows: tuple[tuple[str, ...], ...],
    labels: tuple[str, ...],
) -> Optional[int]:
    for index, row in enumerate(rows):
        label = _compact(row[0])
        if any(label.startswith(item) for item in labels):
            if "증가율" not in label and "이익률" not in label and "순이익률" not in label:
                return index
    return None


def _header_index(rows: tuple[tuple[str, ...], ...], first_metric: int) -> Optional[int]:
    for index in range(first_metric - 1, -1, -1):
        row = rows[index]
        if row and _compact(row[0]) in {"구분", "계정"}:
            return index
    return None


def _period(value: str, column: int) -> Optional[tuple[tuple[int, int], int, str]]:
    normalized = value.upper().replace(" ", "")
    if any(marker.upper().replace(" ", "") in normalized for marker in _FORECAST_MARKERS):
        return None
    match = _ACTUAL_PERIOD.search(value)
    if match is None:
        return None
    year = int(match.group(1))
    quarter = re.search(r"(\d{1,2})\s*분기", value)
    month = re.search(r"(\d{1,2})\s*월", value)
    dotted_months = tuple(int(item) for item in re.findall(r"20\d{2}[./-](\d{1,2})", value))
    if quarter is not None:
        subperiod = int(quarter.group(1)) * 3
    elif month is not None:
        subperiod = int(month.group(1))
    elif dotted_months:
        subperiod = max(dotted_months)
    else:
        subperiod = 12
    return (year, subperiod), column, " ".join(value.split())


def _number(value: str) -> Optional[Decimal]:
    normalized = value.strip().replace(",", "")
    if not normalized or normalized.upper() in {"-", "N/A", "NA", "미공시"}:
        return None
    negative_parentheses = normalized.startswith("(")
    match = re.search(r"[-+]?\d+(?:\.\d+)?", normalized)
    if match is None:
        return None
    parsed = Decimal(match.group())
    return -abs(parsed) if negative_parentheses else parsed


def _table_unit(table: Tag) -> Optional[str]:
    for node in table.find_all_previous(limit=10):
        text = " ".join(node.get_text(" ", strip=True).split())
        match = _UNIT_PATTERN.search(text)
        if match is not None:
            return match.group(1)
    return None


def _statement_basis(table: Tag) -> Optional[str]:
    text = " ".join(table.get_text(" ", strip=True).split())
    if "연결" in text:
        return "연결"
    if "별도" in text:
        return "별도"
    return None


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value)
