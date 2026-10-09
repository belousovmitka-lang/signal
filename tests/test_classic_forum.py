from datetime import datetime, timezone

import pytest

from signal_connectors.config import SourcesFile
from signal_connectors.industries import Forum, Industry
from signal_connectors.runner import run_source
from signal_connectors.services import Services

from .conftest import signal_vcr

LONG_AGO = datetime(2020, 1, 1, tzinfo=timezone.utc)


def forum(name, **kw):
    base = dict(name=name, url=f"https://{name}.example/", status="live", robots="allowed", terms="allowed",
                rss=f"https://{name}.example/forums/-/index.rss", software="xenforo")
    base.update(kw)
    return Forum(**base)


def test_only_collectable_forums_become_queries():
    ind = Industry(industry="logistics", name="Logistics", forums=[
        forum("ok"),
        forum("forbidden", terms="forbidden"),
        forum("unchecked", terms="unchecked"),
        forum("norobots", robots="disallowed"),
        forum("nofeed", rss=None),
        forum("dead", status="dead"),
    ])
    sources = SourcesFile.model_validate({"sources": {"classic_forum": {
        "route": "rss", "source_type": "community", "options": {"industry_forums": True}}}})
    sources.add_industry_forums({"logistics": ind})
    queries = sources.get("classic_forum").queries
    assert [q.params["name"] for q in queries] == ["ok.example"]
    assert queries[0].industry_hint == "logistics" and queries[0].params["software"] == "xenforo"


@signal_vcr.use_cassette("classic_forum.yaml")
async def test_cassette_xenforo_feeds(settings):
    sources = SourcesFile.model_validate({"sources": {"classic_forum": {
        "route": "rss", "source_type": "community",
        "queries": [
            {"industry_hint": "logistics", "params": {
                "feed": "https://www.thetruckersreport.com/truckingindustryforum/forums/-/index.rss",
                "name": "thetruckersreport.com", "community": "TruckersReport", "software": "xenforo"}},
            {"industry_hint": "manufacturing", "params": {
                "feed": "https://www.plctalk.net/forums/forums/-/index.rss",
                "name": "plctalk.net", "community": "PLCtalk", "software": "xenforo"}},
        ]}}})
    async with Services.open(settings, sources) as svc:
        out = await run_source(svc, svc.sources.get("classic_forum"), since=LONG_AGO, collect=True)
    r = out.result
    assert r.status == "ok" and r.invalid == 0 and r.fetched >= 20
    sources_seen = {p.source for p in out.posts}
    assert sources_seen == {"forum:thetruckersreport.com", "forum:plctalk.net"}
    for p in out.posts:
        assert p.source_type == "community" and p.route == "rss" and p.title
        assert "%2F" not in p.url and not p.url.endswith("=")   # XenForo query paths kept intact
        assert set(p.engagement) <= {"replies"} and p.raw["software"] == "xenforo"
        assert p.author_hash and len(p.author_hash) == 64
    plc = [p for p in out.posts if p.source == "forum:plctalk.net"]
    assert all(p.community.startswith("PLCtalk / ") for p in plc)  # board name from the item category
