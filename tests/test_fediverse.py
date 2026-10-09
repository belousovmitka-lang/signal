from datetime import datetime, timezone

import httpx
import pytest

from signal_connectors.connectors.mastodon import consents, status_api_url
from signal_connectors.connectors.lemmy import post_api_url
from signal_connectors.config import SourcesFile
from signal_connectors.runner import recheck_deletions, run_source
from signal_connectors.services import Services

from .conftest import signal_vcr

SINCE = datetime(2026, 10, 1, tzinfo=timezone.utc)
CASSETTE_SINCE = datetime(2026, 9, 25, tzinfo=timezone.utc)


def account(**kw):
    a = {"acct": "pat", "bot": False, "noindex": False, "indexable": True, "note": "<p>Paralegal in Ohio</p>"}
    a.update(kw)
    return a


def status(sid, created="2026-10-05T12:00:00.000Z", **kw):
    s = {"id": str(sid), "created_at": created, "visibility": "public", "language": "en", "reblog": None,
         "uri": f"https://mastodon.social/users/pat/statuses/{sid}", "url": f"https://mastodon.social/@pat/{sid}",
         "content": "<p>Intake is all manual. Is there a tool? <a href='#'>#paralegal</a></p>",
         "favourites_count": 2, "reblogs_count": 1, "replies_count": 0, "tags": [{"name": "paralegal"}],
         "account": account()}
    s.update(kw)
    return s


@pytest.mark.parametrize("acc, ok", [
    (account(), True),
    (account(indexable=False), False),
    (account(indexable=None), False),
    (account(noindex=True), False),
    (account(bot=True), False),
    (account(note="<p>Nurse. #NoBot please</p>"), False),
    (account(note="<p>#noindex</p>"), False),
])
def test_mastodon_consent(acc, ok):
    assert consents(acc) is ok


def test_status_and_post_api_urls():
    assert status_api_url("https://mastodon.social/@pat/123") == "https://mastodon.social/api/v1/statuses/123"
    assert status_api_url("https://misskey.example/notes/abc") is None
    assert post_api_url("https://lemmy.world/post/42") == "https://lemmy.world/api/v3/post?id=42"
    assert post_api_url("https://kbin.example/m/x/t/1") is None


def mastodon_source(**q):
    q.setdefault("communities", ["paralegal"])
    return {"sources": {"mastodon": {"route": "own_api", "source_type": "social",
                                     "queries": [q],
                                     "rate_limit": {"requests_per_second": 100, "domains": ["mastodon.social"]}}}}


async def test_mastodon_tag_timeline_filters_and_pages(make_services):
    page1 = [status(5), status(4, account=account(indexable=False)), status(3, visibility="unlisted"),
             status(2, reblog={"id": "x"}), status(6, url="https://misskey.example/notes/abc")]
    page2 = [status(1), status(0, created="2026-09-01T00:00:00.000Z"), status(-1)]
    calls = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        calls.append(str(request.url))
        if "max_id" in str(request.url):
            return httpx.Response(200, json=page2)
        return httpx.Response(200, json=page1, headers={
            "link": '<https://mastodon.social/api/v1/timelines/tag/paralegal?limit=40&max_id=2>; rel="next"'})

    async with make_services(mastodon_source(), handler) as svc:
        out = await run_source(svc, svc.sources.get("mastodon"), since=SINCE, collect=True)
    assert [p.url.rsplit("/", 1)[1] for p in out.posts] == ["5", "1"]   # consent, public, original, Mastodon URL
    p = out.posts[0]
    assert p.text == "Intake is all manual. Is there a tool? #paralegal" and p.community == "#paralegal"
    assert p.engagement == {"favourites": 2, "reblogs": 1, "replies": 0} and "pat" not in p.author_hash
    assert len(calls) == 2  # stopped at the post older than since; never asked for a third page


