from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from signal_connectors.models import RawPost
from signal_connectors.store import Store
from signal_feed.db import EnrichedPostRow, EnrichmentBatchRow, FeedCostRow
from signal_feed.enrichment.batch import BatchRunner
from signal_feed.enrichment.costs import BudgetExceededError, CostLedger, Usage, claude_usd
from signal_feed.enrichment.embeddings import VoyageEmbedder
from signal_feed.enrichment.llm import TagAttempt, TransientError, read_message
from signal_feed.enrichment.pii import scrub
from signal_feed.enrichment.pipeline import Enricher
from signal_feed.enrichment.prompt import user_prompt
from signal_feed.enrichment.schema import tag_output_model
from signal_feed.posts import PostsReader

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
PAIN = "Every Friday I copy 200 invoices from email into QuickBooks by hand. Takes me four hours. Is there a tool?"


def tags(**overrides):
    base = dict(post_type="pain", industry="Accounting and finance", industry_subpath=["Bookkeeping"],
                skills_needed=["Document parsing"], who_has_pain="bookkeeper",
                pain_statement="Copying invoices from email into QuickBooks takes hours every week.",
                current_workaround="manual entry", tools_mentioned=["QuickBooks"], intensity=0.8, money_signal=None, buyer_likelihood=0.7,
                quality=0.8, spam_score=0.0, toxicity=0.0, language="en", confidence=0.9)
    return tag_output_model()(**(base | overrides))


def ok(model="claude-haiku-5-5", **overrides):
    return TagAttempt(output=tags(**overrides), model=model, usages=[Usage(model, 2000, 300)])


def failed(error, model="claude-haiku-5-5"):
    return TagAttempt(output=None, model=model, usages=[Usage(model, 2000, 50)], error=error)


class FakeTagger:
    """Returns queued attempts per model, in order; records every call."""

    def __init__(self, **queues):
        self.queues = {m.replace("_", "-"): list(q) for m, q in queues.items()}
        self.calls = []

    def tag(self, call, system, user, *, fallbacks=False, output_model=None):
        self.calls.append((call.model, fallbacks))
        item = self.queues[call.model].pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def post(url, text=PAIN, source="hn", **kw):
    return RawPost(source=source, source_type="community", route="own_api", url=url, text=text, is_snippet=False,
                   published_at=datetime(2026, 10, 1, tzinfo=timezone.utc), **kw)


@pytest.fixture
def three_posts(tmp_path):
    url = f"sqlite:///{tmp_path / 'signal.db'}"
    posts = [post("https://a.example/1"), post("https://a.example/2", text="Nice weather today, everyone! Enjoy the weekend."),
             post("https://data.example/3", source="cfpb", text="Product: Credit reporting. Issue: x")]
    Store(url).save_posts(posts, text_dedupe=False)
    return PostsReader(url), posts


def make(feed_store, feed_config, reader, tagger, **kw):
    return Enricher(feed_store, reader, feed_config, tagger, clock=lambda: NOW, **kw)


def rows(feed_store):
    with feed_store.session() as s:
        return {r.post_id: r for r in s.query(EnrichedPostRow)}


def test_tags_posts_prefilters_and_drops_non_actionable_types(feed_store, feed_config, three_posts):
    reader, (pain, chat, cfpb) = three_posts
    tagger = FakeTagger(claude_haiku_5_5=[ok(), ok(post_type="off_topic", pain_statement=None)])
    stats = make(feed_store, feed_config, reader, tagger).enrich([pain.id, chat.id, cfpb.id])
    assert (stats.ok, stats.dropped, stats.skipped) == (1, 1, 1)
    got = rows(feed_store)
    assert got[pain.id].industry_path == ["Accounting and finance", "Bookkeeping"]
    assert got[pain.id].prompt_version == feed_config.enrichment.prompt_version
    assert (got[chat.id].status, got[chat.id].status_reason) == ("dropped", "off_topic")
    assert (got[cfpb.id].status, got[cfpb.id].status_reason) == ("skipped", "aggregate_source")
    assert len(tagger.calls) == 2   # the cfpb record never reached the model


