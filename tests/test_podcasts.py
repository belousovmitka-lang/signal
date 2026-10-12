import hashlib
from datetime import date, datetime, timezone

import httpx

from signal_connectors.config import SourcesFile
from signal_connectors.connectors.podcasts import judge_show, podcast_index_headers, transcript_text
from signal_connectors.industries import Industry, Link
from signal_connectors.runner import run_source
from signal_connectors.services import Services

from .conftest import signal_vcr

SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc)

FEED = """<?xml version="1.0"?><rss version="2.0" xmlns:podcast="https://podcastindex.org/namespace/1.0"
 xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel><title>Shop Talk</title><language>en-us</language>
<item><title>Ep 12: Comebacks</title><guid isPermaLink="false">g12</guid><pubDate>Mon, 05 Oct 2026 10:00:00 GMT</pubDate>
 <enclosure url="https://cdn.example/12.mp3" type="audio/mpeg" length="1"/><itunes:duration>00:42:10</itunes:duration>
 <podcast:transcript url="https://cdn.example/12.vtt" type="text/vtt"/><description>Why cars come back.</description></item>
<item><title>Ep 11</title><link>https://shoptalk.example/11</link><pubDate>Mon, 28 Sep 2026 10:00:00 GMT</pubDate>
 <description>Pricing labor.</description></item>
<item><title>Ep 1</title><link>https://shoptalk.example/1</link><pubDate>Mon, 05 Jan 2025 10:00:00 GMT</pubDate>
 <podcast:transcript url="https://cdn.example/1.vtt" type="text/vtt"/><description>Old.</description></item>
</channel></rss>"""
VTT = "WEBVTT\n\n1\n00:00:01.000 --> 00:00:04.000\n<v Host>Comebacks are killing us.\n\n00:00:04.500 --> 00:00:06.000\nWe track them by hand.\n"


def test_transcript_formats():
    assert transcript_text(VTT, "text/vtt") == "Comebacks are killing us. We track them by hand."
    srt = "1\n00:00:01,000 --> 00:00:02,000\nHello there\n\n2\n00:00:02,500 --> 00:00:03,000\nshop owners\n"
    assert transcript_text(srt, "application/srt") == "Hello there shop owners"
    assert transcript_text('{"segments": [{"body": "Hi"}, {"body": "all"}]}', "application/json") == "Hi all"
    assert transcript_text("<p>Hi <b>all</b></p>", "text/html") == "Hi all"


def test_podcast_index_auth_headers():
    h = podcast_index_headers("KEY", "SECRET", now=1760000000)
    assert h["X-Auth-Date"] == "1760000000"
    assert h["Authorization"] == hashlib.sha1(b"KEYSECRET1760000000").hexdigest()


def test_judge_show():
    today = date(2026, 10, 11)
    live = {"title": "Auto Repair Shop Owner Podcast", "description": "For shop owners", "episodeCount": 120,
            "language": "en-US", "newestItemPubdate": int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp())}
    terms = {"repair", "shop"}
    assert judge_show(live, terms, today) is None
    assert judge_show({**live, "newestItemPubdate": 1500000000}, terms, today) == "no recent episodes"
    assert judge_show({**live, "language": "de"}, terms, today) == "not English"
    assert judge_show({**live, "title": "Daily Comedy", "description": "Jokes"}, terms, today) == "off-topic"
    assert judge_show({**live, "title": "Daily Comedy", "description": "Jokes"}, terms, today, check_topic=False) is None


def test_live_shows_become_queries():
    sources = SourcesFile.model_validate({"sources": {"podcasts": {
        "route": "rss", "source_type": "media", "options": {"industry_podcasts": True}}}})
    ind = Industry(industry="auto_repair", name="Auto repair", podcasts=[
        Link(name="Shop Talk!", url="https://shoptalk.example/feed", status="live"),
        Link(name="Unfound show", status="unverified")])
    sources.add_industry_podcasts({"auto_repair": ind})
    assert [q.params for q in sources.get("podcasts").queries] == [
        {"feed": "https://shoptalk.example/feed", "name": "shop-talk", "community": "Shop Talk!"}]


