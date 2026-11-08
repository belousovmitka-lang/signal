"""Targeted search: an industry's own words on free APIs, stored as "<source>:<industry>" runs."""

from signal_connectors.industries import Industry, Vocabulary
from signal_connectors.targeted import queries, targeted_search

from .test_hackernews import algolia, hit, hn_source

TRUCKING = Industry(industry="logistics", name="Logistics and trucking", vocabulary=Vocabulary(
    roles=["dispatcher", "freight broker"], pain_phrases=["detention time", "load confirmations"],
    tasks=["re-key load confirmations"]))


def test_each_source_gets_the_industrys_words():
    from signal_connectors.targeted import anchor
    assert anchor(TRUCKING) == "logistics"
    it = Industry(industry="it_devtools", name="IT and developer tools", vocabulary=Vocabulary(roles=["sysadmin"]))
    assert anchor(it) == "sysadmin"
    a = anchor(TRUCKING)
    hn = queries(TRUCKING, "hackernews", 40)[0]
    assert hn.industry_hint == "logistics"
    assert {f'"{a}" "detention time"', f'"{a}" "re-key load confirmations"', '"dispatcher" software'} <= set(hn.keywords)
    gh = queries(TRUCKING, "github", 40)
    assert {q.params["kind"] for q in gh} == {"issues", "discussions"}
    assert all(q.keywords[0].startswith(f'"{a}" "') for q in gh)       # the anchor and the need, both required
    assert queries(TRUCKING, "lemmy", 40)[0].keywords == [f"{a} detention time", f"{a} load confirmations"]
    rest = Industry(industry="restaurants", name="Restaurants and hospitality", vocabulary=Vocabulary(roles=["chef"]))
    assert anchor(rest) == "restaurant"


async def test_posts_are_stored_under_the_industry_run_and_the_schedule_is_untouched(make_services):
    import time
    now = int(time.time())
    pages = {(f'"{k}"' if " " in k and not k.startswith('"') else k, 0): [hit(i + 1, created_at_i=now - 3600 * (i + 1), text=f"post {i} about {k}",
                                                   title=f"Ask HN {i}")]
             for i, k in enumerate(queries(TRUCKING, "hackernews", 40)[0].keywords)}
    handler, calls = algolia(pages)
    src = hn_source({"keywords": ["unrelated"]})
    src["sources"]["hackernews"]["enabled"] = True
    async with make_services(src, handler) as svc:
        counts = await targeted_search(svc, [TRUCKING], sources=("hackernews",))
        assert svc.store.count_posts() == counts["hackernews:logistics"] > 0
        assert svc.store.last_run("hackernews:logistics") is not None
        assert svc.store.last_run("hackernews") is None             # the scheduled source's clock is untouched
    from signal_connectors.targeted import anchor
    assert {c["query"] for c in calls} >= {f'"{anchor(TRUCKING)}" "detention time"', '"dispatcher" software'}
    assert all(c["query"] != "unrelated" for c in calls)       # the source's own queries don't run