def test_spam_is_dropped(feed_store, feed_config, three_posts):
    reader, (pain, *_) = three_posts
    make(feed_store, feed_config, reader, FakeTagger(claude_haiku_5_5=[ok(spam_score=0.9)])).enrich([pain.id])
    assert (rows(feed_store)[pain.id].status, rows(feed_store)[pain.id].status_reason) == ("dropped", "spam")


def test_invalid_json_retries_once_then_needs_review(feed_store, feed_config, three_posts):
    reader, (pain, *_) = three_posts
    tagger = FakeTagger(claude_haiku_5_5=[failed("invalid_json"), failed("invalid_json")])
    stats = make(feed_store, feed_config, reader, tagger).enrich([pain.id])
    assert stats.needs_review == 1 and len(tagger.calls) == 2
    assert rows(feed_store)[pain.id].status_reason == "invalid_json"


def test_retry_can_succeed(feed_store, feed_config, three_posts):
    reader, (pain, *_) = three_posts
    tagger = FakeTagger(claude_haiku_5_5=[failed("max_tokens"), ok()])
    assert make(feed_store, feed_config, reader, tagger).enrich([pain.id]).ok == 1


def test_low_confidence_escalates_to_large_model_with_fallbacks(feed_store, feed_config, three_posts):
    reader, (pain, *_) = three_posts
    tagger = FakeTagger(claude_haiku_5_5=[ok(confidence=0.3)],
                        claude_opus_5_5=[ok(model="claude-opus-5-5", industry="Legal", confidence=0.8)])
    stats = make(feed_store, feed_config, reader, tagger).enrich([pain.id])
    row = rows(feed_store)[pain.id]
    assert stats.escalated == 1 and row.escalated and row.model == "claude-opus-5-5"
    assert row.industry_path[0] == "Legal"
    assert tagger.calls == [("claude-haiku-5-5", False), ("claude-opus-5-5", True)]


def test_refusal_escalates_and_failed_escalation_keeps_needs_review(feed_store, feed_config, three_posts):
    reader, (pain, *_) = three_posts
    tagger = FakeTagger(claude_haiku_5_5=[failed("refusal")],
                        claude_opus_5_5=[failed("refusal", model="claude-opus-5-5")])
    make(feed_store, feed_config, reader, tagger).enrich([pain.id])
    row = rows(feed_store)[pain.id]
    assert (row.status, row.status_reason) == ("needs_review", "refusal")


def test_failed_escalation_keeps_the_small_models_tags(feed_store, feed_config, three_posts):
    reader, (pain, *_) = three_posts
    tagger = FakeTagger(claude_haiku_5_5=[ok(confidence=0.2)],
                        claude_opus_5_5=[failed("bad_request", model="claude-opus-5-5")])
    make(feed_store, feed_config, reader, tagger).enrich([pain.id])
    row = rows(feed_store)[pain.id]
    assert (row.status, row.status_reason, row.model) == ("ok", "escalation_bad_request", "claude-haiku-5-5")


def test_costs_are_metered_per_call(feed_store, feed_config, three_posts):
    reader, (pain, *_) = three_posts
    tagger = FakeTagger(claude_haiku_5_5=[ok(confidence=0.3)], claude_opus_5_5=[ok(model="claude-opus-5-5")])
    make(feed_store, feed_config, reader, tagger).enrich([pain.id])
    with feed_store.session() as s:
        costs = [(c.purpose, c.model, round(c.usd, 6)) for c in s.query(FeedCostRow).order_by(FeedCostRow.id)]
    # haiku: 2000 * 0.10 + 300 * 0.50 per M; opus: 2000 * 4 + 300 * 20 per M
    assert costs == [("tag", "claude-haiku-5-5", 0.00035), ("escalate", "claude-opus-5-5", 0.014)]
    assert rows(feed_store)[pain.id].cost_usd == pytest.approx(0.01435)


