import io
import json
import os
import time
import zipfile
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

from app.collectors.dart import (
    DartCollector,
    DartFinancialCollector,
    _bytes_to_text,
    _evidence_excerpt,
    _extract_table_values,
    _safe_zip_members,
    _search_float,
    _search_int,
    _xml_text,
    build_filing_lineage,
    extract_demand_forecast_fields,
    summarize_financial_rows,
)
from app.exceptions import ExternalSourceError


def _collector_without_init(cls=DartCollector):
    collector = object.__new__(cls)
    collector.api_key = "test"
    collector.timeout = 1
    collector.request_interval_sec = 0
    return collector


def test_demand_fields_include_evidence():
    text = (
        "기관 수요예측 경쟁률 1,500:1 의무보유확약 20% "
        "확정 공모가 12,000원 참여 기관 1,800개 밴드 상단 초과 65%"
    )
    result = extract_demand_forecast_fields(text)
    assert result["institutional_competition_ratio"] == 1500
    assert result["lockup_commitment_ratio"] == 20
    assert result["offer_price_final"] == 12000
    assert result["above_band_participation_ratio"] == 0.65
    assert result["evidence"]["offer_price_final"]["excerpt"]


def test_demand_fields_prefer_structured_table_rows():
    text = """
    | 구분 | 결과 |
    | 기관 수요예측 경쟁률 | 1,234.5:1 |
    | 의무보유확약 비율 | 18.2% |
    | 확정 공모가 | 15,000원 |
    | 참여 기관 수 | 1,920개 |
    | 밴드 상단 초과 참여 비율 | 61.5% |
    """
    result = extract_demand_forecast_fields(text)
    assert result["institutional_competition_ratio"] == 1234.5
    assert result["lockup_commitment_ratio"] == 18.2
    assert result["offer_price_final"] == 15000
    assert result["participants_count"] == 1920
    assert result["above_band_participation_ratio"] == 0.615
    assert result["evidence"]["institutional_competition_ratio"]["method"] == "table"
    assert result["evidence"]["offer_price_final"]["unit"] == "KRW"
    assert result["evidence"]["offer_price_final"]["confidence"] == 0.95


def test_exact_corp_match_wins():
    collector = _collector_without_init()
    rows = [
        {"corp_name": "테스트", "corp_code": "1", "stock_code": ""},
        {"corp_name": "테스트홀딩스", "corp_code": "2", "stock_code": ""},
    ]
    assert collector.find_corp_code("테스트", rows) == "1"


def test_ambiguous_fuzzy_corp_match_returns_none():
    collector = _collector_without_init()
    rows = [
        {"corp_name": "테스트A", "corp_code": "1", "stock_code": ""},
        {"corp_name": "테스트B", "corp_code": "2", "stock_code": ""},
    ]
    assert collector.find_corp_code("테스트", rows) is None


def test_stock_code_match_wins():
    collector = _collector_without_init()
    rows = [{"corp_name": "다른명", "corp_code": "1", "stock_code": "123456"}]
    assert collector.find_corp_code("테스트", rows, stock_code="123456") == "1"
    assert collector.find_corp("테스트", rows, stock_code="123456") == rows[0]


def test_find_corp_preserves_stock_code_and_rejects_ambiguous_exact_match():
    collector = _collector_without_init()
    row = {"corp_name": "테스트", "corp_code": "1", "stock_code": "123456"}
    assert collector.find_corp("테 스트", [row]) == row
    assert collector.find_corp("테스트", [row, dict(row, corp_code="2")]) is None


def test_financial_cfs_falls_back_to_ofs(monkeypatch):
    collector = _collector_without_init(DartFinancialCollector)
    calls = []

    def fake_get(_path, params, client=None):
        calls.append(params["fs_div"])
        if params["fs_div"] == "CFS":
            return {"status": "013", "list": []}
        return {"status": "000", "list": [{"account_nm": "매출액", "thstrm_amount": "10"}]}

    monkeypatch.setattr(collector, "_get_json", fake_get)
    result = collector.fetch_single_company_financials("corp", [2025])
    assert calls == ["CFS", "OFS"]
    assert result[0]["fs_div"] == "OFS"


