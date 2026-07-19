from decimal import Decimal

from app.services.public_financial import parse_public_financial_summary


def _table(
    *,
    heading: str = "구분",
    periods: tuple[str, ...] = ("2025년", "2026년 1분기"),
    revenue: tuple[str, ...] = ("1,000", "400"),
    operating: tuple[str, ...] = ("(20)", "30"),
    net_income: tuple[str, ...] = ("-30", "25"),
    revenue_label: str = "매출액",
    unit: str = "백만원",
    extra: str = "",
) -> str:
    def cells(values: tuple[str, ...]) -> str:
        return "".join(f"<td>{value}</td>" for value in values)

    return f"""
    <p>(단위: {unit})</p>
    <table>
      <tr><th>{heading}</th>{cells(periods)}</tr>
      <tr><td>{revenue_label}</td>{cells(revenue)}</tr>
      <tr><td>영업이익(손실)</td>{cells(operating)}</tr>
      <tr><td>당기순이익(손실)</td>{cells(net_income)}</tr>
      {extra}
    </table>
    """


def test_parse_public_financial_summary_selects_latest_actual_and_negative_values():
    summary = parse_public_financial_summary(_table())

    assert summary is not None
    assert summary.period == "2026년 1분기"
    assert summary.revenue == Decimal("400")
    assert summary.operating_profit == Decimal("30")
    assert summary.net_income == Decimal("25")
    assert summary.unit == "백만원"
    assert summary.statement_basis is None


def test_parse_public_financial_summary_supports_operating_revenue_and_basis():
    html = _table(
        periods=("2026년 1분기", "2025년"),
        revenue=("2,572", "15,954"),
        operating=("(1,440)", "(663)"),
        net_income=("(1,464)", "(2,100)"),
        revenue_label="영업수익",
        unit="천원",
        extra="<tr><td colspan='3'>K-IFRS 연결 기준</td></tr>",
    )

    summary = parse_public_financial_summary(html)

    assert summary is not None
    assert summary.period == "2026년 1분기"
    assert summary.operating_profit == Decimal("-1440")
    assert summary.net_income == Decimal("-1464")
    assert summary.unit == "천원"
    assert summary.statement_basis == "연결"


def test_parse_public_financial_summary_rejects_forecasts_and_uses_actual_copy():
    forecast = _table(
        periods=("2026년[E]", "2027년 추정"),
        revenue=("900", "1000"),
        operating=("90", "100"),
        net_income=("80", "90"),
    )
    actual = _table(
        periods=("2024년", "2025년"),
        revenue=("100", "200"),
        operating=("10", "20"),
        net_income=("8", "18"),
    )

    summary = parse_public_financial_summary(forecast + actual)

    assert summary is not None
    assert summary.period == "2025년"
    assert summary.revenue == Decimal("200")


def test_parse_public_financial_summary_rejects_peer_and_incomplete_tables():
    peer = _table(heading="회사명")
    missing_metric = """
    <p>(단위: 원)</p><table>
      <tr><th>구분</th><th>2026년</th></tr>
      <tr><td>매출액 증가율</td><td>10%</td></tr>
      <tr><td>영업이익률</td><td>5%</td></tr>
      <tr><td>당기순이익률</td><td>4%</td></tr>
    </table>
    """

    assert parse_public_financial_summary(peer + missing_metric) is None


def test_parse_public_financial_summary_rejects_missing_unit_or_value():
    without_unit = _table().replace("<p>(단위: 백만원)</p>", "")
    missing_value = _table(net_income=("10", "N/A"))
    nonnumeric_value = _table(net_income=("10", "확인중"))

    assert parse_public_financial_summary(without_unit) is None
    assert parse_public_financial_summary(missing_value) is None
    assert parse_public_financial_summary(nonnumeric_value) is None


def test_parse_public_financial_summary_handles_empty_and_misaligned_markup():
    misaligned = """
    <p>(단위: 원)</p><table>
      <tr><th>계정</th><th>2026년</th><th>2025년</th></tr>
      <tr><td>매출액</td><td>100</td></tr>
      <tr><td>영업손실</td><td>(10)</td></tr>
      <tr><td>당기순손실</td><td>(20)</td></tr>
    </table>
    """

    assert parse_public_financial_summary("") is None
    assert parse_public_financial_summary("<table><tr><td>구분</td></tr></table>") is None
    assert parse_public_financial_summary(misaligned) is not None

    latest_column_missing = misaligned.replace(
        "<th>2026년</th><th>2025년</th>", "<th>2025년</th><th>2026년</th>"
    )
    assert parse_public_financial_summary(latest_column_missing) is None


def test_parse_public_financial_summary_ignores_nonperiod_columns():
    html = _table(
        periods=("비고", "2026년 3월"),
        revenue=("-", "300"),
        operating=("-", "30"),
        net_income=("-", "20"),
    )

    summary = parse_public_financial_summary(html)

    assert summary is not None
    assert summary.period == "2026년 3월"


def test_parse_public_financial_summary_understands_year_do_quarter_and_date_range():
    year_do = _table(periods=("2025년", "2026년도 1분기"))
    date_range = _table(periods=("2025년", "(2026.01.01~2026.03.31)"))

    assert parse_public_financial_summary(year_do).period == "2026년도 1분기"
    assert parse_public_financial_summary(date_range).period == "(2026.01.01~2026.03.31)"


def test_parse_public_financial_summary_accepts_separate_statement_label():
    summary = parse_public_financial_summary(
        _table(extra="<tr><td colspan='3'>K-IFRS 별도 기준</td></tr>")
    )

    assert summary is not None
    assert summary.statement_basis == "별도"
