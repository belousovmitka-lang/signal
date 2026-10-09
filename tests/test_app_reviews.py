from datetime import datetime, timezone

import httpx

from signal_connectors.config import SourcesFile
from signal_connectors.runner import run_source
from signal_connectors.services import Services

from .conftest import signal_vcr

LONG_AGO = datetime(2020, 1, 1, tzinfo=timezone.utc)

REVIEWS = """<?xml version="1.0"?><rss version="2.0"><channel><title>WordPress.org Forums » [Tutor LMS] Reviews</title>
<item><title>Breaks after every update (1 star)</title><link>https://wordpress.org/support/topic/breaks-after-every-update/</link>
<pubDate>Sat, 03 Oct 2026 16:07:25 +0000</pubDate><dc:creator>someone</dc:creator>
<description><![CDATA[<p>Replies: 2</p><p>Rating: 1 star</p><p>Quizzes vanish after each update. We grade by hand now.</p>]]></description></item>
</channel></rss>"""


def source(*queries):
    return {"sources": {"app_reviews": {"route": "rss", "source_type": "review", "queries": list(queries)}}}


async def test_rating_and_replies_parsed_and_stripped(make_services):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        assert request.url.path == "/support/plugin/tutor/reviews/feed/"
        return httpx.Response(200, text=REVIEWS)

    async with make_services(source({"industry_hint": "education", "params": {"plugin": "tutor"}}), handler) as svc:
        out = await run_source(svc, svc.sources.get("app_reviews"), since=LONG_AGO, collect=True)
    (p,) = out.posts
    assert p.source == "wordpress:tutor" and p.source_type == "review" and p.route == "rss"
    assert p.title == "Breaks after every update" and p.engagement == {"rating": 1, "replies": 2}
    assert p.text == "Quizzes vanish after each update. We grade by hand now."
    assert p.community == "WordPress plugin: tutor" and p.industry_hint == "education"


async def test_bad_query_fails_alone(make_services):
    async with make_services(source({"params": {"kind": "reviews"}}), lambda r: httpx.Response(404)) as svc:
        r = (await run_source(svc, svc.sources.get("app_reviews"), since=LONG_AGO)).result
    assert r.status == "failed" and "needs params.plugin" in r.errors[0]


@signal_vcr.use_cassette("app_reviews_wordpress.yaml")
async def test_cassette_wordpress_feeds(settings):
    sources = SourcesFile.model_validate(source(
        {"industry_hint": "ecommerce", "params": {"plugin": "woocommerce", "kind": "reviews"}},
        {"industry_hint": "ecommerce", "params": {"plugin": "woocommerce", "kind": "support"}},
    ))
    async with Services.open(settings, sources) as svc:
        out = await run_source(svc, svc.sources.get("app_reviews"), since=LONG_AGO, collect=True)
    assert out.result.status == "ok", out.result.errors
    assert out.result.invalid == 0 and out.result.fetched >= 20
    reviews = [p for p in out.posts if p.source == "wordpress:woocommerce"]
    support = [p for p in out.posts if p.source == "wordpress:woocommerce:support"]
    assert reviews and support
    assert all(1 <= p.engagement["rating"] <= 5 and "stars)" not in (p.title or "") for p in reviews)
    assert all(p.source_type == "community" and "rating" not in p.engagement for p in support)
    assert all("Rating:" not in p.text and "Replies:" not in p.text for p in out.posts)
