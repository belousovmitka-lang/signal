from datetime import datetime, timezone

import httpx
import pytest

from signal_connectors.config import SourcesFile
from signal_connectors.runner import run_source
from signal_connectors.services import Services

from .conftest import signal_vcr

SINCE = datetime(2026, 10, 6, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "gh-test")
    monkeypatch.setenv("COURTLISTENER_TOKEN", "cl-test")
    monkeypatch.setenv("OPENSTATES_API_KEY", "os-test")


def src(name, *queries):
    return {"sources": {name: {"route": "own_api", "source_type": "community", "queries": list(queries)}}}


async def test_github_issues_bounties_and_prs_skipped(make_services):
    seen = []
    issue = {"html_url": "https://github.com/acme/app/issues/7", "title": "[Bounty $500] Add CSV export",
             "body": "Customers paste into Excel by hand.", "user": {"login": "octo"}, "created_at": "2026-10-07T10:00:00Z",
             "repository_url": "https://api.github.com/repos/acme/app", "labels": [{"name": "💎 Bounty"}],
             "comments": 2, "reactions": {"total_count": 5}, "number": 7, "state": "open"}

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"items": [issue, {**issue, "html_url": "https://github.com/acme/app/pull/8",
                                                            "pull_request": {}}]})

    async with make_services(src("github", {"params": {"kind": "issues"}, "keywords": ['label:"💎 Bounty"']}), handler) as svc:
        out = await run_source(svc, svc.sources.get("github"), since=SINCE, collect=True)
    (p,) = out.posts
    assert p.source == "github:issues" and p.source_type == "paid_demand" and p.community == "acme/app"
    assert p.engagement == {"comments": 2, "reactions": 5} and "octo" not in p.model_dump_json()
    q = seen[0].url.params["q"]
    assert q == 'label:"💎 Bounty" is:issue is:open created:>=2026-10-06T00:00:00Z'
    assert seen[0].headers["authorization"] == "Bearer gh-test" and seen[0].url.params["sort"] == "created"


async def test_github_repos(make_services):
    repo = {"html_url": "https://github.com/acme/intake", "full_name": "acme/intake", "description": "Law firm intake",
            "topics": ["legal"], "language": "Python", "created_at": "2026-10-07T00:00:00Z", "stargazers_count": 120,
            "forks_count": 4, "open_issues_count": 1}
    seen = []
    async with make_services(src("github", {"params": {"kind": "repos", "min_stars": 100}}),
                             lambda r: seen.append(r) or httpx.Response(200, json={"items": [repo]})) as svc:
        out = await run_source(svc, svc.sources.get("github"), since=SINCE, collect=True)
    assert out.posts[0].source_type == "startup" and out.posts[0].engagement["stars"] == 120
    assert "stars:>=100" in seen[0].url.params["q"] and seen[0].url.path == "/search/repositories"


async def test_courtlistener_quota_and_mapping(make_services):
    result = {"absolute_url": "/opinion/1/acme-v-beta/", "caseName": "Acme LLC v. Beta Inc", "court": "D. Del.",
              "dateFiled": "2026-10-07", "docketNumber": "1:26-cv-1", "attorney": "Jane Lawyer", "judge": "Hon. X",
              "opinions": [{"snippet": "breach of the <mark>software license</mark>"}]}
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"results": [result], "next": None})

    cfg = src("courtlistener", {"keywords": ['"software license" AND breach'], "industry_hint": "it_devtools"})
    cfg["sources"]["courtlistener"]["quota"] = {"units": 5, "window_days": 1}
    async with make_services(cfg, handler) as svc:
        out = await run_source(svc, svc.sources.get("courtlistener"), since=SINCE, collect=True)
    (p,) = out.posts
    assert p.url == "https://www.courtlistener.com/opinion/1/acme-v-beta" and p.text == "breach of the software license"
    assert "Jane Lawyer" not in p.model_dump_json() and "Hon. X" not in p.model_dump_json()
    assert seen[0].url.params["filed_after"] == "2026-10-06" and seen[0].headers["authorization"] == "Token cl-test"


async def test_openstates_paging_and_mapping(make_services):
    bill = {"openstates_url": "https://openstates.org/nj/bills/222/S1779/", "identifier": "S 1779",
            "title": "Downtown growth program", "jurisdiction": {"name": "New Jersey"},
            "latest_action_date": "2026-10-08T04:00:00+00:00", "latest_action_description": "Referred to committee",
            "abstracts": [{"abstract": "Helps small municipalities."}]}
    seen = []

    def handler(request):
        page = int(request.url.params["page"])
        seen.append(page)
        return httpx.Response(200, json={"results": [{**bill, "openstates_url": bill["openstates_url"] + str(page)}],
                                         "pagination": {"max_page": 2}})

    async with make_services(src("openstates", {"keywords": ["small business"], "limit": 10}), handler) as svc:
        out = await run_source(svc, svc.sources.get("openstates"), since=SINCE, collect=True)
    assert seen == [1, 2] and len(out.posts) == 2
    p = out.posts[0]
    assert p.title == "New Jersey S 1779: Downtown growth program" and p.community == "New Jersey"
    assert p.text == "Helps small municipalities. Latest action: Referred to committee."


