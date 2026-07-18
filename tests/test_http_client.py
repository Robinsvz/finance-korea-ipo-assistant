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
