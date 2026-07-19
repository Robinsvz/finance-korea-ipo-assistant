from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import pytest

from app.collectors.naver import (
    NaverClient,
    NaverDataLabCollector,
    NaverNewsCollector,
    _clean_news_item,
    _dedupe_articles,
    _is_relevant_article,
    _keyword_article_count,
    _keyword_count,
    _major_media_count,
    _normalize_url,
    _risk_event_article_count,
    _top_keywords,
)
from app.exceptions import ExternalSourceError, MissingCredentialError


def _item(title, link="https://example.com/a", description="공모주 청약", pub_date=None):
    return {
        "title": title,
        "description": description,
        "originallink": link,
        "link": link,
        "pubDate": format_datetime(pub_date or datetime.now(timezone.utc)),
    }


class DummyContext:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


class FakeNewsClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = 0

    def http_client(self):
        return DummyContext()

    def search_news(self, **_kwargs):
        result = self.pages[self.calls]
        self.calls += 1
        return result


class FakeDataLabClient:
    def datalab_search(self, *_args, **_kwargs):
        return {
            "results": [
                {"data": [{"period": f"2026-07-{day:02d}", "ratio": day} for day in range(1, 15)]}
            ]
        }


def test_relevance_requires_company_and_ipo_context():
    assert _is_relevant_article({"title": "ABC 공모주", "description": ""}, "ABC")
    assert not _is_relevant_article({"title": "ABC 신제품", "description": ""}, "ABC")
    assert not _is_relevant_article({"title": "XYZ 공모주", "description": ""}, "ABC")


def test_dedupe_normalizes_tracking_query():
    items = [
        {"title": "A", "originallink": "https://a.com/x?utm=1", "link": ""},
        {"title": "A 재배포", "originallink": "https://a.com/x?utm=2", "link": ""},
    ]
    assert len(_dedupe_articles(items)) == 1


def test_dedupe_removes_republished_same_title_on_other_domain():
    items = [
        {"title": "ABC 공모주 청약 흥행", "originallink": "https://a.com/x", "link": ""},
        {"title": "ABC 공모주 청약 흥행", "originallink": "https://b.com/y", "link": ""},
    ]
    assert len(_dedupe_articles(items)) == 1


def test_url_normalization_removes_query_and_trailing_slash():
    assert _normalize_url("HTTPS://Example.com/a/?x=1") == "https://example.com/a"


def test_major_media_uses_hostname_not_substring():
    items = [
        {"originallink": "https://news.mk.co.kr/a"},
        {"originallink": "https://evil.test/mk.co.kr/a"},
    ]
    assert _major_media_count(items) == 1


def test_keyword_events_count_once_per_article():
    items = [{"title": "소송 소송", "description": "과징금"}]
    assert _keyword_article_count(items, ["소송", "과징금"]) == 1


def test_risk_events_ignore_explicit_negation():
    items = [
        {"title": "소송 없음 확인", "description": ""},
        {"title": "과징금 부과", "description": ""},
    ]
    assert _risk_event_article_count(items, ["소송", "과징금"]) == 1


def test_news_collector_paginates_and_keeps_period_counts():
    pages = [
        {"total": 101, "items": [_item("ABC 공모주 첫 기사")]},
        {"total": 101, "items": [_item("ABC 상장 두 번째", link="https://example.com/b")]},
    ]
    client = FakeNewsClient(pages)
    result = NaverNewsCollector(client=client).collect("ABC", window_days=30)
    assert client.calls == 2
    assert result.api_total_count == 101
    assert result.total_article_count == 2
    assert result.deduped_article_count == 2


def test_news_collector_counts_all_101_in_window_articles():
    first = [
        _item(f"ABC 공모주 기사 {index}", link=f"https://example.com/{index}")
        for index in range(100)
    ]
    second = [_item("ABC 상장 기사 100", link="https://example.com/100")]
    result = NaverNewsCollector(
        client=FakeNewsClient([{"total": 101, "items": first}, {"total": 101, "items": second}])
    ).collect("ABC", window_days=30)
    assert result.api_total_count == 101
    assert result.total_article_count == 101
    assert result.deduped_article_count == 101


