from datetime import date

import pytest

from app.collectors.kind import KindScheduleCollector, parse_kind_puboffering_html
from app.exceptions import ExternalSourceError


def _page(name):
    if name is None:
        return "<table><tbody></tbody></table>"
    return f"""
    <table><tbody><tr onclick="fnDetailView('{name}')">
    <td title="{name}"><img alt="코스닥"/>{name}</td>
    <td>2026-01-01</td><td>2026-01-02 ~ 2026-01-03</td>
    <td>2026-01-04 ~ 2026-01-05</td><td>2026-01-07</td>
    <td>10,000</td><td>5,000</td><td>2026-01-10</td><td>한투</td>
    </tr></tbody></table>
    """


def _many_rows(start, count):
    rows = []
    for index in range(start, start + count):
        name = f"기업{index:03d}"
        row = _page(name)
        rows.append(row[row.index("<tr") : row.index("</tr>") + 5])
    return "<table><tbody>" + "".join(rows) + "</tbody></table>"


class StubKindCollector(KindScheduleCollector):
    def __init__(self, pages):
        super().__init__()
        self.pages = pages
        self.request_interval_sec = 0

    def fetch_page(self, **kwargs):
        return self.pages[kwargs["page_index"] - 1]


def test_collects_multiple_pages_and_deduplicates():
    collector = StubKindCollector([_page("A"), _page("B"), _page(None)])
    rows, raw = collector.collect_with_raw(
        start=date(2026, 1, 1),
        end=date(2026, 12, 31),
        page_size=1,
    )
    assert [row.corp_name for row in rows] == ["A", "B"]
    assert raw.count("KIND_PAGE") == 2


def test_collects_all_250_rows_across_pages():
    collector = StubKindCollector([_many_rows(0, 100), _many_rows(100, 100), _many_rows(200, 50)])
    rows = collector.collect(
        start=date(2026, 1, 1),
        end=date(2026, 12, 31),
        page_size=100,
    )
    assert len(rows) == 250
    assert len({row.kind_biz_no for row in rows}) == 250


def test_missing_table_is_error():
    with pytest.raises(ExternalSourceError, match="schedule table"):
        parse_kind_puboffering_html("<html></html>")


def test_empty_first_page_is_error():
    with pytest.raises(ExternalSourceError, match="unexpectedly empty"):
        parse_kind_puboffering_html(_page(None))


def test_malformed_schema_is_error():
    with pytest.raises(ExternalSourceError, match="schema changed"):
        parse_kind_puboffering_html("<table><tbody><tr><td>one</td></tr></tbody></table>")


def test_changed_headers_are_explicit_error():
    headers = "".join(f"<th>열{index}</th>" for index in range(9))
    html = _page("A").replace("<table>", f"<table><thead><tr>{headers}</tr></thead>")
    with pytest.raises(ExternalSourceError, match="column headers"):
        parse_kind_puboffering_html(html)
