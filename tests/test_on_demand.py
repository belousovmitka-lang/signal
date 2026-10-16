from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import feedparser
import httpx
import pytest

from signal_connectors import on_demand
from signal_connectors.industries import (DiscoveryInfo, Feed, Forum, Industry, Vocabulary, load_industry,
                                          save_industry)
from signal_connectors.on_demand import (IndustryRequest, Plan, ensure_industry, judge_feed, lexical_match,
                                         review_queue, vocabulary_terms)

NOW = datetime.now(timezone.utc)
TODAY = NOW.date()


def rss(titles, *, title="Feed", age_days=1):
    when = format_datetime(NOW - timedelta(days=age_days))
    items = "".join(f"<item><title>{t}</title><link>https://feeds.example/{i}</link><pubDate>{when}</pubDate>"
                    f"<description>{t}</description></item>" for i, t in enumerate(titles))
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>{title}</title>{items}</channel></rss>'


GROOMING = Industry(industry="dog_grooming", name="Dog grooming businesses",
                    vocabulary=Vocabulary(roles=["dog groomer"], tasks=["de-shedding"], jargon=["hand stripping"],
                                          tools=["MoeGo"]))
SALONS = Industry(industry="salons", name="Salons and spas", vocabulary=Vocabulary(roles=["hairstylist", "salon owner"]))


def test_lexical_match():
    maps = {"salons": SALONS, "veterinary": Industry(industry="veterinary", name="Veterinary clinics",
                                                     vocabulary=Vocabulary(roles=["veterinarian"]))}
    assert lexical_match(IndustryRequest("Hair salon"), maps) == "salons"
    assert lexical_match(IndustryRequest("Dog grooming"), maps) is None
    maps["childcare"] = Industry(industry="childcare", name="Childcare centers",
                                 vocabulary=Vocabulary(roles=["home daycare provider"]))
    maps["home_services"] = Industry(industry="home_services", name="Home services",
                                     vocabulary=Vocabulary(roles=["pest control technician"]))
    assert lexical_match(IndustryRequest("Funeral homes"), maps) is None       # "home" alone is too common
    assert lexical_match(IndustryRequest("Pest control"), maps) == "home_services"
    assert lexical_match(IndustryRequest("Hair salons"), maps) == "salons"            # word forms match
    maps["logistics"] = Industry(industry="logistics", name="Logistics and trucking",
                                 vocabulary=Vocabulary(roles=["truck driver"]))
    assert lexical_match(IndustryRequest("Food truck"), maps) is None               # half a match isn't one


@pytest.mark.parametrize("body, reason", [
    (rss(["Dog groomer pricing in 2026", "MoeGo adds waitlists", "Hand stripping basics", "Groomer burnout"]), None),
    (rss(["Pricing", "Waitlists", "Basics"], title="Dog Groomer Weekly"), None),            # the feed names the trade
    (rss(["Election night", "Stocks fall", "Weather"]), "headlines match"),
    (rss(["Dog groomer news"] * 2), "only 2 items"),
    (rss(["Groomer tips"] * 600), "page farm"),
    (rss(["Groomer tips", "Best casino bonus", "Groomer tools"]), "spam"),
    (rss(["Groomer tips"] * 4, age_days=400), "no new items"),
])
def test_judge_feed(body, reason):
    verdict = judge_feed(feedparser.parse(body), vocabulary_terms(GROOMING), TODAY)
    assert verdict is None if reason is None else reason in verdict


@pytest.fixture
def maps(tmp_path, monkeypatch):
    monkeypatch.setattr(on_demand, "LOCK_DIR", tmp_path / "locks")
    d = tmp_path / "industries"
    d.mkdir()
    save_industry(SALONS, d / "salons.yaml")
    return d


SOURCES = {"sources": {"rss": {"route": "rss", "source_type": "news", "options": {"industry_feeds": True}}}}