def test_budget_guard_stops_the_run_before_calling(feed_store, feed_config, three_posts):
    reader, (pain, *_) = three_posts
    cfg = feed_config.model_copy(update={"enrichment": feed_config.enrichment.model_copy(
        update={"monthly_budget_usd": 0.001})})
    tagger = FakeTagger(claude_haiku_5_5=[ok()])
    stats = make(feed_store, cfg, reader, tagger).enrich([pain.id])
    assert stats.stopped and "budget" in stats.stopped
    assert tagger.calls == [] and rows(feed_store) == {}


def test_transient_errors_leave_the_post_pending(feed_store, feed_config, three_posts):
    reader, (pain, *_) = three_posts
    enricher = make(feed_store, feed_config, reader, FakeTagger(claude_haiku_5_5=[TransientError("overloaded")]))
    assert enricher.enrich([pain.id]).transient == 1
    assert pain.id in enricher.pending_post_ids()


def test_pending_skips_current_rows_and_redoes_old_prompt_versions(feed_store, feed_config, three_posts):
    reader, (pain, chat, cfpb) = three_posts
    enricher = make(feed_store, feed_config, reader, FakeTagger(claude_haiku_5_5=[ok()]))
    enricher.enrich([pain.id])
    assert set(enricher.pending_post_ids()) == {chat.id, cfpb.id}
    with feed_store.session() as s, s.begin():
        s.get(EnrichedPostRow, pain.id).prompt_version = "v0"
    assert pain.id in enricher.pending_post_ids()


def test_purge_removes_enrichment_of_deleted_posts(feed_store, feed_config, three_posts, tmp_path):
    reader, (pain, chat, _) = three_posts
    enricher = make(feed_store, feed_config, reader, FakeTagger(claude_haiku_5_5=[ok(), ok()]))
    enricher.enrich([pain.id, chat.id])
    Store(f"sqlite:///{tmp_path / 'signal.db'}").delete_posts([chat.id])
    assert enricher.purge_deleted() == 1
    assert set(rows(feed_store)) == {pain.id}


def test_model_written_fields_are_scrubbed(feed_store, feed_config, three_posts):
    reader, (pain, *_) = three_posts
    tagger = FakeTagger(claude_haiku_5_5=[ok(pain_statement="Email jane.doe@firm.com or call +1 (415) 555-0134.",
                                             who_has_pain="@janedoe")])
    make(feed_store, feed_config, reader, tagger).enrich([pain.id])
    row = rows(feed_store)[pain.id]
    assert row.pain_statement == "Email [email] or call [phone]." and row.who_has_pain == "[user]"


@pytest.mark.parametrize("text, expected", [
    ("Budget $12,000 to $15,000 per month", "Budget $12,000 to $15,000 per month"),
    ("Contract 2024-2026, ref MMT-342", "Contract 2024-2026, ref MMT-342"),
    ("see https://x.com/someone/status/1", "see [link]"),
])
def test_scrub_keeps_money_and_dates(text, expected):
    assert scrub(text) == expected


def test_long_posts_are_cut_with_a_visible_marker():
    p = post("https://a.example/long", text="word " * 3000)
    prompt = user_prompt(p, 100)
    assert "[cut: the rest of the post was not shown]" in prompt and prompt.count("word") == 20


def test_read_message_validates_and_reports_errors():
    def message(text, stop="end_turn"):
        return SimpleNamespace(model="claude-haiku-5-5", stop_reason=stop, content=[SimpleNamespace(type="text", text=text)],
                               usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0,
                                                     cache_creation_input_tokens=0))
    assert read_message(message(tags().model_dump_json())).output.industry == "Accounting and finance"
    assert read_message(message('{"post_type": "pain"}')).error == "invalid_json"
    assert read_message(message(tags(intensity=0.5).model_dump_json().replace("0.5", "1.5"))).error == "invalid_json"
    assert read_message(message("", stop="refusal")).error == "refusal"
    assert read_message(message("{", stop="max_tokens")).error == "max_tokens"


def test_fallback_usage_is_priced_per_model(feed_config):
    cfg = feed_config.enrichment
    assert claude_usd(Usage("claude-opus-5-5", 1_000_000, 0), cfg) == pytest.approx(4.0)
    assert claude_usd(Usage("claude-haiku-5-5", 1_000_000, 1_000_000), cfg, batch=True) == pytest.approx(0.30)
    assert claude_usd(Usage("unknown-model", 1_000_000, 0), cfg) == pytest.approx(5.0)   # dearest known


