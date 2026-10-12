import re
from datetime import datetime, timezone

import httpx

from signal_connectors.config import SourcesFile
from signal_connectors.connectors.job_boards import find_boards, parse_board, pay_from_text, same_company, slugs
from signal_connectors.industries import Industry, Link
from signal_connectors.privacy import redact_contacts
from signal_connectors.runner import run_source
from signal_connectors.services import Services

from .conftest import signal_vcr

SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def src(*boards):
    return {"sources": {"job_boards": {"route": "own_api", "source_type": "paid_demand",
                                       "queries": [{"params": {"board": b, "company": c}} for b, c in boards]}}}


def test_parse_board_slugs_and_pay():
    assert parse_board("https://boards.greenhouse.io/acme") == ("greenhouse", "acme")
    assert parse_board("https://jobs.lever.co/acme/123") == ("lever", "acme")
    assert parse_board("https://jobs.ashbyhq.com/moego") == ("ashby", "moego")
    assert parse_board("https://www.dice.com/") is None
    assert slugs("Zen Planner") == ["zenplanner", "zen-planner"] and slugs("Jo") == []
    assert pay_from_text("Salary: $90,000 - $110,000 a year")["min"] == 90000
    assert pay_from_text("$90K – $110K • Offers Bonus") | {} == {"min": 90000, "max": 110000, "currency": "USD",
                                                                 "stated": "$90K – $110K"}
    assert pay_from_text("401(k) and 20 days off") is None
    assert same_company("Weave", "Weave") and same_company("Toast, Inc.", "Toast") and same_company("Gusto HQ", "Gusto")
    assert not same_company("Cornerstone Child Development Center", "Cornerstone")


def test_redact_contacts():
    text = "Questions? jane.doe@acme.com or (555) 123-4567. Pay $90,000 - $110,000, start 2026-10-11."
    assert redact_contacts(text) == "Questions? [email] or [phone]. Pay $90,000 - $110,000, start 2026-10-11."


def test_only_ats_boards_become_queries():
    sources = SourcesFile.model_validate({"sources": {"job_boards": {
        "route": "own_api", "source_type": "paid_demand", "options": {"industry_job_boards": True}}}})
    ind = Industry(industry="legal", name="Legal", job_boards=[
        Link(name="Clio", url="https://jobs.lever.co/clio", status="live"),
        Link(name="LawCrossing", url="https://www.lawcrossing.com/", status="live"),
        Link(name="Old", url="https://jobs.ashbyhq.com/old", status="dead")])
    sources.add_industry_job_boards({"legal": ind})
    assert [q.params for q in sources.get("job_boards").queries] == [
        {"board": "https://jobs.lever.co/clio", "company": "Clio"}]


GH_JOB = {"id": 1, "title": "Billing Specialist", "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
          "first_published": "2026-09-01T10:00:00-04:00", "updated_at": "2026-10-01T10:00:00-04:00",
          "location": {"name": "Remote"}, "departments": [{"name": "Finance"}],
          "content": "&lt;p&gt;Chase invoices. Email hr@acme.com. Pay $60,000 - $70,000.&lt;/p&gt;"}
LEVER_JOB = {"id": "a1", "text": "Implementation Manager", "hostedUrl": "https://jobs.lever.co/acme/a1",
             "createdAt": 1790000000000, "categories": {"location": "Austin", "team": "Onboarding"},
             "descriptionPlain": "Onboard clinics to Acme.", "lists": [{"text": "You will", "content": "<li>Migrate data</li>"}],
             "salaryRange": {"min": 80000, "max": 95000, "currency": "USD", "interval": "per-year-salary"}}
ASHBY_JOB = {"id": "b2", "title": "Support Lead", "jobUrl": "https://jobs.ashbyhq.com/acme/b2", "isListed": True,
             "publishedAt": "2026-09-09T20:03:11.798+00:00", "descriptionPlain": "Help Acme customers.",
             "compensation": {"compensationTierSummary": "$90K – $110K • Offers Bonus"}}


def ats(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/robots.txt":
        return httpx.Response(404)
    host = request.url.host
    if host == "boards-api.greenhouse.io":
        return httpx.Response(200, json={"jobs": [GH_JOB]} if request.url.path.endswith("/jobs") else {"name": "Acme"})
    if host == "api.lever.co":
        return httpx.Response(200, json=[LEVER_JOB])
    if host == "api.ashbyhq.com":
        return httpx.Response(200, json={"jobs": [ASHBY_JOB, {**ASHBY_JOB, "id": "x", "isListed": False}]})
    return httpx.Response(404)


async def test_mapping_for_each_ats(make_services):
    cfg = src(("https://boards.greenhouse.io/acme", "Acme"), ("https://jobs.lever.co/acme", "Acme"),
              ("https://jobs.ashbyhq.com/acme", "Acme"))
    async with make_services(cfg, ats) as svc:
        out = await run_source(svc, svc.sources.get("job_boards"), since=SINCE, collect=True)
    assert out.result.status == "ok" and out.result.new == 3        # the unlisted Ashby job is skipped
    posts = {p.raw["ats"]: p for p in out.posts}
    gh = posts["greenhouse"]
    assert gh.title == "Billing Specialist" and gh.community == "Acme" and gh.source_type == "paid_demand"
    assert "[email]" in gh.text and "hr@acme.com" not in gh.text and gh.money["min"] == 60000
    assert gh.published_at == datetime(2026, 9, 1, 14, tzinfo=timezone.utc) and gh.raw["departments"] == ["Finance"]
    lever = posts["lever"]
    assert lever.money == {"min": 80000, "max": 95000, "currency": "USD", "interval": "per-year-salary"}
    assert "Migrate data" in lever.text and lever.raw["team"] == "Onboarding"
    assert posts["ashby"].money["max"] == 110000 and posts["ashby"].money["stated"].startswith("$90K")
    assert all(p.author_hash is None for p in out.posts)


async def test_find_boards_needs_proof_it_is_the_company(make_services):
    async with make_services(None, ats) as svc:
        found = await find_boards(svc.http, svc.robots, "Acme")
        assert {(b.ats, b.token) for b in found} == {("greenhouse", "acme"), ("lever", "acme"), ("ashby", "acme")}
        assert [b.url for b in found if b.ats == "lever"] == ["https://jobs.lever.co/acme"]
        # a generic name: the board exists but neither its name nor its posts mention the tool
        assert await find_boards(svc.http, svc.robots, "Playground") == []


@signal_vcr.use_cassette("job_boards.yaml")
async def test_cassette_job_boards(settings):
    sources = SourcesFile.model_validate(src(("https://boards.greenhouse.io/tekmetric", "Tekmetric"),
                                             ("https://jobs.lever.co/pushpress", "PushPress"),
                                             ("https://jobs.ashbyhq.com/moego", "MoeGo")))
    async with Services.open(settings, sources) as svc:
        out = await run_source(svc, svc.sources.get("job_boards"), since=datetime(2020, 1, 1, tzinfo=timezone.utc),
                               collect=True)
    assert out.result.status == "ok" and out.result.invalid == 0 and out.result.new >= 3
    assert {p.raw["ats"] for p in out.posts} == {"greenhouse", "lever", "ashby"}
    assert all(p.source == "job_boards" and p.title and p.text and p.url.startswith("https://") for p in out.posts)
    assert not any(re.search(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", p.text) for p in out.posts)   # contacts redacted
