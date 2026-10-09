import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from signal_connectors.config import SourcesFile
from signal_connectors.connectors.search_api import BlueskyResolver, MediumResolver, SubstackResolver
from signal_connectors.costs import BudgetExceeded
from signal_connectors.discovery import BRAVE_MAX_QUERY, brave_queries
from signal_connectors.runner import recheck_deletions, run_source
from signal_connectors.services import Services

from .conftest import signal_vcr

SINCE = datetime(2026, 9, 1, tzinfo=timezone.utc)
DID = "did:plc:aaaaaaaaaaaaaaaaaaaaaaaa"


def search_source(name="search_api", *queries, budget=1.0, provider_budgets=None):
    return {"defaults": {"provider_budgets": provider_budgets or {"brave": 1.0, "exa": 1.0}},
            "sources": {name: {"connector": "search_api", "route": "search_api", "source_type": "community",
                               "monthly_budget_usd": budget, "queries": list(queries)}}}


def bsky_post(rkey, text="I'm a paralegal and intake is all manual. Is there a tool?", hidden=False):
    labels = [{"val": "!no-unauthenticated"}] if hidden else []
    return {"uri": f"at://{DID}/app.bsky.feed.post/{rkey}", "cid": "c", "likeCount": 3, "replyCount": 1,
            "repostCount": 0, "quoteCount": 0, "author": {"did": DID, "handle": "pat.bsky.social", "labels": labels},
            "record": {"text": text, "createdAt": "2026-10-05T12:00:00.000Z", "langs": ["en-US"]}}


FEED = """<?xml version="1.0"?><rss version="2.0"><channel><title>Pat's blog</title><language>en</language>
<item><title>Why intake is broken</title><link>https://medium.com/@pat/why-intake-is-broken-50e04eb1e905?source=rss-x</link>
<pubDate>Mon, 05 Oct 2026 10:00:00 GMT</pubDate><description>Snippet</description></item>
<item><title>Old post</title><link>https://medium.com/@pat/old-1111aaaa2222</link><pubDate>Mon, 05 Oct 2026 09:00:00 GMT</pubDate></item>
</channel></rss>"""


class Api:
    """Mock for Exa, Brave and the platforms; records every request."""

    def __init__(self, exa_urls=(), brave_urls=(), posts=()):
        self.exa_urls, self.brave_urls, self.posts = list(exa_urls), list(brave_urls), {p["uri"]: p for p in posts}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if path == "/robots.txt":
            return httpx.Response(404)
        if host == "api.exa.ai":
            return httpx.Response(200, json={"results": [{"url": u, "title": "T", "id": "x"} for u in self.exa_urls],
                                             "costDollars": {"total": 0.007}})
        if host == "api.search.brave.com":
            return httpx.Response(200, json={"web": {"results": [{"url": u, "description": "d"} for u in self.brave_urls]},
                                             "query": {"more_results_available": False}})
        if path.endswith("resolveHandle"):
            return httpx.Response(200, json={"did": DID})
        if path.endswith("getPosts"):
            uris = request.url.params.get_list("uris")
            return httpx.Response(200, json={"posts": [self.posts[u] for u in uris if u in self.posts]})
        if host == "medium.com" and path == "/feed/@pat":
            return httpx.Response(200, text=FEED)
        return httpx.Response(404)


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "exa-test")
    monkeypatch.setenv("BRAVE_API_KEY", "brave-test")


async def test_pointer_flow_stores_only_platform_data(make_services):
    api = Api(exa_urls=[
        "https://bsky.app/profile/pat.bsky.social/post/3aaa",
        "https://bsky.app/profile/pat.bsky.social/post/3bbb",       # hidden from logged-out viewers
        "https://bsky.app/profile/pat.bsky.social",                 # not a post
        "https://www.reddit.com/r/paralegal/comments/x/y/",         # no resolver: dropped
        "https://medium.com/@pat/why-intake-is-broken-50e04eb1e905",
        "https://medium.com/@pat/not-in-feed-9999ffff0000",         # older than the feed: skipped
    ], posts=[bsky_post("3aaa"), bsky_post("3bbb", hidden=True)])
    q = {"keywords": ["paralegal intake pain"], "domains": ["bsky.app", "medium.com"], "params": {"provider": "exa"}}
    async with make_services(search_source("search_api", q), api) as svc:
        out = await run_source(svc, svc.sources.get("search_api"), since=SINCE, collect=True)
        assert svc.costs.provider_spent_this_month("exa") == pytest.approx(0.007)

    sky, med = sorted(out.posts, key=lambda p: p.source)
    assert out.result.status == "ok" and len(out.posts) == 2
    assert sky.source == "bluesky" and sky.route == "own_api" and sky.source_type == "social"
    assert sky.url == f"https://bsky.app/profile/{DID}/post/3aaa"        # stable DID form, not the handle
    assert sky.engagement == {"like": 3, "reply": 1, "repost": 0, "quote": 0} and sky.language == "en"
    assert "pat.bsky.social" not in sky.model_dump_json()
    assert med.source == "rss:medium" and med.route == "rss" and med.title == "Why intake is broken"
    assert med.url == "https://medium.com/@pat/why-intake-is-broken-50e04eb1e905"
    # nothing from the search result itself is kept
    for p in out.posts:
        dumped = p.model_dump_json()
        assert '"T"' not in dumped and "exa" not in json.dumps(p.raw)
    # the dropped reddit URL was never fetched
    assert not any(r.url.host.endswith("reddit.com") for r in api.requests)
    exa_body = json.loads(next(r for r in api.requests if r.url.host == "api.exa.ai").content)
    assert "contents" not in exa_body and exa_body["includeDomains"] == ["bsky.app", "medium.com"]
    assert exa_body["startPublishedDate"] == "2026-09-01T00:00:00.000Z"