def web(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/robots.txt":
        return httpx.Response(404)
    if request.url.host == "good.example":
        return httpx.Response(200, text=rss(["Dog groomer pricing", "MoeGo update", "Hand stripping", "Groomer pay"],
                                            age_days=30))         # older than a regular run's lookback
    if request.url.host == "api.ashbyhq.com" and request.url.path.endswith("/moego"):
        return httpx.Response(200, json={"jobs": [{"id": "1", "descriptionPlain": "Join MoeGo's support team"}]})
    if request.url.host == "old.example":
        return httpx.Response(200, text=rss(["Dog groomer pricing"] * 4, age_days=400))
    if request.url.host == "news.example":
        return httpx.Response(200, text=rss(["Election night", "Stocks fall", "Weather today"]))
    return httpx.Response(404)


async def fake_discover(services, seeds, *, budget_usd, today):
    assert seeds.key == "dog_grooming" and seeds.roles == ["dog groomer"]
    return Industry(industry=seeds.key, name=seeds.name, discovery=DiscoveryInfo(generated_at=today),
                    rss_feeds=[Feed(name="good", url="https://good.example/feed", status="live"),
                               Feed(name="news", url="https://news.example/feed", status="live"),
                               Feed(name="old", url="https://old.example/feed", status="live")],
                    forums=[Forum(name="Groomers Board", url="https://board.example/", status="live", rss="https://board.example/rss")])


async def plan_new(services, request, industries):
    assert "salons" in industries
    return Plan(match=None, key="dog_grooming", name="Dog grooming businesses", roles=["dog groomer"],
                keywords=["dog grooming"], tools=["MoeGo"], tasks=["de-shedding"], jargon=["hand stripping"],
                subreddits=["doggrooming"])


async def test_ensure_industry_builds_a_map_and_fills_it(make_services, settings, maps):
    settings.sources_file.write_text("sources:\n  rss:\n    enabled: true\n    route: rss\n    source_type: news\n"
                                     "    options: {industry_feeds: true}\n")
    async with make_services(SOURCES, web) as svc:
        result = await ensure_industry(svc, IndustryRequest("Dog grooming"), industries_dir=maps,
                                       planner=plan_new, discoverer=fake_discover, today=TODAY)
        assert svc.store.last_run("rss") is None                      # the regular schedule is untouched
        assert svc.store.last_run("rss:dog_grooming") is not None
    assert result.action == "created" and result.key == "dog_grooming"
    assert result.feeds_enabled == ["https://good.example/feed"]
    assert list(result.feeds_held) == ["https://news.example/feed"] and "headlines match" in result.feeds_held[
        "https://news.example/feed"]
    assert list(result.feeds_skipped) == ["https://old.example/feed"]     # asleep: no human needed
    assert result.forums_queued == ["https://board.example/"]          # forums wait for a human
    assert result.job_boards == ["https://jobs.ashbyhq.com/moego"]      # the incumbent tool's public board
    assert result.first_fill["rss"]["new"] == 4

    ind = load_industry(maps / "dog_grooming.yaml")
    assert not ind.discovery.needs_review and ind.discovery.on_demand and ind.discovery.request == "Dog grooming"
    assert [f.url for f in ind.live_feeds()] == ["https://good.example/feed"]
    held = next(f for f in ind.rss_feeds if f.url == "https://news.example/feed")
    assert held.locked and held.notes.startswith("held for review")
    assert ind.vocabulary.tasks == ["de-shedding"] and ind.subreddits == ["doggrooming"]
    kinds = {(r["industry"], r["kind"]) for r in review_queue(maps)}
    assert kinds == {("dog_grooming", "forum terms"), ("dog_grooming", "held feed")}

    async with make_services(SOURCES, web) as svc:                     # a second request reuses the map
        again = await ensure_industry(svc, IndustryRequest("Dog grooming"), industries_dir=maps,
                                      planner=plan_new, discoverer=fake_discover, today=TODAY)
    assert again.action == "exists"


async def test_matches_without_discovery(make_services, maps):
    async def plan_match(services, request, industries):
        return Plan(match="salons", key="hair_salons", name="Hair salons")

    async def never(*a, **k):
        raise AssertionError("discovery must not run for a match")

    async with make_services(SOURCES) as svc:
        result = await ensure_industry(svc, IndustryRequest("Hair salon"), industries_dir=maps,
                                       planner=plan_match, discoverer=never)
    assert (result.action, result.key) == ("matched", "salons")


async def test_planner_failure_falls_back_to_the_request(make_services, maps):
    async def broken(services, request, industries):
        raise RuntimeError("API down")

    async with make_services(SOURCES) as svc:
        result = await ensure_industry(svc, IndustryRequest("Salons"), industries_dir=maps,
                                       planner=broken, discoverer=fake_discover)
    assert (result.action, result.key) == ("matched", "salons")         # lexical match still works
