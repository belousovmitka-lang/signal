import httpx
import pytest

from signal_connectors import live
from signal_connectors.industries import Industry, Vocabulary
from signal_connectors.live import build_query, live_search

CFG = {"defaults": {"provider_budgets": {"brave": 1.0}}, "sources": {}}
DENTAL = Industry(industry="dental", name="Dental practices",
                  vocabulary=Vocabulary(roles=["dentist", "office manager"], pain_phrases=["insurance denials"]))


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "brave-test")
    monkeypatch.setenv("YOUTUBE_API_KEY", "yt-test")
    live._cache.clear()


def test_query():
    assert build_query(DENTAL) == 'dentist software "how do you"'
    salon = Industry(industry="salons", name="Salons", vocabulary=Vocabulary(roles=["salon owner"]))
    assert build_query(salon, extra="booth rent") == '"salon owner" booth rent'


def test_only_threads_and_posts_pass():
    import re
    assert re.search(live.PLATFORMS["reddit"][2], "https://www.reddit.com/r/Dentists/comments/1t/x/")
    assert not re.search(live.PLATFORMS["reddit"][2], "https://www.reddit.com/r/Dentistry/")
    assert not re.search(live.PLATFORMS["linkedin"][2], "https://www.linkedin.com/in/someone/")
    assert re.search(live.PLATFORMS["linkedin"][2], "https://www.linkedin.com/posts/acme_dental-activity-1")
    assert not re.search(live.PLATFORMS["tiktok"][2], "https://www.tiktok.com/discover/dental-trend")


class Web:
    def __init__(self):
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.url.host == "api.search.brave.com":
            q = request.url.params["q"]
            if "tiktok" in q:
                return httpx.Response(429)
            return httpx.Response(200, json={"web": {"results": [
                {"title": "Is there a tool for <strong>insurance denials</strong>?", "url": "https://www.reddit.com/r/Dentistry/comments/1t/x/",
                 "description": "Our office manager spends hours...", "age": "3 days ago"}]}})
        if request.url.host == "www.googleapis.com":
            return httpx.Response(200, json={"items": [{"id": {"videoId": "abc"}, "snippet": {
                "title": "Fixing denials", "description": "d", "channelTitle": "Dental Biz", "publishedAt": "2026-10-01T00:00:00Z",
                "thumbnails": {"medium": {"url": "https://i.ytimg.com/vi/abc/mq.jpg"}}}}]})
        return httpx.Response(404)


async def test_sections_unmodified_and_nothing_stored(make_services):
    web = Web()
    async with make_services(CFG, web) as svc:
        res = await live_search(svc, DENTAL, platforms=["reddit", "tiktok", "youtube"])
        again = await live_search(svc, DENTAL, platforms=["reddit", "youtube"])
        assert svc.store.count_posts() == 0                                   # display only
        spent = svc.costs.spent_this_month("live")
    by = {s.platform: s for s in res.sections}
    reddit = by["reddit"].items[0]
    assert reddit.title == "Is there a tool for <strong>insurance denials</strong>?"    # passed through unmodified
    assert reddit.url == "https://www.reddit.com/r/Dentistry/comments/1t/x/" and reddit.age == "3 days ago"
    assert by["tiktok"].error == "Brave HTTP 429" and by["tiktok"].items == []           # one failure, others fine
    yt = by["youtube"].items[0]
    assert (yt.url, yt.channel, by["youtube"].provider) == ("https://www.youtube.com/watch?v=abc", "Dental Biz", "youtube")
    assert "YouTube logo" in by["youtube"].attribution and res.stored is False
    sent = [r for r in web.calls if r.url.host == "api.search.brave.com"]
    assert sent[0].url.params["q"] == 'site:reddit.com dentist software "how do you"'
    assert sent[0].url.params["freshness"] == "py"
    assert len(web.calls) == 3                                                # second view served from transient memory
    assert [s.items for s in again.sections] == [by["reddit"].items, by["youtube"].items]
    assert round(spent, 3) == 0.010                                           # two Brave searches metered


async def test_cache_expires(make_services, monkeypatch):
    web = Web()
    async with make_services(CFG, web) as svc:
        await live_search(svc, DENTAL, platforms=["reddit"])
        monkeypatch.setattr(live, "CACHE_SECONDS", 0)
        await live_search(svc, DENTAL, platforms=["reddit"])
    assert len(web.calls) == 2


async def test_budget_exhausted_is_a_section_error(make_services):
    cfg = {"defaults": {"live_monthly_budget_usd": 0.0, "provider_budgets": {"brave": 1.0}}, "sources": {}}
    async with make_services(cfg, Web()) as svc:
        res = await live_search(svc, DENTAL, platforms=["reddit"])
    assert res.sections[0].error.startswith("BudgetExceeded")
