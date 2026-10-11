import json
from datetime import date

import httpx
import pytest

from signal_connectors.discovery_agent import (Seeds, base_domain, build_queries, discover, group_origins,
                                               jargon_candidates, origin_of)
from signal_connectors.industries import load_industry, save_industry

SEEDS = Seeds("veterinary", "Veterinary clinics", ["veterinarian", "vet tech"], ["vet clinic"], ["ezyVet"])


def test_queries_and_origins():
    q = build_queries(SEEDS)
    assert {p for p, _ in q} == {"exa", "brave"} and len(q) == 7    # 2 roles + 3 topics + 1 keyword + 1 association
    assert origin_of("https://www.reddit.com/r/vettech/comments/x") is None
    assert base_domain("forums.studentdoctor.net") == base_domain("www.studentdoctor.net") == "studentdoctor.net"
    assert base_domain("www.vetnurse.co.uk") == "vetnurse.co.uk"
    assert origin_of("https://forum.vetvine.com/threads/123") == "https://forum.vetvine.com"
    groups = group_origins(["https://a.example/forums/topic/1", "https://a.example/x", "https://youtube.com/watch?v=1"])
    assert list(groups) == ["https://a.example"] and groups["https://a.example"].hits == 2
    assert groups["https://a.example"].paths == {"/forums/"}


def test_jargon_candidates_skip_stopwords_and_seeds():
    titles = ["New CMA leaflet for small animal practices", "CMA leaflet update: small animal pricing",
              "Forum post: small animal dental", "Blog post re CMA leaflet"] * 2
    j = jargon_candidates(titles, SEEDS)
    assert "cma leaflet" in j and "small animal" in j
    assert not any(w in ("forum post", "blog post", "post re") for w in j)


RSS = ('<?xml version="1.0"?><rss version="2.0"><channel><title>Vet News</title>'
       + "".join(f"<item><title>Small animal pricing story {i}</title><link>https://news.example/{i}</link></item>"
                 for i in range(5)) + "</channel></rss>")


class Web:
    """Exa returns pointers; each site then answers for itself."""

    def __init__(self):
        self.exa_bodies = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "api.exa.ai":
            self.exa_bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"results": [
                {"url": "https://forum.vets.example/forums/clinic/1", "title": "SEARCH TITLE"},
                {"url": "https://news.example/story"}, {"url": "https://assoc.example/about"},
                {"url": "https://www.reddit.com/r/vettech/x"}, {"url": "https://www.studentdoctor.net/threads/1"},
                {"url": "https://vetboard.forumotion.example/forum/t1"}, {"url": "https://club.example/community/"},
                {"url": "https://www.vettalk.example/forums/x"}],
                "costDollars": {"total": 0.007}})
        if host == "api.search.brave.com":
            return httpx.Response(200, json={"web": {"results": []}, "query": {}})
        if path == "/robots.txt":
            return httpx.Response(404)
        if host == "forum.vets.example":
            if path.endswith(".rss"):
                return httpx.Response(200, text=RSS)
            return httpx.Response(200, headers={"content-type": "text/html"},
                                  text='<html id="XF"><title>Vets Forum</title><a href="/help/terms">Terms</a></html>')
        if host == "news.example":
            if path == "/feed":
                return httpx.Response(200, text=RSS)
            return httpx.Response(200, headers={"content-type": "text/html"},
                                  text='<title>Vet News</title><link rel="alternate" type="application/rss+xml" href="/feed">')
        if host == "assoc.example":
            return httpx.Response(200, headers={"content-type": "text/html"},
                                  text="<title>National Veterinary Association</title>")
        if host == "vetboard.forumotion.example":
            return httpx.Response(200, headers={"content-type": "text/html"},
                                  text='<title>Vet Board</title><a href="https://www.forumotion.com">Free forum</a>')
        if host == "www.vettalk.example":
            return httpx.Response(200, headers={"content-type": "text/html"},
                                  text='<html id="XF"><title>Vet Talk</title><footer>VerticalScope Inc.</footer></html>')
        if host == "club.example":
            return httpx.Response(200, headers={"content-type": "text/html"},
                                  text='<title>Vet Club</title><script src="https://app.circle.so/x.js"></script>')
        if host == "www.studentdoctor.net":
            return httpx.Response(200, headers={"content-type": "text/html"},
                                  text='<html id="XF"><title>Student Doctor Network</title></html>')
        return httpx.Response(404)


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "exa-test")
    monkeypatch.setenv("BRAVE_API_KEY", "brave-test")


