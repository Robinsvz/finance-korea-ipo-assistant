from datetime import datetime, timezone
from email.utils import format_datetime

from app.collectors.naver import (
    NaverDataLabCollector,
    NaverNewsCollector,
    _dedupe_articles,
    _is_relevant_article,
    _keyword_article_count,
    _major_media_count,
    _normalize_url,
    _risk_event_article_count,
)


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