# --- batches -----------------------------------------------------------------

class FakeBatches:
    def __init__(self):
        self.created, self.status, self.outputs = [], "in_progress", {}

    def create(self, requests):
        self.created.append(requests)
        return SimpleNamespace(id=f"msgbatch_{len(self.created)}")

    def retrieve(self, batch_id):
        return SimpleNamespace(id=batch_id, processing_status=self.status)

    def results(self, batch_id):
        for custom_id, out in self.outputs.items():
            if out is None:
                yield SimpleNamespace(custom_id=custom_id, result=SimpleNamespace(type="expired"))
                continue
            msg = SimpleNamespace(model="claude-haiku-5-5", stop_reason="end_turn",
                                  content=[SimpleNamespace(type="text", text=out.model_dump_json())],
                                  usage=SimpleNamespace(input_tokens=2000, output_tokens=300,
                                                        cache_read_input_tokens=0, cache_creation_input_tokens=0))
            yield SimpleNamespace(custom_id=custom_id, result=SimpleNamespace(type="succeeded", message=msg))


def test_batch_submit_reserves_budget_and_collect_finishes(feed_store, feed_config, three_posts):
    reader, (pain, chat, cfpb) = three_posts
    fake = FakeBatches()
    tagger = FakeTagger(claude_opus_5_5=[ok(model="claude-opus-5-5")])
    enricher = make(feed_store, feed_config, reader, tagger)
    runner = BatchRunner(enricher, client=SimpleNamespace(messages=SimpleNamespace(batches=fake)))

    batch_ids, stats = runner.submit([pain.id, chat.id, cfpb.id])
    assert batch_ids == ["msgbatch_1"] and stats.skipped == 1
    assert [r["custom_id"] for r in fake.created[0]] == [pain.id, chat.id]
    assert fake.created[0][0]["params"]["output_config"]["format"]["type"] == "json_schema"
    reserved = CostLedger(feed_store).month_spend("anthropic", NOW)
    assert reserved > 0
    assert enricher.pending_post_ids() == []   # queued posts aren't resubmitted

    assert runner.collect().ok == 0            # still running: nothing collected
    fake.status = "ended"
    fake.outputs = {pain.id: tags(confidence=0.2), chat.id: None}
    stats = runner.collect()
    assert (stats.ok, stats.escalated) == (1, 1)
    assert tagger.calls == [("claude-opus-5-5", True)]   # escalation runs sync
    with feed_store.session() as s:
        assert s.get(EnrichmentBatchRow, "msgbatch_1").status == "collected"
        batch_costs = s.query(FeedCostRow).filter(FeedCostRow.batch.is_(True)).all()
        actual = sum(c.usd for c in s.query(FeedCostRow))
    assert [round(c.usd, 6) for c in batch_costs] == [0.000175]   # half price
    assert CostLedger(feed_store).month_spend("anthropic", NOW) == pytest.approx(actual)   # reservation released
    assert enricher.pending_post_ids() == [chat.id]   # the expired request is retried later


def test_batch_submit_respects_budget(feed_store, feed_config, three_posts):
    reader, (pain, *_) = three_posts
    CostLedger(feed_store).record(provider="anthropic", purpose="tag", usage=Usage("claude-haiku-5-5"),
                                  usd=feed_config.enrichment.monthly_budget_usd, at=NOW)
    fake = FakeBatches()
    runner = BatchRunner(make(feed_store, feed_config, reader, FakeTagger()),
                         client=SimpleNamespace(messages=SimpleNamespace(batches=fake)))
    _, stats = runner.submit([pain.id])
    assert stats.stopped and fake.created == []


def test_budget_check_counts_this_month_only(feed_store, feed_config):
    ledger = CostLedger(feed_store)
    ledger.record(provider="anthropic", purpose="tag", usage=Usage("m"), usd=49.0,
                  at=datetime(2026, 9, 30, 23, 0, tzinfo=timezone.utc))
    ledger.check("anthropic", 10.0, 50.0, NOW)
    ledger.record(provider="anthropic", purpose="tag", usage=Usage("m"), usd=45.0, at=NOW)
    with pytest.raises(BudgetExceededError):
        ledger.check("anthropic", 10.0, 50.0, NOW)


