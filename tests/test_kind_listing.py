from datetime import date
from html import escape
from types import SimpleNamespace

import pytest

from app.collectors.kind import (
    KIND_LISTING_URL,
    KindListingCollector,
    parse_kind_listing_html,
)
from app.exceptions import ExternalSourceError

BASE_HEADERS = [
    "회사명",
    "상장일",
    "상장유형",
    "증권구분",
    "업종",
    "국적",
    "상장주선인/<br/>지정자문인",
]
OPTIONAL_HEADERS = {
    "01": "액면가 (원)",
    "02": "공모가 (원)",
    "03": "공모금액 (천원)",
    "04": "주요제품",
    "05": "최초상장주식수 (주)",
}
OPTIONAL_VALUES = {
    "01": "500",
    "02": "20,700",
    "03": "24,840,000",
    "04": "의료기기",
    "05": "7,625,791",
}


def _row(
    name="레메디",
    *,
    columns=("02", "03", "05"),
    isur_cd="38769",
    bz_procs_no="20251127000242",
    onclick=None,
    listing_date="2026-07-13",
    listing_type="신규상장",
    security_type="주권",
    underwriter="KB증권(주)",
    with_market=True,
):
    if onclick is None:
        onclick = f"fnDetailView('{isur_cd}','{bz_procs_no}')"
    market = "<img alt='코스닥'/>" if with_market else ""
    cells = [
        f'<td class="first" title="{name}">{market}{name}</td>',
        f"<td>{listing_date}</td>",
        f"<td>{listing_type}</td>",
        f"<td>{security_type}</td>",
        "<td>의료용 기기 제조업</td>",
        "<td>대한민국</td>",
        f"<td>{underwriter}</td>",
    ]
    cells.extend(f"<td>{OPTIONAL_VALUES[column]}</td>" for column in columns)
    return f'<tr onclick="{escape(onclick, quote=True)}">' + "".join(cells) + "</tr>"


def _html(rows, *, columns=("02", "03", "05"), dynamic_headers=True):
    headers = BASE_HEADERS + [OPTIONAL_HEADERS[column] for column in columns]
    if dynamic_headers:
        thead = "<thead><tr id='title-contents'></tr></thead>"
        script = f"<script>fn_InitTitle('{','.join(headers)}', 'true');</script>"
    else:
        thead = (
            "<thead><tr>" + "".join(f"<th>{header}</th>" for header in headers) + "</tr></thead>"
        )
        script = ""
    return (
        '<table class="list type-00"><caption>신규상장기업현황 목록</caption>'
        + thead
        + "<tbody>"
        + "".join(rows)
        + "</tbody></table>"
        + script
    )


def _empty_html(message="조회된 결과값이 없습니다."):
    return _html([f"<tr><td colspan='10'>{message}</td></tr>"])


def test_parse_kind_listing_live_shape_and_identifiers():
    records = parse_kind_listing_html(_html([_row()]))

    assert len(records) == 1
    record = records[0]
    assert record.corp_name == "레메디"
    assert record.listing_date == date(2026, 7, 13)
    assert record.listing_type == "신규상장"
    assert record.security_type == "주권"
    assert record.market == "코스닥"
    assert record.underwriter == "KB증권(주)"
    assert record.offer_price == 20_700
    assert record.offer_amount_thousand == 24_840_000
    assert record.initial_shares == 7_625_791
    assert record.isur_cd == "38769"
    assert record.bz_procs_no == "20251127000242"
    assert record.raw["industry"] == "의료용 기기 제조업"
    assert record.raw["country"] == "대한민국"
    assert record.raw["selected_columns"] == ["02", "03", "05"]


def test_selected_columns_are_canonical_and_unselected_values_are_none():
    columns = ("01", "05")
    row = (
        _row(
            columns=columns,
            onclick='fnDetailView( "0039P" , "20250328000374" )',
            listing_type="-",
            security_type=" ",
            underwriter="-",
            with_market=False,
        )
        .replace("의료용 기기 제조업", "-")
        .replace("대한민국", "-")
    )
    record = parse_kind_listing_html(
        _html([row], columns=columns, dynamic_headers=False),
        selected_columns=("05", "01", "05"),
    )[0]

    assert record.market is None
    assert record.listing_type is None
    assert record.security_type is None
    assert record.underwriter is None
    assert record.offer_price is None
    assert record.offer_amount_thousand is None
    assert record.initial_shares == 7_625_791
    assert record.isur_cd == "0039P"
    assert record.raw["industry"] is None
    assert record.raw["country"] is None
    assert record.raw["selected_columns"] == ["01", "05"]


