import httpx
import pytest

from signal_connectors.base import HttpClient
from signal_connectors.robots import RobotsChecker, RobotsDisallowed, parse_robots

from .conftest import signal_vcr

ROBOTS = """
User-agent: *
Disallow: /search
Disallow: /*?sort=
Allow: /search/about
Disallow: /*.pdf$
Crawl-delay: 2

User-agent: GPTBot
User-agent: SignalBot
Disallow: /private/
Allow: /private/ok

User-agent: OtherBot
Disallow: /
"""


@pytest.mark.parametrize("agent, path, allowed", [
    ("SomeBot", "/", True),
    ("SomeBot", "/search?q=x", False),
    ("SomeBot", "/search/about", True),         # longer Allow wins
    ("SomeBot", "/forum/t/1?sort=new", False),  # * wildcard
    ("SomeBot", "/files/a.pdf", False),         # $ anchor
    ("SomeBot", "/files/a.pdf?x=1", True),
    ("SignalBot", "/search?q=x", True),         # our own group replaces *
    ("SignalBot", "/private/x", False),
    ("SignalBot", "/private/ok/1", True),
])
def test_rule_matching(agent, path, allowed):
    assert parse_robots(ROBOTS, agent).allowed(path) is allowed


def test_crawl_delay_and_empty_disallow():
    assert parse_robots(ROBOTS, "SomeBot").crawl_delay == 2
    assert parse_robots("User-agent: *\nDisallow:\n", "SignalBot").allowed("/anything")
    assert parse_robots("", "SignalBot").allowed("/anything")


def checker(handler):
    http = HttpClient(user_agent="SignalBot/0.1", default_rps=1000, backoff_initial=0.01, backoff_max=0.02,
                      max_attempts=2, transport=httpx.MockTransport(handler))
    return http, RobotsChecker(http)


async def test_fetch_is_cached_per_origin_and_applies_crawl_delay():
    fetched = []

    def handler(request):
        fetched.append(str(request.url))
        return httpx.Response(200, text="User-agent: *\nDisallow: /admin\nCrawl-delay: 4\n")

    http, robots = checker(handler)
    async with http:
        assert await robots.allowed("https://forum.example.org/t/1")
        assert not await robots.allowed("https://forum.example.org/admin/x")
        with pytest.raises(RobotsDisallowed):
            await robots.require("https://forum.example.org/admin")
        assert http.limiter.rate("forum.example.org") == 0.25
        import time
        assert http.limiter._next["forum.example.org"] >= time.monotonic() + 3  # next request waits ~4s
    assert fetched == ["https://forum.example.org/robots.txt"]


@pytest.mark.parametrize("status, allowed", [(404, True), (403, True), (500, False), (503, False)])
async def test_status_handling_follows_rfc_9309(status, allowed):
    http, robots = checker(lambda r: httpx.Response(status))
    async with http:
        assert await robots.allowed("https://x.example.org/page") is allowed


async def test_unreachable_means_disallow():
    def handler(request):
        raise httpx.ConnectError("nope", request=request)

    http, robots = checker(handler)
    async with http:
        assert not await robots.allowed("https://x.example.org/page")


@signal_vcr.use_cassette("robots_python_org.yaml")
async def test_cassette_real_robots_txt():
    """Recorded response: checks the cassette setup works with the shared httpx client."""
    async with HttpClient(user_agent="SignalBot/0.1 (+mailto:tests@example.org)") as http:
        robots = RobotsChecker(http)
        assert await robots.allowed("https://www.python.org/about/")
        rules = await robots.rules_for("https://www.python.org/")
        assert rules.rules  # parsed something real