async def test_brave_discovery_splits_long_site_filters(make_services):
    domains = [f"reddit.com/r/subreddit_number_{i}" for i in range(20)]
    queries = brave_queries('("is there a tool" OR "so tired of")', domains)
    assert len(queries) > 1 and all(len(q) <= BRAVE_MAX_QUERY for q in queries)
    assert sum(q.count("site:") for q in queries) == 20

    api = Api(brave_urls=["https://substack.example/x"])
    q = {"keywords": ["tool"], "domains": domains, "params": {"provider": "brave"}}
    async with make_services(search_source("search_api", q), api) as svc:
        r = (await run_source(svc, svc.sources.get("search_api"), since=SINCE)).result
        brave_calls = [x for x in api.requests if x.url.host == "api.search.brave.com"]
        assert len(brave_calls) == len(queries) and r.fetched == 0
        assert brave_calls[0].headers["x-subscription-token"] == "brave-test"
        assert brave_calls[0].url.params["freshness"].startswith("2026-09-01to")
        assert svc.costs.provider_spent_this_month("brave") == pytest.approx(0.005 * len(queries))


async def test_provider_budget_is_shared_across_sources(make_services):
    api = Api(exa_urls=[])
    q = {"keywords": ["x"], "params": {"provider": "exa"}}
    cfg = search_source("search_api", q, provider_budgets={"exa": 0.01})
    async with make_services(cfg, api) as svc:
        svc.costs.record("bluesky", usd=0.005, provider="exa")    # another source used half the cap
        r = (await run_source(svc, svc.sources.get("search_api"), since=SINCE)).result
    assert "provider exa" in r.errors[0] and not [x for x in api.requests if x.url.host == "api.exa.ai"]


@pytest.mark.parametrize("url, feed", [
    ("https://medium.com/@pat/why-intake-is-broken-50e04eb1e905", "https://medium.com/feed/@pat"),
    ("https://medium.com/better-law/some-post-50e04eb1e905", "https://medium.com/feed/better-law"),
    ("https://pat.medium.com/some-post-50e04eb1e905", "https://pat.medium.com/feed"),
    ("https://medium.com/p/50e04eb1e905", None),
    ("https://medium.com/@pat", None),
])
def test_medium_feed_urls(url, feed):
    assert MediumResolver(None, None).feed_url(url) == feed


@pytest.mark.parametrize("url, feed", [
    ("https://legalops.substack.com/p/intake", "https://legalops.substack.com/feed"),
    ("https://substack.com/@pat/p-123456", None),
    ("https://legalops.substack.com/archive", None),
])
def test_substack_feed_urls(url, feed):
    assert SubstackResolver(None, None).feed_url(url) == feed


def test_bluesky_url_parsing():
    assert BlueskyResolver.parse("https://bsky.app/profile/pat.bsky.social/post/3abc") == ("pat.bsky.social", "3abc")
    assert BlueskyResolver.parse("https://bsky.app/profile/pat.bsky.social") is None
    assert BlueskyResolver.parse("https://example.com/profile/x/post/y") is None


async def test_deletion_recheck_purges_gone_and_hidden_posts(make_services):
    api = Api(exa_urls=[f"https://bsky.app/profile/{DID}/post/{k}" for k in ("1aa", "2bb", "3cc")],
              posts=[bsky_post("1aa"), bsky_post("2bb", text="another long enough post about manual billing work"),
                     bsky_post("3cc", text="third post, also long enough to be distinct from the others")])
    cfg = search_source("bluesky", {"keywords": ["x"], "domains": ["bsky.app"], "params": {"provider": "exa"}})
    async with make_services(cfg, api) as svc:
        src = svc.sources.get("bluesky")
        assert (await run_source(svc, src, since=SINCE)).result.new == 3
        del api.posts[f"at://{DID}/app.bsky.feed.post/2bb"]                          # deleted by its author
        api.posts[f"at://{DID}/app.bsky.feed.post/3cc"] = bsky_post("3cc", hidden=True)  # author opted out
        assert await recheck_deletions(svc, src) == 2
        assert svc.store.count_posts("bluesky") == 1


OFFICIAL = ["https://bsky.app/profile/bsky.app/post/3mxhdhodv222n", "https://bsky.app/profile/bsky.app/post/3mx5e63uvns2d"]


@signal_vcr.use_cassette("bluesky_getposts.yaml")
async def test_cassette_bluesky_public_api(settings, monkeypatch):
    """Recorded Bluesky API responses for two posts by Bluesky's own account (no search output stored)."""
    from signal_connectors.discovery import Discovery

    async def fixed_urls(self, *args, **kwargs):
        return OFFICIAL

    monkeypatch.setattr(Discovery, "urls", fixed_urls)
    sources = SourcesFile.model_validate(search_source(
        "bluesky", {"keywords": ["x"], "domains": ["bsky.app"], "params": {"provider": "exa"}}))
    async with Services.open(settings, sources) as svc:
        out = await run_source(svc, svc.sources.get("bluesky"), since=SINCE, collect=True)
    assert out.result.status == "ok" and len(out.posts) == 2
    for p in out.posts:
        assert p.source == "bluesky" and p.url.startswith("https://bsky.app/profile/did:plc:")
        assert p.text and p.published_at > SINCE and set(p.engagement) == {"like", "reply", "repost", "quote"}