def test_parser_supports_no_optional_columns():
    record = parse_kind_listing_html(
        _html([_row(columns=())], columns=()),
        selected_columns=(),
    )[0]
    assert record.offer_price is None
    assert record.offer_amount_thousand is None
    assert record.initial_shares is None


@pytest.mark.parametrize("marker", ["페이지 오류", "잠시 후 다시 이용해 주세요."])
def test_parser_rejects_kind_error_pages(marker):
    with pytest.raises(ExternalSourceError, match="error page"):
        parse_kind_listing_html(marker)


def test_parser_rejects_missing_table_and_missing_or_changed_headers():
    with pytest.raises(ExternalSourceError, match="new-listing table"):
        parse_kind_listing_html("<html></html>")

    html = "<table class='list'><thead></thead><tbody></tbody></table>"
    with pytest.raises(ExternalSourceError, match="column headers"):
        parse_kind_listing_html(html)

    changed_headers = list(BASE_HEADERS)
    changed_headers[1] = "변경일"
    headers = changed_headers + [OPTIONAL_HEADERS[column] for column in ("02", "03", "05")]
    original_headers = BASE_HEADERS + [OPTIONAL_HEADERS[column] for column in ("02", "03", "05")]
    html = _html([_row()]).replace(",".join(original_headers), ",".join(headers))
    with pytest.raises(ExternalSourceError, match="column headers"):
        parse_kind_listing_html(html)


def test_parser_handles_explicit_and_implicit_empty_results():
    no_rows = _html([])
    with pytest.raises(ExternalSourceError, match="unexpectedly empty"):
        parse_kind_listing_html(no_rows)
    assert parse_kind_listing_html(no_rows, allow_empty=True) == []

    for message in ("조회된 결과값이 없습니다.", "검색된 자료가 없습니다."):
        html = _empty_html(message)
        with pytest.raises(ExternalSourceError, match="unexpectedly empty"):
            parse_kind_listing_html(html)
        assert parse_kind_listing_html(html, allow_empty=True) == []


def test_parser_rejects_all_malformed_rows_and_keeps_valid_rows():
    too_short = "<tr><td>한 칸</td></tr>"
    blank_name = _row(name="")
    invalid_date = _row(name="날짜오류", listing_date="not-a-date")
    no_identifiers = _row(name="식별자오류", onclick="otherFunction()")
    html = _html([too_short, blank_name, invalid_date, no_identifiers])
    with pytest.raises(ExternalSourceError, match="no valid rows"):
        parse_kind_listing_html(html)

    records = parse_kind_listing_html(_html([too_short, _row()]))
    assert [record.corp_name for record in records] == ["레메디"]


def test_parser_rejects_unknown_selected_column():
    with pytest.raises(ValueError, match="unsupported KIND listing columns"):
        parse_kind_listing_html(_html([_row()]), selected_columns=("99",))


class _ClientContext:
    def __init__(self, text="ok"):
        self.text = text
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return SimpleNamespace(text=self.text)


def test_fetch_page_posts_exact_window_page_and_canonical_columns():
    collector = KindListingCollector()
    client = _ClientContext("payload")
    result = collector.fetch_page(
        start=date(2026, 7, 1),
        end=date(2026, 7, 31),
        page_size=50,
        page_index=3,
        client=client,
        selected_columns=("05", "02", "03", "02"),
    )

    assert result == "payload"
    method, url, kwargs = client.calls[0]
    assert method == "POST"
    assert url == KIND_LISTING_URL
    assert kwargs["source"] == "KIND listing"
    assert kwargs["data"]["method"] == "searchListingTypeSub"
    assert kwargs["data"]["forward"] == "listingtype_sub"
    assert kwargs["data"]["fromDate"] == "2026-07-01"
    assert kwargs["data"]["toDate"] == "2026-07-31"
    assert kwargs["data"]["currentPageSize"] == "50"
    assert kwargs["data"]["pageIndex"] == "3"
    assert kwargs["data"]["listTypeArrStr"] == "01|"
    assert kwargs["data"]["choicTypeArrStr"] == "02|03|05|"
    assert kwargs["data"]["secuGrpArrStr"] == "0|ST|FS|MF|SC|RT|IF|DR|"