def test_news_collector_filters_irrelevant_articles():
    pages = [{"total": 2, "items": [_item("ABC 신제품", description="출시"), _item("XYZ 공모주")]}]
    result = NaverNewsCollector(client=FakeNewsClient(pages)).collect("ABC")
    assert result.deduped_article_count == 0


def test_news_growth_with_no_previous_articles_is_bounded():
    pages = [{"total": 1, "items": [_item("ABC 공모주")]}]
    result = NaverNewsCollector(client=FakeNewsClient(pages)).collect("ABC")
    assert result.article_growth_rate_7d == 1.0


def test_datalab_baseline_excludes_recent_seven_days():
    result = NaverDataLabCollector(client=FakeDataLabClient()).collect(
        "ABC", ["ABC"], window_days=14
    )
    assert result.baseline_avg_index == 4
    assert result.recent_avg_index == 11
    assert result.trend_growth_rate == 1.75


def test_datalab_zero_ratio_is_one_for_empty_series():
    class EmptyClient:
        def datalab_search(self, *_args, **_kwargs):
            return {"results": [{"data": []}]}

    result = NaverDataLabCollector(client=EmptyClient()).collect("ABC", ["ABC"])
    assert result.zero_data_ratio == 1.0
    assert result.trend_growth_rate == 0.0