async def test_discover_writes_a_reviewable_draft(make_services, tmp_path):
    web = Web()
    cfg = {"defaults": {"provider_budgets": {"exa": 1, "brave": 1}, "discovery_monthly_budget_usd": 1}, "sources": {}}
    async with make_services(cfg, web) as svc:
        ind = await discover(svc, SEEDS, budget_usd=0.03, today=date(2026, 10, 9))
    assert ind.discovery.needs_review and ind.discovery.queries == 4      # budget stops at 4 Exa searches
    assert "excludeDomains" in web.exa_bodies[0] and "reddit.com" in web.exa_bodies[0]["excludeDomains"]
    forums = {f.name: f for f in ind.forums}
    vets = forums["Vets Forum"]
    assert vets.software == "xenforo" and vets.terms == "unchecked" and vets.terms_url == "https://forum.vets.example/help/terms"
    assert vets.url == "https://forum.vets.example/forums/" and vets.status == "live"   # trimmed to the forum root
    sdn = forums["Student Doctor Network"]
    assert sdn.terms == "forbidden" and "carried over" in sdn.terms_notes    # decided in the healthcare map
    board = forums["Vet Board"]
    assert board.terms == "forbidden" and "Forumotion" in board.terms_notes       # platform-wide terms
    assert forums["Vet Talk"].terms == "forbidden" and "VerticalScope" in forums["Vet Talk"].terms_notes
    assert "Vet Club" not in forums                                              # private Circle community
    assert [f.url for f in ind.rss_feeds] == ["https://news.example/feed"] and ind.rss_feeds[0].status == "live"
    assert [a.name for a in ind.associations] == ["National Veterinary Association"]
    assert all("SEARCH TITLE" not in f.name for f in ind.forums)          # names come from the sites
    path = tmp_path / "veterinary.yaml"
    save_industry(ind, path)
    assert load_industry(path).discovery.needs_review


def test_unreviewed_drafts_are_not_collected(tmp_path):
    from signal_connectors.config import load_sources
    from signal_connectors.industries import DiscoveryInfo, Feed, Industry
    draft = Industry(industry="vet", name="Vet", discovery=DiscoveryInfo(generated_at=date(2026, 10, 9)),
                     rss_feeds=[Feed(name="x", url="https://news.example/feed", status="live")])
    maps = tmp_path / "industries"
    maps.mkdir()
    save_industry(draft, maps / "vet.yaml")
    sources_yaml = tmp_path / "sources.yaml"
    sources_yaml.write_text("sources:\n  rss:\n    route: rss\n    source_type: news\n    options: {industry_feeds: true}\n")
    assert load_sources(sources_yaml, industries_dir=maps).get("rss").queries == []
    draft.discovery.needs_review = False
    save_industry(draft, maps / "vet.yaml")
    assert len(load_sources(sources_yaml, industries_dir=maps).get("rss").queries) == 1


def test_forum_feeds_are_recognised():
    from signal_connectors.forum_detect import FORUM_FEED_PATTERNS
    for url in ["http://autotechsonline.proboards.com/rss/public", "https://www.autoprofessionals.org/syndication.php",
                "https://x.example/forums/-/index.rss", "https://x.example/external.php?type=RSS2"]:
        assert FORUM_FEED_PATTERNS.search(url), url
    assert not FORUM_FEED_PATTERNS.search("https://www.ratchetandwrench.com/feed/")