def test_fetch_page_validates_input_empty_columns_and_error_response():
    collector = KindListingCollector()
    client = _ClientContext()
    common = {
        "start": date(2026, 7, 1),
        "end": date(2026, 7, 31),
        "page_size": 10,
        "page_index": 1,
        "client": client,
    }
    with pytest.raises(ValueError, match="must not be after"):
        collector.fetch_page(**{**common, "start": date(2026, 8, 1)})
    with pytest.raises(ValueError, match="positive"):
        collector.fetch_page(**{**common, "page_size": 0})
    with pytest.raises(ValueError, match="positive"):
        collector.fetch_page(**{**common, "page_index": 0})
    with pytest.raises(ValueError, match="unsupported"):
        collector.fetch_page(**common, selected_columns=("99",))

    assert collector.fetch_page(**common, selected_columns=()) == "ok"
    assert client.calls[-1][2]["data"]["choicTypeArrStr"] == ""

    client.text = "페이지 오류"
    with pytest.raises(ExternalSourceError, match="error page"):
        collector.fetch_page(**common)


class StubKindListingCollector(KindListingCollector):
    def __init__(self, pages):
        super().__init__()
        self.pages = pages
        self.request_interval_sec = 0

    def fetch_page(self, **kwargs):
        return self.pages[kwargs["page_index"] - 1]

    def _listing_client(self):
        return _ClientContext()


def test_collect_paginates_deduplicates_and_keeps_raw_pages():
    first = _html(
        [
            _row(name="A", isur_cd="A1", bz_procs_no="P1"),
            _row(name="B", isur_cd="B1", bz_procs_no="P2"),
        ]
    )
    second = _html(
        [
            _row(name="A-duplicate", isur_cd="A1", bz_procs_no="P1"),
            _row(name="C", isur_cd="C1", bz_procs_no="P3"),
        ]
    )
    collector = StubKindListingCollector([first, second, _empty_html()])
    records, raw = collector.collect_with_raw(
        date(2026, 7, 1),
        date(2026, 7, 31),
        page_size=2,
    )

    assert [record.corp_name for record in records] == ["A", "B", "C"]
    assert raw.count("KIND_LISTING_PAGE") == 2


def test_collect_wrapper_short_page_empty_page_and_validation():
    one = _html([_row()])
    collector = StubKindListingCollector([one])
    records = collector.collect(date(2026, 7, 1), date(2026, 7, 31), page_size=100)
    assert [record.corp_name for record in records] == ["레메디"]

    empty = _empty_html()
    records, raw = StubKindListingCollector([empty]).collect_with_raw(
        date(2026, 7, 1), date(2026, 7, 31)
    )
    assert records == []
    assert raw == empty

    with pytest.raises(ValueError, match="must not be after"):
        collector.collect_with_raw(date(2026, 8, 1), date(2026, 7, 1))
    with pytest.raises(ValueError, match="positive"):
        collector.collect_with_raw(date(2026, 7, 1), date(2026, 7, 31), page_size=0)
    with pytest.raises(ValueError, match="positive"):
        collector.collect_with_raw(date(2026, 7, 1), date(2026, 7, 31), max_pages=0)
    with pytest.raises(ValueError, match="unsupported"):
        collector.collect_with_raw(
            date(2026, 7, 1),
            date(2026, 7, 31),
            selected_columns=("99",),
        )


def test_collect_raises_when_pagination_limit_is_reached():
    full_page = _html([_row()])
    collector = StubKindListingCollector([full_page])
    with pytest.raises(ExternalSourceError, match="exceeded 1 pages"):
        collector.collect_with_raw(
            date(2026, 7, 1),
            date(2026, 7, 31),
            page_size=1,
            max_pages=1,
        )


def test_real_listing_client_factory_can_be_closed():
    collector = KindListingCollector(timeout=0.1)
    client = collector._listing_client()
    assert client.min_interval_sec == collector.request_interval_sec
    assert client.client.headers["X-Requested-With"] == "XMLHttpRequest"
    client.close()
