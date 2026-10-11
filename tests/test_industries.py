from datetime import date

import httpx
import pytest

from signal_connectors.base import HttpClient
from signal_connectors.config import SourcesFile
from signal_connectors.forum_detect import advertised_feeds, detect_software, wall_of
from signal_connectors.industries import Feed, Forum, Industry, Link, load_industries
from signal_connectors.robots import RobotsChecker
from signal_connectors.verify import check_feed, check_forum, check_link

TODAY = date(2026, 10, 9)


def test_repo_industry_maps_are_valid_and_verified():
    industries = load_industries()
    assert len(industries) == 15
    for key, ind in industries.items():
        assert ind.industry == key
        assert ind.vocabulary.roles and ind.vocabulary.pain_phrases and ind.subreddits
        assert ind.live_feeds(), f"{key} has no live feed"
        assert all(f.checked_at for f in ind.rss_feeds)


def test_rss_source_pulls_live_feeds_once():
    ind = Industry(industry="legal", name="Legal", rss_feeds=[
        Feed(name="a", url="https://a.example/feed", status="live"),
        Feed(name="b", url="https://b.example/feed", status="dead"),
        Feed(name="c", url="https://c.example/feed", status="live"),
    ])
    sources = SourcesFile.model_validate({"sources": {"rss": {
        "route": "rss", "source_type": "news", "options": {"industry_feeds": True},
        "queries": [{"params": {"feed": "https://c.example/feed", "name": "c"}}]},
        "hn": {"route": "own_api", "source_type": "community"}}})
    sources.add_industry_feeds({"legal": ind})
    feeds = [(q.params["feed"], q.industry_hint) for q in sources.get("rss").queries]
    assert feeds == [("https://c.example/feed", None), ("https://a.example/feed", "legal")]
    assert sources.get("hn").queries == []


@pytest.mark.parametrize("html, software", [
    ('<meta name="generator" content="Discourse 3.4">', "discourse"),
    ('<html id="XF" data-app="public">', "xenforo"),
    ('<meta name="generator" content="vBulletin 5.7">', "vbulletin"),
    ('Powered by phpBB', "phpbb"),
    ('<div class="lia-component-x">', "khoros"),
    ("<html><body>hi</body></html>", "unknown"),
])
def test_detect_software(html, software):
    assert detect_software(html) == software


def test_advertised_feeds_and_walls():
    html = ('<link rel="alternate" type="application/rss+xml" href="/feed.xml">'
            '<link rel="alternate" type="text/html" href="/x"><link rel="stylesheet" href="/s.css">')
    assert advertised_feeds(html, "https://a.example/blog/") == ["https://a.example/feed.xml"]
    assert wall_of("https://a.example/login?url=%2fboard") == "login"
    assert wall_of("https://a.example/.stile/challenge?rung=nojs") == "challenge"
    assert wall_of("https://a.example/forums/latest") is None
    assert wall_of("https://a.example/author/jane") is None


RSS = '<?xml version="1.0"?><rss version="2.0"><channel><title>T</title><item><title>x</title><link>https://a.example/1</link></item></channel></rss>'


def site(request: httpx.Request) -> httpx.Response:
    host, path = request.url.host, request.url.path
    if host == "gone.example":
        raise httpx.ConnectError("dns", request=request)
    if path == "/robots.txt":
        if host == "norobots.example":
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        return httpx.Response(404)
    if host == "forbidden.example":
        return httpx.Response(403)
    if host == "moved.example":
        if path == "/":
            return httpx.Response(200, text='<link rel="alternate" type="application/rss+xml" href="/new.rss">')
        if path == "/new.rss":
            return httpx.Response(200, text=RSS)
        return httpx.Response(404)
    if host == "xf.example":
        if path == "/forums/-/index.rss":
            return httpx.Response(200, text=RSS)
        return httpx.Response(200, text='<html id="XF">forum</html>')
    if host == "walled.example":
        if path == "/login":
            return httpx.Response(200, text="log in")
        return httpx.Response(302, headers={"location": "https://walled.example/login?returnurl=/board"})
    if host == "ok.example":
        return httpx.Response(200, text=RSS)
    return httpx.Response(404)


@pytest.fixture
async def tools():
    http = HttpClient(user_agent="SignalBot/0.1", default_rps=1000, max_attempts=1, transport=httpx.MockTransport(site))
    async with http:
        yield http, RobotsChecker(http)


@pytest.mark.parametrize("url, status, note", [
    ("https://ok.example/feed", "live", None),
    ("https://norobots.example/feed", "blocked", "robots.txt disallows"),
    ("https://forbidden.example/feed", "blocked", "HTTP 403 to our bot"),
    ("https://gone.example/feed", "dead", "site unreachable (ConnectError)"),
    ("https://moved.example/old.rss", "live", "moved from https://moved.example/old.rss (found on homepage)"),
])
async def test_check_feed(tools, url, status, note):
    feed = Feed(name="f", url=url)
    await check_feed(*tools, feed, TODAY)
    assert (feed.status, feed.notes) == (status, note)
    assert feed.checked_at == TODAY
    if status == "live":
        assert feed.entries == 1


async def test_check_forum_detects_xenforo_feed(tools):
    forum = Forum(name="xf", url="https://xf.example/")
    await check_forum(*tools, forum, TODAY)
    assert (forum.status, forum.software, forum.rss) == ("live", "xenforo", "https://xf.example/forums/-/index.rss")


@pytest.mark.parametrize("url, status", [
    ("https://walled.example/board", "blocked"),
    ("https://norobots.example/", "search_only"),
    ("https://forbidden.example/", "search_only"),
    ("https://gone.example/", "dead"),
])
async def test_check_forum_statuses(tools, url, status):
    forum = Forum(name="f", url=url)
    await check_forum(*tools, forum, TODAY)
    assert forum.status == status


async def test_check_link(tools):
    links = [Link(name=n, url=u) for n, u in [("a", "https://ok.example/"), ("b", "https://forbidden.example/"),
                                                ("c", "https://gone.example/"), ("d", None)]]
    for link in links:
        await check_link(*tools, link, TODAY)
    assert [l.status for l in links] == ["live", "search_only", "dead", "unverified"]


async def test_locked_entries_are_left_alone(tools):
    forum = Forum(name="gone", url="https://ok.example/", status="dead", locked=True, notes="human said so")
    feed = Feed(name="f", url="https://gone.example/feed", status="live", locked=True)
    await check_forum(*tools, forum, TODAY)
    await check_feed(*tools, feed, TODAY)
    assert (forum.status, forum.notes, forum.checked_at) == ("dead", "human said so", None)
    assert feed.status == "live" and feed.checked_at is None
