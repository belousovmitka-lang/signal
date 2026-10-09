from datetime import datetime, timezone

import httpx

from signal_connectors.runner import run_source
from signal_connectors.services import Services

from .conftest import signal_vcr

LONG_AGO = datetime(2020, 1, 1, tzinfo=timezone.utc)


def rss_source(*feeds):
    return {"sources": {"rss": {
        "route": "rss", "source_type": "news",
        "queries": [{"industry_hint": ind, "params": {"feed": url, "name": name}} for ind, url, name in feeds],
    }}}


@signal_vcr.use_cassette("rss_fierce_jlc.yaml")
async def test_cassette_parses_real_feeds(settings):
    from signal_connectors.config import SourcesFile

    sources = SourcesFile.model_validate(rss_source(
        ("healthcare", "https://www.fiercehealthcare.com/rss/xml", "fiercehealthcare"),
        ("construction", "https://www.jlconline.com/feed/", "jlc"),
    ))
    async with Services.open(settings, sources) as svc:
        out = await run_source(svc, svc.sources.get("rss"), since=LONG_AGO, collect=True)

    r = out.result
    assert r.status == "ok" and r.invalid == 0 and r.fetched >= 20
    by_source: dict[str, list] = {}
    for p in out.posts:
        by_source.setdefault(p.source, []).append(p)
    fierce, jlc = by_source["rss:fiercehealthcare"], by_source["rss:jlc"]

    for p in out.posts:
        assert p.route == "rss" and p.source_type == "news"
        assert p.url.startswith("https://") and p.title and "<" not in p.title
        assert p.text and "<p" not in p.text
        assert p.published_at.tzinfo is not None and "date_missing" not in p.raw
        assert p.author_hash is None or len(p.author_hash) == 64
    assert all(p.is_snippet and p.industry_hint == "healthcare" for p in fierce)
    assert all(p.community == "Fierce Healthcare" and p.language == "en" for p in fierce)
    assert all(not p.is_snippet for p in jlc)  # JLC ships full content
    assert max(len(p.text) for p in jlc) > max(len(p.text) for p in fierce)


FEED = """<?xml version="1.0"?><rss version="2.0"><channel><title>Trade Mag</title><language>en-us</language>
<item><title>Has a date</title><link>https://mag.example/a</link><description>&lt;p&gt;Sum &lt;b&gt;one&lt;/b&gt;&lt;/p&gt;</description>
<pubDate>Oct 9, 2026 11:15am</pubDate><author>Jane Writer</author></item>
<item><title>No date</title><link>https://mag.example/b</link><description>Two</description></item>
<item><title>No link</title><description>Three</description></item>
</channel></rss>"""


def handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/robots.txt":
        if request.url.host == "blocked.example":
            return httpx.Response(200, text="User-agent: *\nDisallow: /feed\n")
        return httpx.Response(404)
    if request.url.host == "broken.example":
        return httpx.Response(200, text="<html>not a feed")
    return httpx.Response(200, text=FEED, headers={"content-type": "application/rss+xml"})


async def test_entries_dates_and_invalid_items(make_services):
    async with make_services(rss_source((None, "https://mag.example/feed", "mag")), handler) as svc:
        out = await run_source(svc, svc.sources.get("rss"), since=LONG_AGO, collect=True)
    a, b = out.posts
    assert a.published_at == datetime(2026, 10, 9, 11, 15, tzinfo=timezone.utc)
    assert a.text == "Sum one" and a.is_snippet and a.language == "en" and a.community == "Trade Mag"
    assert a.author_hash and "jane" not in a.author_hash.lower()
    assert b.raw["date_missing"] is True
    assert out.result.invalid == 1  # the item without a link


async def test_robots_disallow_and_broken_feeds_fail_alone(make_services):
    sources = rss_source((None, "https://blocked.example/feed", "blocked"),
                         (None, "https://broken.example/feed", "broken"),
                         (None, "https://mag.example/feed", "mag"))
    async with make_services(sources, handler) as svc:
        r = (await run_source(svc, svc.sources.get("rss"), since=LONG_AGO)).result
    assert r.status == "partial" and r.new == 2
    assert any("robots.txt disallows" in e for e in r.errors)
    assert any("unparseable" in e for e in r.errors)
