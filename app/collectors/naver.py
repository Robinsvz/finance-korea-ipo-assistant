from __future__ import annotations

import html
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterable, Optional
from urllib.parse import urlsplit, urlunsplit

from app.collectors.http import ResilientHttpClient
from app.config import get_settings, load_yaml_config
from app.exceptions import ExternalSourceError, MissingCredentialError
from app.services.clock import local_today

NAVER_NEWS_URL = "https://openapi.naver.com/v1/search/news.json"
NAVER_DATALAB_URL = "https://openapi.naver.com/v1/datalab/search"
IPO_CONTEXT = ("공모", "공모주", "상장", "청약", "수요예측", "IPO")


@dataclass
class NewsMetricsPayload:
    window_start: date
    window_end: date
    source: str
    api_total_count: int
    total_article_count: int
    deduped_article_count: int
    major_media_count: int
    recent_7d_article_count: int
    previous_7d_article_count: int
    article_growth_rate_7d: float
    positive_keyword_count: int
    negative_keyword_count: int
    risk_keyword_count: int
    top_keywords_json: list[dict[str, Any]]
    top_articles_json: list[dict[str, Any]]
    summary: str


@dataclass
class SearchTrendPayload:
    source: str
    keyword_group: str
    window_start: date
    window_end: date
    baseline_avg_index: float
    recent_avg_index: float
    peak_index: float
    trend_growth_rate: float
    trend_acceleration: float
    zero_data_ratio: float
    raw_series_json: list[dict[str, Any]]


class NaverClient:
    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        settings = get_settings()
        self.client_id = client_id or settings.naver_client_id
        self.client_secret = client_secret or settings.naver_client_secret
        self.timeout = timeout or settings.http_timeout_sec
        if not self.client_id or not self.client_secret:
            raise MissingCredentialError("NAVER_CLIENT_ID and NAVER_CLIENT_SECRET are required")
        sources = load_yaml_config("sources.yaml").get("sources") or {}
        self.request_interval_sec = max(
            float((sources.get("naver_news") or {}).get("request_interval_sec", 0)),
            float((sources.get("naver_datalab") or {}).get("request_interval_sec", 0)),
        )

    @property
    def headers(self) -> Dict[str, str]:
        return {
            "X-Naver-Client-Id": self.client_id,
            "X-Naver-Client-Secret": self.client_secret,
        }

    def http_client(self) -> ResilientHttpClient:
        return ResilientHttpClient(
            timeout=self.timeout,
            headers=self.headers,
            min_interval_sec=self.request_interval_sec,
        )

    def search_news(
        self,
        query: str,
        display: int = 100,
        start: int = 1,
        sort: str = "date",
        *,
        client: Optional[ResilientHttpClient] = None,
    ) -> Dict[str, Any]:
        if not 1 <= display <= 100 or not 1 <= start <= 1000:
            raise ValueError("Naver news display/start are outside API limits")
        params = {"query": query, "display": display, "start": start, "sort": sort}
        if client is None:
            with self.http_client() as owned:
                data = owned.get_json(NAVER_NEWS_URL, source="Naver News", params=params)
        else:
            data = client.get_json(NAVER_NEWS_URL, source="Naver News", params=params)
        if "errorCode" in data:
            raise ExternalSourceError(
                f"Naver News error {data.get('errorCode')}: {data.get('errorMessage')}"
            )
        return data

    def datalab_search(
        self,
        keyword_group: str,
        keywords: list[str],
        start_date: date,
        end_date: date,
        time_unit: str = "date",
    ) -> Dict[str, Any]:
        cleaned_keywords = [keyword.strip() for keyword in keywords if keyword.strip()][:20]
        if not cleaned_keywords:
            raise ValueError("Naver DataLab requires at least one keyword")
        if start_date > end_date:
            raise ValueError("Naver DataLab start date must not be after end date")
        payload = {
            "startDate": start_date.isoformat(),
            "endDate": end_date.isoformat(),
            "timeUnit": time_unit,
            "keywordGroups": [{"groupName": keyword_group, "keywords": cleaned_keywords}],
        }
        with self.http_client() as client:
            data = client.post_json(NAVER_DATALAB_URL, source="Naver DataLab", json=payload)
        if "errorCode" in data:
            raise ExternalSourceError(
                f"Naver DataLab error {data.get('errorCode')}: {data.get('errorMessage')}"
            )
        return data


