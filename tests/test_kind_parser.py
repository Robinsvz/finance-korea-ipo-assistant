from datetime import date

from app.collectors.kind import parse_kind_puboffering_html

HTML = """
<table><tbody>
<tr onclick="fnDetailView('20260223000234')">
  <td class="first" title="한국제16호스팩"><img alt="코스닥"/>한국제16호스팩</td>
  <td class="txc">2026-05-26</td>
  <td class="txc">&nbsp;&nbsp;&nbsp;2026-06-17<br/> ~ 2026-06-18</td>
  <td class="txc">&nbsp;&nbsp;&nbsp;2026-06-22<br/> ~ 2026-06-23</td>
  <td class="txc">2026-06-25</td>
  <td class="txr">2,000</td>
  <td class="txr">11,000</td>
  <td class="txc">2026-06-30</td>
  <td class="txl">한국투자증권(주),엔에이치투자증권(주)</td>
</tr>
</tbody></table>
"""


def test_parse_kind_puboffering_html():
    records = parse_kind_puboffering_html(HTML)
    assert len(records) == 1
    record = records[0]
    assert record.corp_name == "한국제16호스팩"
    assert record.market == "코스닥"
    assert record.kind_biz_no == "20260223000234"
    assert record.report_filed_date == date(2026, 5, 26)
    assert record.demand_start_date == date(2026, 6, 17)
    assert record.demand_end_date == date(2026, 6, 18)
    assert record.subscription_start_date == date(2026, 6, 22)
    assert record.subscription_end_date == date(2026, 6, 23)
    assert record.offer_price_final == 2000
    assert record.offer_amount_million == 11000
    assert record.payment_date == date(2026, 6, 25)
    assert record.listing_date == date(2026, 6, 30)
    assert record.underwriters == ["한국투자증권(주)", "엔에이치투자증권(주)"]
