from datetime import datetime, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from signal_connectors.config import SourcesFile
from signal_connectors.runner import run_source
from signal_connectors.services import Services

from .conftest import signal_vcr

SINCE = datetime(2026, 10, 1, tzinfo=timezone.utc)


def hn_source(*queries):
    return {"sources": {"hackernews": {
        "route": "own_api", "source_type": "community", "queries": list(queries),
        # fast replay; live limits come from sources.yaml
        "rate_limit": {"requests_per_second": 100, "domains": ["hn.algolia.com", "hacker-news.firebaseio.com"]},
    }}}


@signal_vcr.use_cassette("hackernews.yaml")
async def test_cassette_all_three_modes(settings):
    sources = SourcesFile.model_validate(hn_source(
        {"keywords": ["is there a tool", "wish there was"], "limit": 10},
        {"params": {"tags": "ask_hn"}, "limit": 10},
        {"params": {"list": "jobstories"}, "limit": 3},
    ))
    async with Services.open(settings, sources) as svc:
        out = await run_source(svc, svc.sources.get("hackernews"), since=SINCE, collect=True)

    r = out.result
    assert r.status == "ok" and r.invalid == 0 and r.fetched >= 20
    for p in out.posts:
        assert p.source == "hackernews" and p.route == "own_api"
        assert p.url.startswith("https://news.ycombinator.com/item?id=")
        assert p.published_at >= SINCE and p.author_hash and len(p.author_hash) == 64
        assert p.title or p.text
        assert "&#x27;" not in p.text and "<p>" not in p.text
    types = {p.raw["type"] for p in out.posts}
    assert {"story", "job"} <= types
    assert any(p.community == "Ask HN" for p in out.posts)
    assert all(p.query in {"is there a tool", "wish there was"} for p in out.posts if p.raw["type"] == "comment")


def hit(oid, *, comment=False, text="body", **kw):
    h = {"objectID": str(oid), "author": "pg", "created_at_i": 1791000000 + oid, "points": 3,
         "num_comments": 1, "_tags": ["comment" if comment else "story", f"author_pg", f"story_{oid}"]}
    if comment:
        h.update(comment_text=text, story_title="Ask HN: What&#x27;s slow?", story_id=1, parent_id=1)
    else:
        h.update(title="A &amp; B", story_text=text, url="https://ext.example/a")
    h.update(kw)
    return h


def algolia(pages):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "hn.algolia.com":
            return httpx.Response(404)
        q = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        calls.append(q)
        hits = pages.get((q["query"], int(q["page"])), [])
        n = len({p for (query, p) in pages if query == q["query"]})
        return httpx.Response(200, json={"hits": hits, "nbPages": n})

    return handler, calls


async def test_comment_mapping_paging_and_cross_phrase_dedupe(make_services):
    handler, calls = algolia({
        ('"so tired of"', 0): [hit(1, comment=True, text="I&#x27;m a paralegal.<p>So tired of this")],
        ('"so tired of"', 1): [hit(2)],
        ("manually", 0): [hit(2), hit(3)],  # 2 again: deduped within the query
    })
    async with make_services(hn_source({"keywords": ["so tired of", "manually"]}), handler) as svc:
        out = await run_source(svc, svc.sources.get("hackernews"), since=SINCE, collect=True)
    c = out.posts[0]
    assert c.title == "Ask HN: What's slow?" and c.text == "I'm a paralegal.\n\nSo tired of this"
    assert c.raw["type"] == "comment" and c.query == "so tired of" and c.engagement == {"points": 3, "comments": 1}
    assert [p.raw["objectID"] for p in out.posts] == ["1", "2", "3"]
    assert out.posts[1].title == "A & B" and out.posts[1].raw["external_url"] == "https://ext.example/a"
    assert calls[0]["numericFilters"] == f"created_at_i>{int(SINCE.timestamp())}"
    assert all("author_pg" not in t for p in out.posts for t in p.raw["tags"])


async def test_firebase_list_skips_deleted_and_stops_at_since(make_services):
    items = {
        10: {"id": 10, "type": "job", "title": "Acme (YC W26) is hiring", "by": "acme", "time": 1791000500},
        11: {"id": 11, "deleted": True, "time": 1791000400},
        12: {"id": 12, "type": "story", "title": "Ask HN: Tools?", "text": "x", "by": "a", "dead": True, "time": 1791000300},
        13: {"id": 13, "type": "job", "title": "Old job", "by": "old", "time": 1700000000},
        14: {"id": 14, "type": "job", "title": "Never read", "by": "x", "time": 1791000000},
    }
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v0/jobstories.json":
            return httpx.Response(200, json=list(items))
        item_id = int(path.rsplit("/", 1)[1].removesuffix(".json"))
        seen.append(item_id)
        return httpx.Response(200, json=items[item_id])

    async with make_services(hn_source({"params": {"list": "jobstories"}}), handler) as svc:
        out = await run_source(svc, svc.sources.get("hackernews"), since=SINCE, collect=True)
    assert [p.title for p in out.posts] == ["Acme (YC W26) is hiring"]
    assert out.posts[0].community == "HN Jobs"
    assert seen == [10, 11, 12, 13]  # stopped at the first item older than since


async def test_query_without_keywords_tags_or_list_is_an_error(make_services):
    async with make_services(hn_source({}), algolia({})[0]) as svc:
        r = (await run_source(svc, svc.sources.get("hackernews"), since=SINCE)).result
    assert r.status == "failed" and "needs keywords" in r.errors[0]
