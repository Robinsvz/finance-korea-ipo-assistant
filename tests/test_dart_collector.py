import io
import zipfile

import pytest

from app.collectors.dart import (
    DartCollector,
    DartFinancialCollector,
    _safe_zip_members,
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
