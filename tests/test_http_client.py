from datetime import datetime, timezone
from email.utils import format_datetime

import httpx
import pytest

from app.collectors.http import ResilientHttpClient
from app.exceptions import ExternalSourceError


def test_retries_429_then_succeeds():
    calls = []
    sleeps = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"ok": True})

    with ResilientHttpClient(
        transport=httpx.MockTransport(handler),
        max_retries=1,
        sleep=sleeps.append,
    ) as client:
        assert client.get_json("https://example.test", source="test") == {"ok": True}
    assert len(calls) == 2
    assert sleeps == [0.0]


@pytest.mark.parametrize("status_code", [500, 503])
def test_retries_server_errors(status_code):
    calls = 0

    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(status_code if calls == 1 else 200, json={"ok": True})

    with ResilientHttpClient(
        transport=httpx.MockTransport(handler), max_retries=1, sleep=lambda _value: None
    ) as client:
        assert client.get_json("https://example.test", source="test") == {"ok": True}
    assert calls == 2


def test_retries_timeout_then_succeeds():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("temporary", request=request)
        return httpx.Response(200, json={"ok": True})

    with ResilientHttpClient(
        transport=httpx.MockTransport(handler), max_retries=1, sleep=lambda _value: None
    ) as client:
        assert client.get_json("https://example.test", source="test") == {"ok": True}
    assert calls == 2


def test_common_client_enforces_request_interval():
    sleeps = []
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={"ok": True}))
    with ResilientHttpClient(
        transport=transport,
        min_interval_sec=0.5,
        sleep=sleeps.append,
        clock=lambda: 10.0,
    ) as client:
        client.get_json("https://example.test/1", source="test")
        client.get_json("https://example.test/2", source="test")
    assert sleeps == [0.5]


def test_non_retryable_error_does_not_leak_url_query():
    transport = httpx.MockTransport(lambda _request: httpx.Response(401))
    with ResilientHttpClient(transport=transport, max_retries=0) as client:
        with pytest.raises(ExternalSourceError) as exc:
            client.request(
                "GET",
                "https://example.test",
                source="test",
                params={"api_key": "secret"},
            )
    assert "secret" not in str(exc.value)
    assert "401" in str(exc.value)


def test_invalid_json_is_explicit_error():
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, text="not json"))
    with ResilientHttpClient(transport=transport) as client:
        with pytest.raises(ExternalSourceError, match="invalid JSON"):
            client.get_json("https://example.test", source="test")


def test_non_object_json_is_rejected():
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=[]))
    with ResilientHttpClient(transport=transport) as client:
        with pytest.raises(ExternalSourceError, match="non-object"):
            client.get_json("https://example.test", source="test")


def test_negative_request_interval_is_rejected():
    with pytest.raises(ValueError, match="non-negative"):
        ResilientHttpClient(min_interval_sec=-0.1)


def test_exhausted_transport_error_is_reported_without_request_details():
    def handler(request):
        raise httpx.ReadTimeout("secret query", request=request)

    with ResilientHttpClient(
        transport=httpx.MockTransport(handler), max_retries=0, sleep=lambda _value: None
    ) as client:
        with pytest.raises(ExternalSourceError, match="ReadTimeout") as exc:
            client.request(
                "GET",
                "https://example.test",
                source="test",
                params={"token": "secret"},
            )
    assert "token" not in str(exc.value)


def test_negative_retry_count_is_rejected():
    with pytest.raises(ValueError, match="max_retries"):
        ResilientHttpClient(max_retries=-1)


def test_post_json_uses_post_request():
    seen = []

    def handler(request):
        seen.append(request.method)
        return httpx.Response(200, json={"ok": True})

    with ResilientHttpClient(transport=httpx.MockTransport(handler)) as client:
        assert client.post_json("https://example.test", source="test", json={}) == {"ok": True}
    assert seen == ["POST"]


def test_retry_delay_accepts_seconds_http_date_and_invalid_values(monkeypatch):
    client = ResilientHttpClient(max_retries=0, backoff_base_sec=1)
    try:
        assert client._delay(0, "-5") == 0
        assert client._delay(0, "99") == 30
        monkeypatch.setattr("app.collectors.http.time.time", lambda: 100.0)
        future = format_datetime(datetime.fromtimestamp(105, timezone.utc), usegmt=True)
        assert client._delay(0, future) == 5
        assert client._delay(1, "not a date") == pytest.approx(2.074)
    finally:
        client.close()


def test_pacing_does_not_sleep_when_interval_already_elapsed():
    ticks = iter([10.0, 11.0])
    sleeps = []
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={"ok": True}))
    with ResilientHttpClient(
        transport=transport,
        min_interval_sec=0.5,
        sleep=sleeps.append,
        clock=lambda: next(ticks),
    ) as client:
        client.get_json("https://example.test/1", source="test")
        client.get_json("https://example.test/2", source="test")
    assert sleeps == []
