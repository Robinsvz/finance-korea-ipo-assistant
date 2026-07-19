from __future__ import annotations

from datetime import date

import httpx
import pytest

from app.collectors.dart_public import (
    DART_PUBLIC_BASE,
    DartPublicCollector,
    PublicDartFiling,
    PublicDartSection,
    extract_html_tables,
    parse_document_tree,
    parse_public_search_results,
    section_text,
    tables_containing,
)
from app.exceptions import ExternalSourceError

SEARCH_HTML = """
<table>
  <tr>
    <td>1</td>
    <td><a href="javascript:openCorpInfoNew('01379129', 'x', '/popup');">매드업</a></td>
    <td><a href="/dsaf001/main.do?rcpNo=20260626000469">증권발행실적보고서</a></td>
    <td>매드업</td><td>2026.06.26</td><td></td>
  </tr>
  <tr>
    <td>2</td><td>매드업</td>
    <td><a href="/dsaf001/main.do?rcpNo=20260622000382">증권신고서(지분증권)</a></td>
    <td>매드업</td><td>invalid</td><td></td>
  </tr>
  <tr><td>duplicate</td><td>매드업</td>
    <td><a href="/dsaf001/main.do?rcpNo=20260622000382">증권신고서(지분증권)</a></td>
    <td>매드업</td><td>2026.06.22</td><td></td></tr>
  <tr><td>short</td><td><a href="/dsaf001/main.do?rcpNo=bad">bad</a></td></tr>
  <tr><td>no report link</td><td>매드업</td><td>plain text</td><td>x</td><td>x</td></tr>
  <tr><td>short valid</td><td><a href="/dsaf001/main.do?rcpNo=20260101000001">short</a></td></tr>
  <tr><td>3</td><td>매드업</td>
    <td><a href="/dsaf001/main.do?rcpNo=20260101000002">투자설명서</a></td>
    <td>매드업</td><td>2026.02.30</td><td></td></tr>
</table>
"""

TREE_HTML = """
<script>
var node1 = {};
node1['text'] = "Ⅱ. 청약 &amp; 배정";
node1['rcpNo'] = "20260626000469";
node1['dcmNo'] = "11449820";
node1['eleId'] = "5";
node1['offset'] = "7033";
node1['length'] = "168291";
node1['dtd'] = "dart4.xsd";
treeData.push(node1);
var node2 = {};
node2['text'] = "broken";
node2['rcpNo'] = "20260626000469";
node2['dcmNo'] = "11449820";
node2['eleId'] = "6";
node2['offset'] = "bad";
node2['length'] = "0";
node2['dtd'] = "dart4.xsd";
treeData.push(node2);
var node3 = {};
node3['text'] = "other";
node3['rcpNo'] = "20000101000000";
node3['dcmNo'] = "1";
node3['eleId'] = "1";
node3['offset'] = "1";
node3['length'] = "1";
node3['dtd'] = "x";
treeData.push(node3);
var node4 = {};
node4['text'] = "missing dtd";
node4['rcpNo'] = "20260626000469";
node4['dcmNo'] = "11449820";
node4['eleId'] = "7";
node4['offset'] = "1";
node4['length'] = "1";
treeData.push(node4);
var node5 = {};
node5['text'] = "invalid range";
node5['rcpNo'] = "20260626000469";
node5['dcmNo'] = "11449820";
node5['eleId'] = "8";
node5['offset'] = "1";
node5['length'] = "0";
node5['dtd'] = "dart4.xsd";
treeData.push(node5);
</script>
"""

SECTION_HTML = """
<html><body><p>Ⅱ. 청약 및 배정</p>
<table><caption>결과</caption><tr><th>구분</th><th>경쟁률</th></tr>
<tr><td>일반청약자</td><td>1,000 : 1</td></tr></table>
<table><tr><td>주</td><td>500</td></tr></table>
<table><tr></tr></table>
</body></html>
"""


def test_public_filing_url_and_search_parser() -> None:
    rows = parse_public_search_results(SEARCH_HTML)
    assert len(rows) == 3
    assert rows[0] == PublicDartFiling(
        corp_name="매드업",
        corp_code="01379129",
        receipt_no="20260626000469",
        report_name="증권발행실적보고서",
        presenter="매드업",
        submitted_on=date(2026, 6, 26),
    )
    assert rows[0].url == f"{DART_PUBLIC_BASE}/dsaf001/main.do?rcpNo=20260626000469"
    assert rows[1].corp_code is None
    assert rows[1].submitted_on is None
    assert rows[2].submitted_on is None
    assert parse_public_search_results("<html></html>") == ()


