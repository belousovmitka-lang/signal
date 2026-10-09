from datetime import datetime, timezone

import httpx
import pytest

from signal_connectors.config import SourcesFile
from signal_connectors.runner import recheck_deletions, run_source

SINCE = datetime(2026, 10, 5, tzinfo=timezone.utc)
T_NEW, T_OLD = 1791000000, 1700000000   # 2026-10-03.. (after since?) and 2023


def post(pid, created=1791500000, **kw):
    d = {"id": pid, "name": f"t3_{pid}", "title": "Is there a tool for conflict checks?", "subreddit": "paralegal",
         "selftext": f"I'm a paralegal and we still do conflict checks in Excel. Post {pid}.", "author": "someone",
         "created_utc": created, "score": 12, "num_comments": 4, "is_self": True,
         "permalink": f"/r/paralegal/comments/{pid}/is_there_a_tool/"}
    d.update(kw)
    return {"kind": "t3", "data": d}


def src():
    return {"sources": {"reddit": {"route": "own_api", "source_type": "community", "paid": True, "enabled": True,
                                   "feature_flag": "ENABLE_REDDIT_API", "deletion_recheck_days": 2,
                                   "options": {"user_agent": "web:signal:0.1 (by /u/signal)", "roles": ["paralegal"]},
                                   "queries": [{"communities": ["paralegal"], "industry_hint": "legal", "limit": 50}]}}}


class Api:
    def __init__(self, listing):
        self.listing, self.tokens, self.calls, self.info = listing, 0, [], {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.reddit.com":
            self.tokens += 1
            assert request.headers["authorization"].startswith("Basic ")
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        assert request.headers["authorization"] == "Bearer tok"
        assert request.headers["user-agent"] == "web:signal:0.1 (by /u/signal)"
        self.calls.append(request.url)
        if request.url.path == "/api/info":
            ids = request.url.params["id"].split(",")
            return httpx.Response(200, json={"data": {"children": [self.info[i] for i in ids if i in self.info]}})
        after = request.url.params.get("after")
        return httpx.Response(200, json={"data": self.listing[after]})


@pytest.fixture(autouse=True)
def creds(monkeypatch):
    monkeypatch.setenv("REDDIT_CLIENT_ID", "cid")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")


async def test_listing_pages_skips_removed_and_stops_at_since(make_services):
    api = Api({
        None: {"children": [post("a1"), post("a2", selftext="[removed]"), post("a3", author="[deleted]")],
               "after": "t3_a3"},
        "t3_a3": {"children": [post("a4"), post("a5", created=T_OLD), post("a6")], "after": "t3_a6"},
    })
    async with make_services(src(), api) as svc:
        out = await run_source(svc, svc.sources.get("reddit"), since=SINCE, collect=True)
    assert [p.url.split("/comments/")[1].split("/")[0] for p in out.posts] == ["a1", "a4"]
    assert api.tokens == 1 and len(api.calls) == 2      # token reused; stopped at the old post
    p = out.posts[0]
    assert p.author_role == "paralegal" and p.community == "r/paralegal" and len(p.author_hash) == 64
    assert p.engagement == {"score": 12, "comments": 4} and "someone" not in p.model_dump_json()
    assert api.calls[0].path == "/r/paralegal/new" and api.calls[0].params["raw_json"] == "1"


async def test_deletion_recheck(make_services):
    api = Api({None: {"children": [post("b1"), post("b2"), post("b3")], "after": None}})
    async with make_services(src(), api) as svc:
        src_cfg = svc.sources.get("reddit")
        assert (await run_source(svc, src_cfg, since=SINCE)).result.new == 3
        api.info = {"t3_b1": post("b1"), "t3_b2": post("b2", selftext="[deleted]")}   # b3 gone entirely
        assert await recheck_deletions(svc, src_cfg) == 2
        assert svc.store.count_posts("reddit") == 1


def test_flag_off_by_default(monkeypatch):
    monkeypatch.delenv("ENABLE_REDDIT_API", raising=False)
    assert "ENABLE_REDDIT_API" in SourcesFile.model_validate(src()).get("reddit").blocked_reason()
