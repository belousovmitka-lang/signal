"""OPPORTUNITIES.md 18.3: the opportunity-fields pass."""

import threading

from signal_feed.db import EnrichedPostRow, FeedCostRow
from signal_feed.enrichment.costs import Usage
from signal_feed.enrichment.llm import TagAttempt
from signal_feed.enrichment.opportunity_fields import (OpportunityFields, OpportunityFieldsPass, export_review,
                                                       sample_ids)

from .test_enrichment import NOW, FakeTagger, make, ok, rows, three_posts  # noqa: F401  (fixture)


def fields(**over):
    base = dict(job_statement="bookkeeper needs invoice entry into QuickBooks", deliverable=None,
                buyer_type="small_business", buyer_segment="small bookkeeping firms", recurrence="recurring",
                one_off_request=False, fixable_by_third_party="yes", platform_owner=None,
                outside_route="QuickBooks API", time_cost={"hours": 4, "per": "week", "quote": "four hours, call 555-123-4567"},
                plain_summary="Bookkeepers copy invoices from email into their accounting app by hand every week.",
                confidence=0.8)
    return OpportunityFields(**(base | over))


class Tagger:
    def __init__(self, *answers):
        self.answers, self.calls, self.lock = list(answers), [], threading.Lock()

    def tag(self, call, system, user, *, fallbacks=False, output_model=None):
        with self.lock:
            self.calls.append((call.model, output_model, user))
            out = self.answers.pop(0)
        return out if isinstance(out, TagAttempt) else TagAttempt(output=out, model=call.model,
                                                                   usages=[Usage(call.model, 1500, 200)])


def tagged(feed_store, feed_config, reader, *posts_):
    make(feed_store, feed_config, reader, FakeTagger(claude_haiku_5_5=[ok() for _ in posts_])).enrich([p.id for p in posts_])


def test_fields_are_written_beside_the_tags_without_touching_them(feed_store, feed_config, three_posts, tmp_path):  # noqa: F811
    reader, (pain, *_) = three_posts
    tagged(feed_store, feed_config, reader, pain)
    before = rows(feed_store)[pain.id]
    run = OpportunityFieldsPass(feed_store, reader, feed_config, Tagger(fields()), clock=lambda: NOW, workers=2)
    assert run.pending_ids() == [pain.id]
    [result] = run.run([pain.id])
    after = rows(feed_store)[pain.id]
    assert result.output is not None and run.pending_ids() == []
    assert (after.buyer_type, after.recurrence, after.fixable_by_third_party, after.outside_route) == \
        ("small_business", "recurring", "yes", "QuickBooks API")
    assert after.time_cost["hours"] == 4 and "555" not in after.time_cost["quote"]            # contacts scrubbed
    assert after.opp_version == feed_config.enrichment.opportunity_fields.version
    assert (after.pain_statement, after.enriched_at, after.prompt_version) == \
        (before.pain_statement, before.enriched_at, before.prompt_version)          # main tags and the cache key stay
    _, model, user = run.tagger.calls[0]
    assert model is OpportunityFields and "<earlier_tags>" in user and "Copying invoices" in user
    with feed_store.session() as s:
        assert [r.purpose for r in s.query(FeedCostRow)].count("opportunity_fields") == 1
    csv_path, html_path = export_review(feed_store, reader, [pain.id], tmp_path / "review" / "o1")
    assert "small bookkeeping firms" in csv_path.read_text() and "your_verdict" in csv_path.read_text()
    assert "<script>" in html_path.read_text() and "small bookkeeping firms" in html_path.read_text()


def test_a_failed_answer_is_recorded_and_not_asked_again_until_the_version_changes(feed_store, feed_config, three_posts):  # noqa: F811
    reader, (pain, *_) = three_posts
    tagged(feed_store, feed_config, reader, pain)
    bad = TagAttempt(output=None, model="claude-haiku-5-5", usages=[Usage("claude-haiku-5-5", 1500, 50)], error="refusal")
    run = OpportunityFieldsPass(feed_store, reader, feed_config, Tagger(bad), clock=lambda: NOW)
    [result] = run.run([pain.id])
    assert result.output is None and result.error == "refusal" and run.pending_ids() == []
    assert rows(feed_store)[pain.id].buyer_type is None
    run.ocfg = run.ocfg.model_copy(update={"version": run.ocfg.version + "-next"})
    assert run.pending_ids() == [pain.id]


def test_the_sample_mixes_types_and_keeps_out_x(feed_store, feed_config, tmp_path):
    from datetime import datetime, timezone

    from signal_connectors.models import RawPost
    from signal_connectors.store import Store
    from signal_feed.posts import PostsReader
    url = f"sqlite:///{tmp_path / 'sig.db'}"
    posts = [RawPost(source=src, source_type="community", route="own_api", url=f"https://e.example/{i}",
                     text=f"post number {i} about invoices", is_snippet=False,
                     published_at=datetime(2026, 10, 1, tzinfo=timezone.utc)) for i, src in
             enumerate(["hn", "x", "stackexchange", "github", "hn", "rss:a", "rss:b", "hn"] * 3)]
    Store(url).save_posts(posts, text_dedupe=False)
    reader = PostsReader(url)
    types = ["pain", "paid_demand", "tool_gap", "change", "idea", "spam"]
    with feed_store.session() as s, s.begin():
        for i, p in enumerate(posts):
            s.add(EnrichedPostRow(post_id=p.id, status="ok", post_type=types[i % 6], prompt_version="v9",
                                  enriched_at=NOW))
    got = sample_ids(feed_store, reader, 10)
    src = {p.id: p.source for p in posts}
    assert got and all(src[g] != "x" for g in got) and len(set(got)) == len(got) <= 10
    assert sample_ids(feed_store, reader, 10) == got                      # same seed, same sample
