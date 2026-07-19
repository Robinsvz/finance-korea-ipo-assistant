from datetime import date
from types import SimpleNamespace

import pytest

from app.collectors.kind import (
    KindScheduleCollector,
    _parse_date_range,
    _parse_underwriters,
    filter_records_for_week,
    parse_kind_puboffering_html,
)
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


class _ClientContext:
    def __init__(self, text="<table><tbody></tbody></table>"):
        self.text = text
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return SimpleNamespace(text=self.text)


def test_fetch_page_validates_parameters_and_rejects_kind_error_page():
    collector = KindScheduleCollector()
    client = _ClientContext()
    with pytest.raises(ValueError, match="positive"):
        collector.fetch_page(
            start=date(2026, 1, 1),
            end=date(2026, 1, 2),
            page_size=0,
            page_index=1,
            client=client,
        )
    with pytest.raises(ValueError, match="positive"):
        collector.fetch_page(
            start=date(2026, 1, 1),
            end=date(2026, 1, 2),
            page_size=1,
            page_index=0,
            client=client,
        )

    client.text = "페이지 오류"
    with pytest.raises(ExternalSourceError, match="error page"):
        collector.fetch_page(
            start=date(2026, 1, 1),
            end=date(2026, 1, 2),
            page_size=10,
            page_index=2,
            client=client,
        )

    client.text = "ok"
    assert (
        collector.fetch_page(
            start=date(2026, 1, 1),
            end=date(2026, 1, 2),
            page_size=10,
            page_index=2,
            client=client,
        )
        == "ok"
    )
    assert client.calls[-1][2]["params"]["pageIndex"] == "2"


def test_fetch_uses_default_window_and_owned_client(monkeypatch):
    collector = KindScheduleCollector()
    client = _ClientContext("payload")
    captured = {}
    monkeypatch.setattr(collector, "_client", lambda: client)

    def fake_fetch_page(**kwargs):
        captured.update(kwargs)
        return "payload"

    monkeypatch.setattr(collector, "fetch_page", fake_fetch_page)
    monkeypatch.setattr("app.collectors.kind.local_today", lambda: date(2026, 7, 20))
    assert collector.fetch(page_size=25) == "payload"
    assert captured["page_index"] == 1
    assert captured["client"] is client


def test_collect_rejects_reversed_window_and_page_limit():
    collector = StubKindCollector([_page("A")])
    with pytest.raises(ValueError, match="must not be after"):
        collector.collect_with_raw(start=date(2026, 2, 1), end=date(2026, 1, 1))
    with pytest.raises(ExternalSourceError, match="exceeded 1 pages"):
        collector.collect_with_raw(
            start=date(2026, 1, 1),
            end=date(2026, 1, 2),
            page_size=1,
            max_pages=1,
        )


def test_collect_defaults_and_deduplicates_last_record(monkeypatch):
    collector = StubKindCollector([_many_rows(0, 2)])
    monkeypatch.setattr("app.collectors.kind.local_today", lambda: date(2026, 7, 20))
    records, _raw = collector.collect_with_raw(page_size=100)
    assert len(records) == 2

    duplicate_page = _page("A") + _page("A")
    duplicate_page = duplicate_page.replace("</table>\n    <table><tbody>", "")
    duplicate_page = duplicate_page.replace("</tbody><tbody>", "")
    collector = StubKindCollector([duplicate_page])
    records, _raw = collector.collect_with_raw(
        start=date(2026, 1, 1), end=date(2026, 12, 31), page_size=100
    )
    assert len(records) == 1


def test_parser_handles_optional_fields_malformed_rows_and_valid_headers():
    headers = [
        "기업명",
        "신고일",
        "수요예측일",
        "청약일",
        "납입일",
        "공모가격",
        "공모금액",
        "상장일",
        "주간사",
    ]
    valid = _page("A")
    valid_row = valid[valid.index("<tr") : valid.index("</tr>") + 5]
    valid_row = valid_row.replace(" onclick=\"fnDetailView('A')\"", "")
    valid_row = valid_row.replace('<td title="A"><img alt="코스닥"/>A</td>', "<td>A</td>")
    malformed = "<tr><td>only one</td></tr>"
    blank_name = "<tr>" + "".join("<td></td>" for _ in range(9)) + "</tr>"
    html = (
        "<table><thead><tr>"
        + "".join(f"<th>{header}</th>" for header in headers)
        + "</tr></thead><tbody>"
        + malformed
        + blank_name
        + valid_row
        + "</tbody></table>"
    )
    records = parse_kind_puboffering_html(html)
    assert len(records) == 1
    assert records[0].market is None
    assert records[0].kind_biz_no is None


def test_kind_small_parsers_and_week_filter():
    assert _parse_date_range("없음") == (None, None)
    assert _parse_date_range("2026.07.20") == (date(2026, 7, 20), date(2026, 7, 20))
    assert _parse_date_range("2026/07/20 ~ 2026-07-21") == (
        date(2026, 7, 20),
        date(2026, 7, 21),
    )
    assert _parse_underwriters("한투 / - , 미래") == ["한투", "미래"]

    records = parse_kind_puboffering_html(_page("A"))
    record = records[0]
    assert filter_records_for_week(records, date(2026, 1, 4), date(2026, 1, 4)) == [record]
    assert filter_records_for_week(records, date(2026, 1, 10), date(2026, 1, 10)) == [record]
    assert filter_records_for_week(records, date(2026, 2, 1), date(2026, 2, 2)) == []


def test_real_client_factory_can_be_closed():
    collector = KindScheduleCollector(timeout=0.1)
    client = collector._client()
    assert client.min_interval_sec == collector.request_interval_sec
    client.close()