class FakeHttpClient(DummyContext):
    def __init__(self, *, get_data=None, post_data=None):
        self.get_data = get_data
        self.post_data = post_data
        self.calls = []

    def get_json(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.get_data

    def post_json(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.post_data


def test_naver_client_constructor_headers_and_http_factory(monkeypatch):
    settings = SimpleNamespace(
        naver_client_id="",
        naver_client_secret="",
        http_timeout_sec=3,
    )
    monkeypatch.setattr("app.collectors.naver.get_settings", lambda: settings)
    monkeypatch.setattr("app.collectors.naver.load_yaml_config", lambda _name: {"sources": {}})
    with pytest.raises(MissingCredentialError):
        NaverClient()

    client = NaverClient("id", "secret", timeout=1)
    assert client.headers == {
        "X-Naver-Client-Id": "id",
        "X-Naver-Client-Secret": "secret",
    }
    http_client = client.http_client()
    assert http_client.min_interval_sec == 0
    http_client.close()


def test_naver_news_api_validation_owned_supplied_and_error(monkeypatch):
    client = object.__new__(NaverClient)
    client.client_id = "id"
    client.client_secret = "secret"
    client.timeout = 1
    client.request_interval_sec = 0
    with pytest.raises(ValueError, match="outside API limits"):
        client.search_news("A", display=0)
    with pytest.raises(ValueError, match="outside API limits"):
        client.search_news("A", start=1001)

    owned = FakeHttpClient(get_data={"items": []})
    monkeypatch.setattr(client, "http_client", lambda: owned)
    assert client.search_news("A") == {"items": []}
    assert owned.calls[0][0] == "GET"

    supplied = FakeHttpClient(get_data={"errorCode": "E", "errorMessage": "bad"})
    with pytest.raises(ExternalSourceError, match="Naver News error E"):
        client.search_news("A", client=supplied)


def test_naver_datalab_validation_payload_keyword_limit_and_error(monkeypatch):
    client = object.__new__(NaverClient)
    client.client_id = "id"
    client.client_secret = "secret"
    client.timeout = 1
    client.request_interval_sec = 0
    with pytest.raises(ValueError, match="at least one"):
        client.datalab_search("A", [" ", ""], date(2026, 1, 1), date(2026, 1, 2))
    with pytest.raises(ValueError, match="must not be after"):
        client.datalab_search("A", ["A"], date(2026, 1, 2), date(2026, 1, 1))

    http = FakeHttpClient(post_data={"results": []})
    monkeypatch.setattr(client, "http_client", lambda: http)
    keywords = [f" k{index} " for index in range(25)]
    assert client.datalab_search("A", keywords, date(2026, 1, 1), date(2026, 1, 2)) == {
        "results": []
    }
    sent_keywords = http.calls[0][2]["json"]["keywordGroups"][0]["keywords"]
    assert len(sent_keywords) == 20
    assert sent_keywords[0] == "k0"

    http.post_data = {"errorCode": "E", "errorMessage": "bad"}
    with pytest.raises(ExternalSourceError, match="DataLab error E"):
        client.datalab_search("A", ["A"], date(2026, 1, 1), date(2026, 1, 2))


def test_news_collector_default_client_and_short_window(monkeypatch):
    fake = FakeNewsClient([{"total": 0, "items": []}])
    monkeypatch.setattr("app.collectors.naver.NaverClient", lambda: fake)
    collector = NaverNewsCollector()
    assert collector.client is fake
    with pytest.raises(ValueError, match="at least 14"):
        collector.collect("ABC", window_days=13)


def test_news_collection_stops_on_empty_and_old_page(monkeypatch):
    today = date(2026, 7, 20)
    monkeypatch.setattr("app.collectors.naver.local_today", lambda: today)
    empty_client = FakeNewsClient([{"total": 1000, "items": []}])
    assert NaverNewsCollector(client=empty_client).collect("ABC").deduped_article_count == 0
    assert empty_client.calls == 1

    old = datetime.combine(today - timedelta(days=40), datetime.min.time(), tzinfo=timezone.utc)
    old_client = FakeNewsClient([{"total": 1000, "items": [_item("ABC 공모주", pub_date=old)]}])
    result = NaverNewsCollector(client=old_client).collect("ABC", window_days=30)
    assert result.deduped_article_count == 0
    assert old_client.calls == 1


def test_news_collection_stops_at_naver_thousand_result_cap(monkeypatch):
    today = date(2026, 7, 20)
    monkeypatch.setattr("app.collectors.naver.local_today", lambda: today)
    pages = [
        {
            "total": 2000,
            "items": [
                _item(
                    f"ABC 공모주 {index}",
                    link=f"https://example.com/{index}",
                    pub_date=datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc),
                )
            ],
        }
        for index in range(10)
    ]
    client = FakeNewsClient(pages)
    result = NaverNewsCollector(client=client).collect("ABC")
    assert client.calls == 10
    assert result.deduped_article_count == 10


def test_datalab_short_window_small_series_and_positive_zero_baseline():
    with pytest.raises(ValueError, match="at least 8"):
        NaverDataLabCollector(client=FakeDataLabClient()).collect("A", ["A"], window_days=7)

    class SmallClient:
        def datalab_search(self, *_args, **_kwargs):
            return {"results": [{"data": [{"ratio": 0}, {"ratio": 3}]}]}

    result = NaverDataLabCollector(client=SmallClient()).collect("A", ["A"], window_days=8)
    assert result.baseline_avg_index == 1.5
    assert result.peak_index == 3
    assert result.trend_acceleration == 0


def test_news_helpers_handle_invalid_dates_urls_empty_identity_and_keywords():
    cleaned = _clean_news_item(
        {
            "title": "&lt;b&gt;ABC&lt;/b&gt; <b>공모</b>",
            "description": "설명",
            "pubDate": "invalid",
        }
    )
    assert cleaned["title"] == "ABC 공모"
    assert cleaned["pub_date"] is None

    cleaned_without_date = _clean_news_item({"title": "A"})
    assert cleaned_without_date["pub_date"] is None

    items = [
        {"title": "", "originallink": "", "link": ""},
        {"title": "", "originallink": "https://example.com/url-only", "link": ""},
        {"title": "Only title", "originallink": "", "link": ""},
        {"title": "Another", "originallink": "", "link": ""},
        {"title": "Another", "originallink": "", "link": ""},
    ]
    deduped = _dedupe_articles(items)
    assert [item["title"] for item in deduped] == ["", "Only title", "Another"]
    assert _normalize_url("http://[invalid") == "http://[invalid"
    assert _major_media_count([{"originallink": "http://[invalid"}]) == 0
    assert _keyword_count("공모 공모 상장", ["공모", "상장"]) == 3
    assert _risk_event_article_count([{"title": "무관", "description": ""}], ["소송"]) == 0
    assert _top_keywords("공모 ABC ABC 상장 테스트")[0] == {"keyword": "ABC", "count": 2}