def test_financial_summary_preserves_zero_revenue():
    rows = [
        {
            "fiscal_year": 2024,
            "account_nm": "매출액",
            "account_id": "ifrs_Revenue",
            "thstrm_amount": "0",
        },
        {
            "fiscal_year": 2025,
            "account_nm": "매출액",
            "account_id": "ifrs_Revenue",
            "thstrm_amount": "10",
        },
    ]
    result = summarize_financial_rows(rows)
    assert result[0]["revenue"] == 0
    assert result[1]["revenue_yoy_growth"] is None
    assert result[1]["revenue_yoy_status"] == "prior_zero"


def test_financial_summary_handles_negative_equity_without_division_error():
    rows = [
        {"fiscal_year": 2025, "account_nm": "자본총계", "thstrm_amount": "-10"},
        {"fiscal_year": 2025, "account_nm": "부채총계", "thstrm_amount": "30"},
    ]
    assert summarize_financial_rows(rows)[0]["debt_ratio"] == -3


def test_safe_zip_rejects_invalid_archive():
    with pytest.raises(ExternalSourceError, match="invalid ZIP"):
        _safe_zip_members(b"not a zip")


def test_safe_zip_reads_member():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("file.xml", "hello")
    assert _safe_zip_members(buffer.getvalue()) == [("file.xml", b"hello")]


def test_search_filings_paginates_and_sorts(monkeypatch):
    collector = _collector_without_init()
    pages = [
        {"status": "000", "total_count": 2, "list": [{"rcept_no": "1"}]},
        {"status": "000", "total_count": 2, "list": [{"rcept_no": "2"}]},
    ]

    class DummyClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr("app.collectors.dart.ResilientHttpClient", lambda **_kwargs: DummyClient())
    monkeypatch.setattr(collector, "_get_json", lambda _path, _params, client=None: pages.pop(0))
    result = collector.search_filings(corp_code="1")
    assert [row["rcept_no"] for row in result] == ["1", "2"]


def test_filing_lineage_marks_latest_correction_as_final():
    rows = [
        {"report_nm": "증권신고서(지분증권)", "rcept_dt": "20260701", "rcept_no": "1"},
        {"report_nm": "[정정]증권신고서(지분증권)", "rcept_dt": "20260702", "rcept_no": "2"},
    ]
    result = build_filing_lineage(rows)
    assert result[0]["rcept_no"] == "2"
    assert result[0]["filing_lineage"]["is_final"] is True
    assert result[0]["filing_lineage"]["receipt_nos"] == ["1", "2"]


class _HttpContext:
    def __init__(self, *, json_value=None, response=None):
        self.json_value = json_value
        self.response = response
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def get_json(self, url, **kwargs):
        self.calls.append(("json", url, kwargs))
        return self.json_value

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response


