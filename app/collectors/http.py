from __future__ import annotations

import time
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Optional

import httpx

from app.config import get_settings
from app.exceptions import ExternalSourceError

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class ResilientHttpClient:
    def __init__(
        self,
        *,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        backoff_base_sec: Optional[float] = None,
        headers: Optional[dict[str, str]] = None,
        transport: Optional[httpx.BaseTransport] = None,
        sleep: Callable[[float], None] = time.sleep,
        min_interval_sec: float = 0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        settings = get_settings()
        self.max_retries = settings.http_max_retries if max_retries is None else max_retries
        self.backoff_base_sec = (
            settings.http_backoff_base_sec if backoff_base_sec is None else backoff_base_sec
        )
        self.sleep = sleep
        if self.max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if min_interval_sec < 0:
            raise ValueError("min_interval_sec must be non-negative")
        self.min_interval_sec = min_interval_sec
        self.clock = clock
        self._last_request_at: Optional[float] = None
        self.client = httpx.Client(
            timeout=timeout or settings.http_timeout_sec,
            follow_redirects=True,
            headers=headers,
            transport=transport,
        )

    def __enter__(self) -> "ResilientHttpClient":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def close(self) -> None:
        self.client.close()

    def request(self, method: str, url: str, *, source: str, **kwargs: Any) -> httpx.Response:
        attempt = 0
        while True:
            self._pace()
            try:
                response = self.client.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                if attempt >= self.max_retries:
                    raise ExternalSourceError(
                        f"{source} request failed after retries: {type(exc).__name__}"
                    ) from exc
                self.sleep(self._delay(attempt, None))
                attempt += 1
                continue
            if response.status_code < 400:
                return response
            if response.status_code not in RETRYABLE_STATUS or attempt >= self.max_retries:
                raise ExternalSourceError(f"{source} HTTP {response.status_code}")
            self.sleep(self._delay(attempt, response.headers.get("Retry-After")))
            attempt += 1

    def get_json(self, url: str, *, source: str, **kwargs: Any) -> dict[str, Any]:
        response = self.request("GET", url, source=source, **kwargs)
        return self._json(response, source)

    def post_json(self, url: str, *, source: str, **kwargs: Any) -> dict[str, Any]:
        response = self.request("POST", url, source=source, **kwargs)
        return self._json(response, source)

    def _delay(self, attempt: int, retry_after: Optional[str]) -> float:
        if retry_after:
            try:
                return min(max(float(retry_after), 0), 30)
            except ValueError:
                try:
                    parsed = parsedate_to_datetime(retry_after)
                    return min(max(parsed.timestamp() - time.time(), 0), 30)
                except (TypeError, ValueError):
                    pass
        jitter = (attempt + 1) * 0.037
        return min(self.backoff_base_sec * (2**attempt) + jitter, 30)

    def _pace(self) -> None:
        now = self.clock()
        if self._last_request_at is not None:
            wait = self.min_interval_sec - (now - self._last_request_at)
            if wait > 0:
                self.sleep(wait)
                now += wait
        self._last_request_at = now

    @staticmethod
    def _json(response: httpx.Response, source: str) -> dict[str, Any]:
        try:
            value = response.json()
        except ValueError as exc:
            raise ExternalSourceError(f"{source} returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise ExternalSourceError(f"{source} returned a non-object JSON response")
        return value