# --- embeddings --------------------------------------------------------------

def test_embeddings_through_voyage(feed_store, feed_config, three_posts):
    reader, (pain, chat, _) = three_posts
    seen = []

    def voyage(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        seen.append(body)
        return httpx.Response(200, json={"object": "list", "model": body["model"], "usage": {"total_tokens": 1000},
                                         "data": [{"index": i, "embedding": [0.1, 0.2]} for i in range(len(body["input"]))]})

    embedder = VoyageEmbedder(feed_config.embeddings, api_key="test", transport=httpx.MockTransport(voyage))
    enricher = make(feed_store, feed_config, reader,
                    FakeTagger(claude_haiku_5_5=[ok(), ok(post_type="opinion")]), embedder=embedder)
    enricher.enrich([pain.id, chat.id])
    assert enricher.embed_pending() == 1      # the dropped post is not embedded
    assert seen[0]["model"] == "voyage-4" and seen[0]["input_type"] == "document"
    assert seen[0]["input"][0].startswith("Copying invoices")
    assert seen[1]["input"] == ["Copying invoices from email into QuickBooks takes hours every week."]
    row = rows(feed_store)[pain.id]
    assert row.embedding == [0.1, 0.2] and row.pain_embedding == [0.1, 0.2] and row.embedding_model == "voyage-4"
    assert row.tools_mentioned == ["QuickBooks"]
    with feed_store.session() as s:
        costs = s.query(FeedCostRow).filter(FeedCostRow.provider == "voyage").all()
    assert len(costs) == 2 and costs[0].usd == pytest.approx(1000 * 0.06 / 1_000_000)
    assert enricher.embed_pending() == 0


def test_voyage_retries_rate_limits(feed_config):
    calls = []

    def voyage(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429)
        return httpx.Response(200, json={"model": "voyage-4", "usage": {"total_tokens": 3},
                                         "data": [{"index": 0, "embedding": [1.0]}]})

    embedder = VoyageEmbedder(feed_config.embeddings, api_key="k", transport=httpx.MockTransport(voyage))
    embedder._post.retry.wait = lambda *_: 0
    assert embedder.embed(["x"]).vectors == [[1.0]] and len(calls) == 2


def test_voyage_paces_under_free_tier_limits(feed_config):
    cfg = feed_config.embeddings.model_copy(update={"requests_per_minute": 3, "tokens_per_minute": 1000})
    sizes, now, slept = [], [0.0], []

    def voyage(request):
        n = len(__import__("json").loads(request.content)["input"])
        sizes.append(n)
        return httpx.Response(200, json={"model": "voyage-4", "usage": {"total_tokens": 10},
                                         "data": [{"index": i, "embedding": [float(i)]} for i in range(n)]})

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    embedder = VoyageEmbedder(cfg, api_key="k", transport=httpx.MockTransport(voyage),
                              clock=lambda: now[0], sleep=sleep)
    result = embedder.embed(["x" * 1400] * 8)   # ~467 estimated tokens each: two per request
    assert sizes == [2, 2, 2, 2] and len(result.vectors) == 8 and result.total_tokens == 40
    assert len(slept) == 3 and now[0] >= 120     # each request waits for the token window to clear


def test_no_credit_stops_the_run_instead_of_marking_posts(feed_config):
    import anthropic
    import httpx2

    from signal_feed.enrichment.costs import BillingError
    from signal_feed.enrichment.llm import AnthropicTagger

    response = httpx2.Response(400, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))

    class Messages:
        def create(self, **_):
            raise anthropic.BadRequestError("Your credit balance is too low to access the Anthropic API.",
                                            response=response, body=None)

    client = type("Client", (), {"messages": Messages()})()
    with pytest.raises(BillingError):
        AnthropicTagger(client).tag(feed_config.enrichment.tagger, "s", "u")