class NaverNewsCollector:
    def __init__(self, client: Optional[NaverClient] = None) -> None:
        self.client = client or NaverClient()
        scoring_rules = load_yaml_config("scoring_rules.yaml")
        risk_keywords = scoring_rules.get("risk_keywords", {})
        self.severe_keywords = risk_keywords.get("severe", [])
        self.normal_keywords = risk_keywords.get("normal", [])
        self.positive_keywords = scoring_rules.get("positive_keywords", [])
        source = (load_yaml_config("sources.yaml").get("sources") or {}).get("naver_news", {})
        self.request_interval_sec = float(source.get("request_interval_sec", 0))

    def collect(self, query: str, window_days: int = 30) -> NewsMetricsPayload:
        if window_days < 14:
            raise ValueError("News analysis window must be at least 14 days")
        today = local_today()
        window_start = today - timedelta(days=window_days - 1)
        raw_items: list[dict[str, Any]] = []
        api_total = 0
        start = 1
        display = 100
        with self.client.http_client() as http_client:
            while start <= 1000:
                data = self.client.search_news(
                    query=query,
                    display=display,
                    start=start,
                    sort="date",
                    client=http_client,
                )
                api_total = max(api_total, int(data.get("total") or 0))
                page = data.get("items", []) or []
                if not page:
                    break
                cleaned_page = [_clean_news_item(item) for item in page]
                raw_items.extend(cleaned_page)
                dated = [item["pub_date"].date() for item in cleaned_page if item["pub_date"]]
                if dated and min(dated) < window_start:
                    break
                start += display
                if start > min(api_total, 1000):
                    break

        in_window_raw = [
            item
            for item in raw_items
            if item["pub_date"] and window_start <= item["pub_date"].date() <= today
        ]
        relevant = [item for item in in_window_raw if _is_relevant_article(item, query)]
        deduped = _dedupe_articles(relevant)
        recent_start = today - timedelta(days=6)
        previous_start = today - timedelta(days=13)
        recent_count = sum(item["pub_date"].date() >= recent_start for item in deduped)
        previous_count = sum(
            previous_start <= item["pub_date"].date() < recent_start for item in deduped
        )
        growth = (
            (recent_count - previous_count) / previous_count
            if previous_count
            else float(recent_count > 0)
        )
        joined_text = " ".join(item["title"] + " " + item["description"] for item in deduped)
        positive_count = _keyword_article_count(deduped, self.positive_keywords)
        normal_risk_count = _risk_event_article_count(deduped, self.normal_keywords)
        risk_event_count = _risk_event_article_count(
            deduped,
            [*self.normal_keywords, *self.severe_keywords],
        )
        top_articles = [
            {
                "title": item["title"],
                "link": item["originallink"] or item["link"],
                "pub_date": item["pub_date"].isoformat(),
            }
            for item in deduped[:5]
        ]
        summary = (
            f"최근 {window_days}일 관련 기사 {len(deduped)}건, 최근 7일 {recent_count}건 "
            f"(직전 7일 대비 {growth:.1%})."
        )
        return NewsMetricsPayload(
            window_start=window_start,
            window_end=today,
            source="naver_news",
            api_total_count=api_total,
            total_article_count=len(relevant),
            deduped_article_count=len(deduped),
            major_media_count=_major_media_count(deduped),
            recent_7d_article_count=recent_count,
            previous_7d_article_count=previous_count,
            article_growth_rate_7d=growth,
            positive_keyword_count=positive_count,
            negative_keyword_count=normal_risk_count,
            risk_keyword_count=risk_event_count,
            top_keywords_json=_top_keywords(joined_text),
            top_articles_json=top_articles,
            summary=summary,
        )


class NaverDataLabCollector:
    def __init__(self, client: Optional[NaverClient] = None) -> None:
        self.client = client or NaverClient()

    def collect(
        self,
        keyword_group: str,
        keywords: list[str],
        window_days: int = 30,
    ) -> SearchTrendPayload:
        if window_days < 8:
            raise ValueError("Search trend window must be at least 8 days")
        end = local_today()
        start = end - timedelta(days=window_days - 1)
        data = self.client.datalab_search(keyword_group, keywords, start, end, time_unit="date")
        result = (data.get("results") or [{}])[0]
        series = result.get("data", []) or []
        ratios = [float(item.get("ratio") or 0) for item in series]
        baseline = _avg(ratios[:-7]) if len(ratios) > 7 else _avg(ratios)
        recent = _avg(ratios[-7:])
        peak = max(ratios) if ratios else 0.0
        accel = _avg(ratios[-3:]) - _avg(ratios[-7:-3]) if len(ratios) >= 7 else 0.0
        zero_ratio = sum(ratio == 0 for ratio in ratios) / len(ratios) if ratios else 1.0
        growth = (recent - baseline) / baseline if baseline > 0 else float(recent > 0)
        return SearchTrendPayload(
            source="naver_datalab",
            keyword_group=keyword_group,
            window_start=start,
            window_end=end,
            baseline_avg_index=baseline,
            recent_avg_index=recent,
            peak_index=peak,
            trend_growth_rate=growth,
            trend_acceleration=accel,
            zero_data_ratio=zero_ratio,
            raw_series_json=series,
        )