def test_search_filings_validates_filters_and_final_flag() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=SEARCH_HTML, request=request)

    collector = DartPublicCollector(transport=httpx.MockTransport(handler), min_interval_sec=0)
    rows = collector.search_filings(
        " 매드업 ",
        start=date(2026, 1, 1),
        end=date(2026, 7, 19),
        report_keywords=("증권발행실적보고서",),
        final_only=True,
    )
    assert [row.receipt_no for row in rows] == ["20260626000469"]
    assert b"finalReport=recent" in requests[0].content
    assert b"maxResults=100" in requests[0].content
    all_rows = collector.search_filings(
        "매드업",
        start=date(2026, 1, 1),
        end=date(2026, 7, 19),
        report_keywords=(),
    )
    assert len(all_rows) == 3
    assert b"finalReport" not in requests[1].content
    with pytest.raises(ValueError, match="company_name"):
        collector.search_filings(" ", start=date(2026, 1, 1), end=date(2026, 1, 1))
    with pytest.raises(ValueError, match="end"):
        collector.search_filings("x", start=date(2026, 2, 1), end=date(2026, 1, 1))


def test_document_tree_section_fetch_and_matching() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        text = TREE_HTML if request.url.path.endswith("main.do") else SECTION_HTML
        return httpx.Response(200, text=text, request=request)

    collector = DartPublicCollector(transport=httpx.MockTransport(handler), min_interval_sec=0)
    sections = collector.document_tree("20260626000469")
    assert sections == (
        PublicDartSection(
            receipt_no="20260626000469",
            document_no="11449820",
            element_id="5",
            title="Ⅱ. 청약 & 배정",
            offset=7033,
            length=168291,
            dtd="dart4.xsd",
        ),
    )
    assert "일반청약자" in collector.fetch_section(sections[0])
    assert collector.fetch_matching_sections("20260626000469", "청약")[0][0] == sections[0]
    assert collector.fetch_matching_sections("20260626000469", "없음") == ()
    assert collector.fetch_matching_sections("20260626000469")[0][1] == SECTION_HTML
    with pytest.raises(ValueError, match="14 digits"):
        collector.document_tree("bad")


def test_document_tree_rejects_empty_or_invalid_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>empty</html>", request=request)

    collector = DartPublicCollector(transport=httpx.MockTransport(handler), min_interval_sec=0)
    with pytest.raises(ExternalSourceError, match="no document sections"):
        collector.document_tree("20260626000469")
    assert parse_document_tree(TREE_HTML, "20260626000469")[0].title.endswith("배정")
    with pytest.raises(ValueError, match="14 digits"):
        parse_document_tree(TREE_HTML, "x")


def test_fetch_section_rejects_invalid_payload_and_redirect_host() -> None:
    section = PublicDartSection("20260626000469", "1", "1", "x", 1, 1, "x")

    def invalid_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not markup", request=request)

    invalid = DartPublicCollector(
        transport=httpx.MockTransport(invalid_handler), min_interval_sec=0
    )
    with pytest.raises(ExternalSourceError, match="invalid section"):
        invalid.fetch_section(section)
    with pytest.raises(ValueError, match="14 digits"):
        invalid.fetch_section(PublicDartSection("bad", "1", "1", "x", 1, 1, "x"))

    def redirect_handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "dart.fss.or.kr":
            return httpx.Response(
                302, headers={"Location": "https://evil.example/x"}, request=request
            )
        return httpx.Response(200, text="<html></html>", request=request)

    redirected = DartPublicCollector(
        transport=httpx.MockTransport(redirect_handler), min_interval_sec=0
    )
    with pytest.raises(ExternalSourceError, match="unexpected host"):
        redirected.fetch_section(section)


def test_table_and_text_helpers() -> None:
    tables = extract_html_tables(SECTION_HTML)
    assert len(tables) == 2
    assert tables[0].caption == "결과"
    assert tables[0].headers == ("구분", "경쟁률")
    assert tables[0].rows == (("일반청약자", "1,000 : 1"),)
    assert tables[0].searchable_text.startswith("결과 구분 경쟁률")
    assert tables_containing(tables, "일반청약자", "경쟁률") == (tables[0],)
    assert tables_containing(tables, "없는 값") == ()
    assert tables_containing(tables) == tables
    assert tables_containing(tables, " ") == tables
    assert extract_html_tables("<html></html>") == ()
    assert section_text("<p> A&nbsp; B\n C </p>") == "A B C"