async def test_episodes_with_transcripts(make_services):
    fetched = []

    def web(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/feed":
            return httpx.Response(200, text=FEED)
        if request.url.path == "/12.vtt":
            return httpx.Response(200, text=VTT)
        return httpx.Response(404)

    cfg = {"sources": {"podcasts": {"route": "rss", "source_type": "media", "options": {"transcripts_per_feed": 3},
                                    "queries": [{"params": {"feed": "https://shoptalk.example/feed", "name": "shop-talk"}}]}}}
    async with make_services(cfg, web) as svc:
        out = await run_source(svc, svc.sources.get("podcasts"), since=SINCE, collect=True)
    assert out.result.status == "ok" and out.result.new == 2                 # Ep 1 is older than since
    ep12, ep11 = out.posts
    assert ep12.url == "https://cdn.example/12.mp3"                          # guid isn't a link: the audio is
    assert ep12.source == "podcast:shop-talk" and ep12.source_type == "media" and not ep12.is_snippet
    assert ep12.text == "Why cars come back.\n\nTranscript:\nComebacks are killing us. We track them by hand."
    assert ep12.raw["duration"] == "00:42:10" and ep12.raw["transcript_chars"] == 48
    assert ep11.is_snippet and ep11.text == "Pricing labor."
    assert "https://cdn.example/1.vtt" not in fetched                       # no transcript fetch for old episodes
    assert not any(u.endswith(".mp3") for u in fetched)                     # audio is never downloaded


@signal_vcr.use_cassette("podcasts.yaml")
async def test_cassette_podcasts(settings):
    sources = SourcesFile.model_validate({"sources": {"podcasts": {
        "route": "rss", "source_type": "media", "options": {"transcripts_per_feed": 2},
        "queries": [{"params": {"feed": "https://feeds.buzzsprout.com/1.rss", "name": "buzzsprout"},
                     "limit": 5}]}}})
    async with Services.open(settings, sources) as svc:
        out = await run_source(svc, svc.sources.get("podcasts"), since=datetime(2020, 1, 1, tzinfo=timezone.utc),
                               collect=True)
    assert out.result.status == "ok" and out.result.invalid == 0 and out.result.fetched == 5
    with_transcripts = [p for p in out.posts if "transcript_chars" in p.raw]
    assert len(with_transcripts) == 2 and all(not p.is_snippet and "Transcript:" in p.text for p in with_transcripts)
    assert all(p.source == "podcast:buzzsprout" for p in out.posts)


async def test_find_shows(make_services, monkeypatch):
    monkeypatch.setenv("PODCASTINDEX_KEY", "K")
    monkeypatch.setenv("PODCASTINDEX_SECRET", "S")
    recent = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp())
    show = {"episodeCount": 50, "language": "en", "newestItemPubdate": recent}
    calls = []

    def index(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, request.url.params["q"], request.headers.get("X-Auth-Key")))
        if request.url.path.endswith("/bytitle"):
            return httpx.Response(200, json={"feeds": [{**show, "id": 1, "title": "Shop Talk", "url": "https://a/feed"}]})
        return httpx.Response(200, json={"feeds": [
            {**show, "id": 1, "title": "Shop Talk", "url": "https://a/feed"},
            {**show, "id": 2, "title": "Auto Repair Profits", "description": "For shop owners", "url": "https://b/feed"},
            {**show, "id": 3, "title": "Celebrity Gossip", "url": "https://c/feed"},
            {**show, "id": 4, "title": "Repair shop marketing", "url": "https://d/feed", "dead": 1}]})

    from signal_connectors.connectors.podcasts import find_shows
    async with make_services(None, index) as svc:
        found = await find_shows(svc.http, "Auto repair", ["shop owner"], ["Shop Talk"], {"repair", "shop"},
                                 date(2026, 10, 11))
    assert [(name, f["id"]) for name, f in found] == [("Shop Talk", 1), (None, 2)]
    assert calls[0] == ("/api/1.0/search/bytitle", "Shop Talk", "K")