async def test_mastodon_recheck_purges_deleted_and_withdrawn(make_services):
    statuses = {"7": status(7), "8": status(8), "9": status(9)}

    def handler(request):
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(404)
        if path.startswith("/api/v1/timelines/tag/"):
            return httpx.Response(200, json=list(statuses.values()))
        sid = path.rsplit("/", 1)[1]
        return httpx.Response(200, json=statuses[sid]) if sid in statuses else httpx.Response(404)

    async with make_services(mastodon_source(), handler) as svc:
        src = svc.sources.get("mastodon")
        statuses["8"]["content"] = "<p>Another long post about manual billing work, different enough.</p>"
        statuses["9"]["content"] = "<p>A third post about chasing signatures by email for every single client.</p>"
        assert (await run_source(svc, src, since=SINCE)).result.new == 3
        del statuses["8"]                                             # deleted
        statuses["9"] = status(9, account=account(indexable=False))   # consent withdrawn
        assert await recheck_deletions(svc, src) == 2
        assert svc.store.count_posts("mastodon") == 1


def lemmy_view(pid, published="2026-10-05T12:00:00Z", **post):
    p = {"id": pid, "name": f"Is there a tool for intake? #{pid}", "body": "We do it in spreadsheets.",
         "ap_id": f"https://lemmy.world/post/{pid}", "published": published, "deleted": False, "removed": False,
         "url": "https://ext.example/x"}
    p.update(post)
    return {"post": p, "creator": {"actor_id": "https://lemmy.world/u/pat", "bot_account": False},
            "community": {"id": 1, "name": "smallbusiness", "actor_id": "https://lemmy.world/c/smallbusiness"},
            "counts": {"score": 5, "comments": 2, "upvotes": 5}}


async def test_lemmy_search_skips_removed_bots_and_old(make_services):
    bot = lemmy_view(3)
    bot["creator"]["bot_account"] = True
    views = [lemmy_view(1), lemmy_view(2, removed=True), bot, lemmy_view(4, ap_id="https://kbin.example/m/x/t/4"),
             lemmy_view(5, published="2026-09-01T00:00:00Z"), lemmy_view(6)]
    seen = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"posts": views})

    cfg = {"sources": {"lemmy": {"route": "own_api", "source_type": "community",
                                 "queries": [{"keywords": ["is there a tool"]}],
                                 "rate_limit": {"requests_per_second": 100, "domains": ["lemmy.world"]}}}}
    async with make_services(cfg, handler) as svc:
        out = await run_source(svc, svc.sources.get("lemmy"), since=SINCE, collect=True)
    assert [p.url for p in out.posts] == ["https://lemmy.world/post/1"]
    p = out.posts[0]
    assert p.title.startswith("Is there a tool") and p.community == "!smallbusiness@lemmy.world"
    assert p.engagement == {"score": 5, "comments": 2, "upvotes": 5} and p.query == "is there a tool"
    assert seen[0]["type_"] == "Posts" and seen[0]["sort"] == "New" and len(seen) == 1


@signal_vcr.use_cassette("mastodon.yaml")
async def test_cassette_mastodon_tag_timeline(settings):
    sources = SourcesFile.model_validate(mastodon_source(communities=["smallbusiness", "sysadmin"], limit=25))
    async with Services.open(settings, sources) as svc:
        out = await run_source(svc, svc.sources.get("mastodon"), since=CASSETTE_SINCE, collect=True)
    assert out.result.status == "ok" and out.result.invalid == 0 and out.result.fetched >= 20
    for p in out.posts:
        assert p.source == "mastodon" and p.route == "own_api" and status_api_url(p.url)
        assert p.text and p.published_at >= CASSETTE_SINCE and p.community in {"#smallbusiness", "#sysadmin"}
        assert set(p.engagement) <= {"favourites", "reblogs", "replies"}


@signal_vcr.use_cassette("lemmy.yaml")
async def test_cassette_lemmy_search(settings):
    sources = SourcesFile.model_validate({"sources": {"lemmy": {
        "route": "own_api", "source_type": "community",
        "queries": [{"keywords": ["is there a tool"], "limit": 25}]}}})
    async with Services.open(settings, sources) as svc:
        # Replay needn't honor lemmy.world's 60s crawl delay (tested in test_robots).
        svc.http.limiter.set_rate = svc.http.limiter.defer = lambda *a: None
        svc.http.limiter.default_rps = 1000
        out = await run_source(svc, svc.sources.get("lemmy"), since=CASSETTE_SINCE, collect=True)
    assert out.result.status == "ok" and out.result.invalid == 0 and out.result.fetched >= 20
    for p in out.posts:
        assert p.source == "lemmy" and post_api_url(p.url) and p.title
        assert p.community and p.community.startswith("!") and p.published_at >= CASSETTE_SINCE
