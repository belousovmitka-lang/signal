import asyncio
import time

import httpx
import pytest

from signal_connectors.base import HttpClient, RateLimiter, retry_after_seconds


def client(handler, **kw):
    kw.setdefault("backoff_initial", 0.01)
    kw.setdefault("backoff_max", 0.05)
    return HttpClient(user_agent="SignalBot/0.1 (+mailto:t@example.org)", transport=httpx.MockTransport(handler), **kw)


async def test_user_agent_is_sent():
    seen = {}

    def handler(request):
        seen["ua"] = request.headers["user-agent"]
        return httpx.Response(200, json={"ok": True})

    async with client(handler, default_rps=100) as http:
        assert await http.get_json("https://api.example.org/x") == {"ok": True}
    assert seen["ua"] == "SignalBot/0.1 (+mailto:t@example.org)"


async def test_rate_limiter_spaces_requests_per_domain():
    limiter = RateLimiter(default_rps=20)  # 50 ms apart
    start = time.monotonic()
    await asyncio.gather(*(limiter.acquire("a.example") for _ in range(4)))
    assert time.monotonic() - start >= 0.14
    start = time.monotonic()
    await asyncio.gather(limiter.acquire("b.example"), limiter.acquire("c.example"))
    assert time.monotonic() - start < 0.05  # different domains don't wait on each other


async def test_retries_429_then_succeeds():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, text="ok")

    async with client(handler, default_rps=1000) as http:
        r = await http.get("https://api.example.org/x")
    assert r.status_code == 200 and len(calls) == 3


async def test_gives_up_after_max_attempts_and_returns_last_response():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503)

    async with client(handler, default_rps=1000, max_attempts=3) as http:
        r = await http.get("https://api.example.org/x")
        assert r.status_code == 503 and len(calls) == 3
        with pytest.raises(httpx.HTTPStatusError):
            await http.get_text("https://api.example.org/x")


async def test_does_not_retry_client_errors():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(404)

    async with client(handler, default_rps=1000) as http:
        assert (await http.get("https://api.example.org/x")).status_code == 404
    assert len(calls) == 1


async def test_transport_errors_retry_then_raise():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectError("down", request=request)

    async with client(handler, default_rps=1000, max_attempts=2) as http:
        with pytest.raises(httpx.ConnectError):
            await http.get("https://api.example.org/x")
    assert len(calls) == 2


def test_retry_after_parsing():
    assert retry_after_seconds(httpx.Response(429, headers={"Retry-After": "7"})) == 7
    assert retry_after_seconds(httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})) == 0
    assert retry_after_seconds(httpx.Response(429)) is None