def _clean_news_item(item: Dict[str, Any]) -> Dict[str, Any]:
    title = re.sub(r"<[^>]+>", "", html.unescape(item.get("title") or ""))
    description = re.sub(r"<[^>]+>", "", html.unescape(item.get("description") or ""))
    pub_date = None
    if item.get("pubDate"):
        try:
            pub_date = parsedate_to_datetime(item["pubDate"])
        except (TypeError, ValueError):
            pass
    return {
        "title": title,
        "description": description,
        "originallink": item.get("originallink") or "",
        "link": item.get("link") or "",
        "pub_date": pub_date,
    }


def _is_relevant_article(item: Dict[str, Any], company_name: str) -> bool:
    text = (item.get("title", "") + " " + item.get("description", "")).lower()
    normalized_company = re.sub(r"\s+", "", company_name).lower()
    normalized_text = re.sub(r"\s+", "", text)
    return normalized_company in normalized_text and any(
        context.lower() in text for context in IPO_CONTEXT
    )


def _dedupe_articles(items: Iterable[Dict[str, Any]]) -> list[Dict[str, Any]]:
    seen_urls = set()
    seen_titles = set()
    result = []
    for item in items:
        url = item.get("originallink") or item.get("link") or ""
        normalized_url = _normalize_url(url)
        normalized_title = re.sub(r"\W+", "", item.get("title", "")).lower()
        if not normalized_url and not normalized_title:
            continue
        if (normalized_url and normalized_url in seen_urls) or (
            normalized_title and normalized_title in seen_titles
        ):
            continue
        if normalized_url:
            seen_urls.add(normalized_url)
        if normalized_title:
            seen_titles.add(normalized_title)
        result.append(item)
    return result


def _normalize_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", "")
    )


def _top_keywords(text: str, limit: int = 10) -> list[dict[str, Any]]:
    tokens = re.findall(r"[가-힣A-Za-z0-9]{2,}", text)
    stopwords = {"관련", "공모", "상장", "기업", "기자", "뉴스", "이번", "위해", "통해"}
    counts = Counter(token for token in tokens if token not in stopwords)
    return [{"keyword": key, "count": value} for key, value in counts.most_common(limit)]


def _keyword_count(text: str, keywords: Iterable[str]) -> int:
    return sum(text.count(keyword) for keyword in keywords)


def _keyword_article_count(items: Iterable[Dict[str, Any]], keywords: Iterable[str]) -> int:
    keyword_values = tuple(keywords)
    return sum(
        any(
            keyword in (item.get("title", "") + " " + item.get("description", ""))
            for keyword in keyword_values
        )
        for item in items
    )


def _risk_event_article_count(items: Iterable[Dict[str, Any]], keywords: Iterable[str]) -> int:
    keyword_values = tuple(keywords)
    return sum(
        any(_risk_keyword_is_asserted(text, keyword) for keyword in keyword_values)
        for item in items
        for text in [item.get("title", "") + " " + item.get("description", "")]
    )


def _risk_keyword_is_asserted(text: str, keyword: str) -> bool:
    for match in re.finditer(re.escape(keyword), text):
        context = text[max(0, match.start() - 12) : min(len(text), match.end() + 15)]
        if not re.search(
            rf"(?:{re.escape(keyword)}\s*(?:없|아니|무관|해소|기각|취하)|"
            rf"(?:없|무|비)\s*{re.escape(keyword)})",
            context,
        ):
            return True
    return False


def _major_media_count(items: Iterable[Dict[str, Any]]) -> int:
    major_domains = {
        "yna.co.kr",
        "mk.co.kr",
        "hankyung.com",
        "chosun.com",
        "joongang.co.kr",
        "donga.com",
    }
    count = 0
    for item in items:
        link = item.get("originallink") or item.get("link") or ""
        try:
            hostname = (urlsplit(link).hostname or "").lower()
        except ValueError:
            continue
        if any(hostname == domain or hostname.endswith("." + domain) for domain in major_domains):
            count += 1
    return count


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0