def _real_key(monkeypatch, name):
    import os
    from dotenv import dotenv_values
    if os.environ.get("VCR_RECORD_MODE") == "once":
        monkeypatch.setenv(name, dotenv_values(".env")[name])


@signal_vcr.use_cassette("github.yaml")
async def test_cassette_github(settings, monkeypatch):
    _real_key(monkeypatch, "GITHUB_TOKEN")
    sources = SourcesFile.model_validate(src("github", {"params": {"kind": "issues"}, "keywords": ["label:bounty"],
                                                        "limit": 15},
                                             {"params": {"kind": "repos", "min_stars": 50}, "limit": 10}))
    async with Services.open(settings, sources) as svc:
        out = await run_source(svc, svc.sources.get("github"), since=SINCE, collect=True)
    assert out.result.status == "ok" and out.result.fetched == 25 and out.result.invalid == 0
    assert {p.source for p in out.posts} == {"github:issues", "github:repos"}
    assert all(p.url.startswith("https://github.com/") and "/pull/" not in p.url for p in out.posts)


@signal_vcr.use_cassette("courtlistener.yaml")
async def test_cassette_courtlistener(settings, monkeypatch):
    _real_key(monkeypatch, "COURTLISTENER_TOKEN")
    sources = SourcesFile.model_validate(src("courtlistener", {"keywords": ["contract AND software"], "limit": 20}))
    async with Services.open(settings, sources) as svc:
        out = await run_source(svc, svc.sources.get("courtlistener"),
                               since=datetime(2026, 9, 1, tzinfo=timezone.utc), collect=True)
    assert out.result.status == "ok" and out.result.fetched >= 10 and out.result.invalid == 0
    assert all(p.url.startswith("https://www.courtlistener.com/opinion/") and p.title for p in out.posts)


@signal_vcr.use_cassette("openstates.yaml")
async def test_cassette_openstates(settings, monkeypatch):
    _real_key(monkeypatch, "OPENSTATES_API_KEY")
    sources = SourcesFile.model_validate(src("openstates", {"keywords": ["small business"], "limit": 20}))
    async with Services.open(settings, sources) as svc:
        out = await run_source(svc, svc.sources.get("openstates"),
                               since=datetime(2026, 9, 15, tzinfo=timezone.utc), collect=True)
    assert out.result.status == "ok" and out.result.fetched >= 15 and out.result.invalid == 0
    assert all(p.url.startswith("https://openstates.org/") and p.community for p in out.posts)


async def test_github_discussions_graphql(make_services):
    import json as _json
    bodies = []

    def handler(request):
        body = _json.loads(request.content)
        bodies.append(body)
        node = {"title": "Is there a way to export to CSV?", "url": "https://github.com/acme/app/discussions/3",
                "createdAt": "2026-10-07T10:00:00Z", "bodyText": "We copy rows by hand.", "upvoteCount": 4,
                "answerChosenAt": None, "author": {"login": "octo"}, "category": {"name": "Q&A"},
                "repository": {"nameWithOwner": "acme/app"}, "comments": {"totalCount": 2}}
        return httpx.Response(200, json={"data": {"search": {"pageInfo": {"hasNextPage": False}, "nodes": [node, {}]}}})

    async with make_services(src("github", {"params": {"kind": "discussions"}, "keywords": ['"is there a way"']}),
                             handler) as svc:
        out = await run_source(svc, svc.sources.get("github"), since=SINCE, collect=True)
    (p,) = out.posts
    assert p.source == "github:discussions" and p.community == "acme/app" and p.raw == {"category": "Q&A", "answered": False}
    assert p.engagement == {"upvotes": 4, "comments": 2} and "octo" not in p.model_dump_json()
    assert bodies[0]["variables"]["q"] == '"is there a way" created:>=2026-10-06T00:00:00Z'


@signal_vcr.use_cassette("github_discussions.yaml")
async def test_cassette_github_discussions(settings, monkeypatch):
    _real_key(monkeypatch, "GITHUB_TOKEN")
    sources = SourcesFile.model_validate(src("github", {"params": {"kind": "discussions"},
                                                        "keywords": ['"feature request"'], "limit": 20}))
    async with Services.open(settings, sources) as svc:
        out = await run_source(svc, svc.sources.get("github"), since=SINCE, collect=True)
    assert out.result.status == "ok" and out.result.fetched == 20 and out.result.invalid == 0
    assert all("/discussions/" in p.url for p in out.posts)