def _zip_bytes(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, value in files:
            if value is None:
                archive.writestr(name, "")
            else:
                archive.writestr(name, value)
    return buffer.getvalue()


def test_dart_constructor_uses_config_and_requires_key(monkeypatch, tmp_path):
    settings = SimpleNamespace(dart_api_key="", http_timeout_sec=7, data_dir=tmp_path)
    monkeypatch.setattr("app.collectors.dart.get_settings", lambda: settings)
    monkeypatch.setattr("app.collectors.dart.load_yaml_config", lambda _name: {"sources": {}})
    with pytest.raises(Exception, match="DART_API_KEY"):
        DartCollector()

    collector = DartCollector(api_key="key", timeout=2)
    assert collector.api_key == "key"
    assert collector.timeout == 2
    assert collector.request_interval_sec == 0
    assert collector.corp_code_cache_hours == 24


def test_get_json_owned_provided_and_error_status(monkeypatch):
    collector = _collector_without_init()
    owned = _HttpContext(json_value={"status": "000", "value": 1})
    monkeypatch.setattr("app.collectors.dart.ResilientHttpClient", lambda **_kwargs: owned)
    assert collector._get_json("x.json", {"a": 1})["value"] == 1
    assert owned.calls[0][2]["params"]["crtfc_key"] == "test"

    supplied = _HttpContext(json_value={"status": "013"})
    assert collector._get_json("x.json", {}, client=supplied) == {"status": "013"}
    supplied.json_value = {"status": "999", "message": "bad"}
    with pytest.raises(ExternalSourceError, match="999"):
        collector._get_json("x.json", {}, client=supplied)


def test_fetch_corp_codes_reads_cache_or_downloads_zip(monkeypatch, tmp_path):
    collector = _collector_without_init()
    cached = [{"corp_code": "cached"}]
    monkeypatch.setattr(collector, "_read_corp_code_cache", lambda: cached)
    assert collector.fetch_corp_codes() is cached

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    collector.corp_code_cache_path = raw_dir / "dart_corp_codes.json"
    monkeypatch.setattr(collector, "_read_corp_code_cache", lambda: None)
    xml = b"""<result><list><corp_code>1</corp_code><corp_name>A</corp_name>
    <corp_eng_name>Alpha</corp_eng_name><stock_code>123456</stock_code>
    <modify_date>20260720</modify_date></list></result>"""
    http = _HttpContext(response=SimpleNamespace(content=_zip_bytes([("CORPCODE.xml", xml)])))
    monkeypatch.setattr("app.collectors.dart.ResilientHttpClient", lambda **_kwargs: http)
    rows = collector.fetch_corp_codes()
    assert rows == [
        {
            "corp_code": "1",
            "corp_name": "A",
            "corp_eng_name": "Alpha",
            "stock_code": "123456",
            "modify_date": "20260720",
        }
    ]
    assert json.loads(collector.corp_code_cache_path.read_text()) == rows


def test_corp_code_cache_handles_absent_stale_invalid_and_non_list(tmp_path, monkeypatch):
    collector = _collector_without_init()
    collector.corp_code_cache_hours = 1
    path = tmp_path / "cache.json"
    collector.corp_code_cache_path = path
    assert collector._read_corp_code_cache() is None

    path.write_text("[]")
    old = time.time() - 7200
    os.utime(path, (old, old))
    assert collector._read_corp_code_cache() is None

    path.write_text("not json")
    assert collector._read_corp_code_cache() is None
    path.write_text("{}")
    assert collector._read_corp_code_cache() is None
    path.write_text('[{"corp_code": "1"}]')
    assert collector._read_corp_code_cache() == [{"corp_code": "1"}]

    class BrokenPath:
        def exists(self):
            return True

        def stat(self):
            return SimpleNamespace(st_mtime=time.time())

        def read_text(self, **_kwargs):
            raise OSError("broken")

    collector.corp_code_cache_path = BrokenPath()
    assert collector._read_corp_code_cache() is None


def test_find_corp_fetches_rows_finds_fuzzy_and_handles_missing(monkeypatch):
    collector = _collector_without_init()
    rows = [{"corp_name": "유일한회사", "corp_code": "1", "stock_code": ""}]
    monkeypatch.setattr(collector, "fetch_corp_codes", lambda: rows)
    assert collector.find_corp("유일한") == rows[0]
    assert collector.find_corp_code("없는", rows) is None
    assert collector.find_corp("유일한회사", rows, stock_code="없는종목") == rows[0]


def test_search_filings_without_corp_and_empty_page(monkeypatch):
    collector = _collector_without_init()
    http = _HttpContext()
    captured = []
    monkeypatch.setattr("app.collectors.dart.ResilientHttpClient", lambda **_kwargs: http)

    def fake_get(_path, params, client=None):
        captured.append(params)
        return {"status": "013", "list": []}

    monkeypatch.setattr(collector, "_get_json", fake_get)
    assert collector.search_filings() == []
    assert "corp_code" not in captured[0]


def test_search_filings_enforces_page_limit(monkeypatch):
    collector = _collector_without_init()
    http = _HttpContext()
    monkeypatch.setattr("app.collectors.dart.ResilientHttpClient", lambda **_kwargs: http)
    monkeypatch.setattr(
        collector,
        "_get_json",
        lambda *_args, **_kwargs: {"total_count": 1000, "list": [{"rcept_no": "x"}]},
    )
    with pytest.raises(ExternalSourceError, match="exceeded 100"):
        collector.search_filings()


def test_search_ipo_related_filings_filters_and_builds_lineage(monkeypatch):
    collector = _collector_without_init()
    monkeypatch.setattr(
        collector,
        "search_filings",
        lambda **_kwargs: [
            {"report_nm": "사업보고서", "rcept_dt": "1", "rcept_no": "1"},
            {"report_nm": "정정 투자설명서", "rcept_dt": "2", "rcept_no": "2"},
        ],
    )
    rows = collector.search_ipo_related_filings("corp")
    assert [row["rcept_no"] for row in rows] == ["2"]


@pytest.mark.parametrize("error_text", ["사용한도 초과", "정의되지 않은 오류"])
def test_fetch_document_rejects_dart_error_payload(monkeypatch, error_text):
    collector = _collector_without_init()
    response = SimpleNamespace(content=error_text.encode(), text=error_text)
    monkeypatch.setattr(
        "app.collectors.dart.ResilientHttpClient",
        lambda **_kwargs: _HttpContext(response=response),
    )
    with pytest.raises(ExternalSourceError, match="error payload"):
        collector.fetch_document_text("1")


def test_fetch_document_handles_plain_and_zipped_tables(monkeypatch):
    collector = _collector_without_init()
    plain = "<html><table><tr><th>항목</th><td>값</td></tr><tr></tr></table></html>"
    response = SimpleNamespace(content=plain.encode(), text=plain)
    monkeypatch.setattr(
        "app.collectors.dart.ResilientHttpClient",
        lambda **_kwargs: _HttpContext(response=response),
    )
    assert "항목 | 값" in collector.fetch_document_text("1")

    zipped = _zip_bytes([("a.xml", "<p>첫째</p>"), ("b.xml", "<p>둘째</p>")])
    response = SimpleNamespace(content=zipped, text="")
    assert "첫째" in collector.fetch_document_text("2")
    assert "둘째" in collector.fetch_document_text("2")


def test_financial_collection_without_fallback_and_custom_division(monkeypatch):
    collector = _collector_without_init(DartFinancialCollector)
    calls = []

    def fake_get(_path, params, client=None):
        calls.append(params)
        return {"list": [{"account_nm": "매출", "thstrm_amount": "1"}]}

    monkeypatch.setattr(collector, "_get_json", fake_get)
    rows = collector.fetch_single_company_financials("corp", [2024, 2025], fs_div="OFS")
    assert [row["fiscal_year"] for row in rows] == [2024, 2025]
    assert {row["fs_div"] for row in rows} == {"OFS"}
    assert len(calls) == 2


def test_financial_summary_covers_accounts_and_missing_states():
    rows = [
        {"bsns_year": "2019", "account_id": "ifrs-full_Revenue", "thstrm_amount": "100"},
        {"fiscal_year": 2019, "account_nm": "영업이익", "thstrm_amount": "10"},
        {"fiscal_year": 2019, "account_nm": "당기순이익", "thstrm_amount": "5"},
        {"fiscal_year": 2019, "account_nm": "자산총계", "thstrm_amount": "50"},
        {"fiscal_year": 2019, "account_nm": "부채총계", "thstrm_amount": "20"},
        {"fiscal_year": 2019, "account_nm": "자본총계", "thstrm_amount": "25"},
        {"fiscal_year": 2019, "account_nm": "영업활동 현금흐름", "thstrm_amount": "-1"},
        {"fiscal_year": 2019, "account_nm": "무관", "thstrm_amount": "3"},
        {"fiscal_year": 2019, "account_nm": "무효", "thstrm_amount": "-"},
        {"fiscal_year": 2020, "account_nm": "영업수익", "thstrm_amount": "200"},
        {"fiscal_year": 2020, "account_nm": "분기순이익", "thstrm_amount": "-2"},
        {"fiscal_year": 2021, "account_nm": "영업이익", "thstrm_amount": "1"},
        {"fiscal_year": 2022, "account_nm": "매출액", "thstrm_amount": "0"},
        {"fiscal_year": 2023, "account_nm": "매출액", "thstrm_amount": "10"},
    ]
    result = summarize_financial_rows(rows)
    by_year = {row["fiscal_year"]: row for row in result}
    assert by_year[2019]["operating_margin"] == 0.1
    assert by_year[2019]["net_margin"] == 0.05
    assert by_year[2019]["debt_ratio"] == 0.8
    assert by_year[2019]["roe"] == 0.2
    assert by_year[2019]["roa"] == 0.1
    assert by_year[2019]["has_negative_ocf"] is True
    assert by_year[2020]["revenue_yoy_status"] == "calculated"
    assert by_year[2021]["revenue_yoy_status"] == "missing_current"
    assert by_year[2023]["revenue_yoy_status"] == "prior_zero"


def test_dart_private_parsers_cover_empty_invalid_and_boundary_values():
    root = ElementTree.fromstring("<list><present>x</present><empty /></list>")
    assert _xml_text(root, "present") == "x"
    assert _xml_text(root, "empty") == ""
    assert _xml_text(root, "missing") == ""

    assert _bytes_to_text("한글".encode("cp949")) == "한글"
    assert isinstance(_bytes_to_text(b"\xff"), str)
    assert _search_float("두번째 1,234.5", [r"없음 (\d+)", r"두번째 ([\d,.]+)"]) == 1234.5
    assert _search_float("없음", [r"값 (\d+)"]) is None
    assert _search_int("두번째 1,234", [r"없음 (\d+)", r"두번째 ([\d,]+)"]) == 1234
    assert _search_int("없음", [r"값 (\d+)"]) is None
    assert _evidence_excerpt("abc", 999) == ""
    assert _evidence_excerpt("value 123 end", 123, radius=100) == "value 123 end"

    assert extract_demand_forecast_fields("관련 값 없음")["evidence"] == {}
    boundary = extract_demand_forecast_fields("밴드 상단 초과 1%")
    assert boundary["above_band_participation_ratio"] == 1
    parsed = _extract_table_values(
        "무관한 줄\n| 경쟁률 | 잘못된 값 |\n| 경쟁률 | 10:1 |\n| 경쟁률 | 20:1 |"
    )
    assert parsed["institutional_competition_ratio"]["value"] == 10


def test_safe_zip_rejects_member_count_size_and_directory_only():
    empty = _zip_bytes([])
    with pytest.raises(ExternalSourceError, match="member count"):
        _safe_zip_members(empty)

    two = _zip_bytes([("a", "1"), ("b", "2")])
    with pytest.raises(ExternalSourceError, match="member count"):
        _safe_zip_members(two, max_members=1)
    with pytest.raises(ExternalSourceError, match="too large"):
        _safe_zip_members(two, max_total_bytes=1)

    directory_only = _zip_bytes([("folder/", None)])
    with pytest.raises(ExternalSourceError, match="no files"):
        _safe_zip_members(directory_only)


def test_empty_filing_lineage_is_empty():
    assert build_filing_lineage([]) == []
