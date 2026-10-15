"""Section 17, build steps 1-2: Detector 1 (paid workarounds), the critic pass, confidence, and inferred
problems in the feed."""

import uuid
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from sqlalchemy import select

from signal_connectors.models import RawPost
from signal_connectors.store import Store
from signal_feed.db import (
    DetectorStatsRow,
    EnrichedPostRow,
    EventRow,
    InferredEvidenceRow,
    InferredProblemRow,
    NicheRow,
    PostNicheRow,
    UserNicheAffinityRow,
)
from signal_feed.enrichment.costs import Usage
from signal_feed.enrichment.llm import TagAttempt
from signal_feed.feed.candidates import EXPLORE, generate
from signal_feed.feed.data import FeedDataCache, load
from signal_feed.feed.service import FeedService
from signal_feed.feed.usermodel import apply_events, load_state, onboard
from signal_feed.inferred.common import Evidence, confidence
from signal_feed.inferred.critic import CriticVerdict, EvidenceCheck, critic_prompt
from signal_feed.inferred.paid_workarounds import derived_task, find_candidates
from signal_feed.inferred.pipeline import run_detector
from signal_feed.llm_task import LLMTask
from signal_feed.niches.tree import seed_tree

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
DIM = 12


def unit(v):
    return (np.asarray(v) / np.linalg.norm(v)).tolist()


class FakeCritic:
    """Answers each critic call with the next scripted verdict (or None: invalid output)."""

    def __init__(self, *verdicts):
        self.verdicts, self.prompts = list(verdicts), []

    def tag(self, call, system, user, *, fallbacks=False, output_model=None):
        self.prompts.append(user)
        v = self.verdicts.pop(0) if self.verdicts else None
        return TagAttempt(output=v, model=call.model, usages=[Usage(call.model, 800, 120)],
                          error=None if v is not None else "invalid_json")


def verdict(n, verdict="keep", supports=None, existing=None):
    supports = supports or [True] * n
    return CriticVerdict(checks=[EvidenceCheck(index=k + 1, supports=s, reason="r") for k, s in enumerate(supports)],
                         alternative_explanation="Seasonal backlog", existing_solution=existing, verdict=verdict,
                         reasons=["fits"])


@pytest.fixture
def market(tmp_path, feed_store):
    """Legal > Contract work: 6 paid items (3 tenders + 3 job posts, 6 buyers) about one automatable task,
    a positive review of a tool that does it, and a 6-item single-portal cluster elsewhere."""
    rng = np.random.default_rng(5)
    seed_tree(feed_store)
    with feed_store.session() as s:
        legal = s.scalars(select(NicheRow).where(NicheRow.label == "Legal", NicheRow.parent_id.is_(None))).one()
        contract = s.scalars(select(NicheRow).where(NicheRow.label == "Contract work",
                                                    NicheRow.parent_id == legal.id)).one()
        farm = s.scalars(select(NicheRow).where(NicheRow.label == "Farm equipment")).one()
    task_vec, other_vec = rng.normal(size=DIM), rng.normal(size=DIM)
    raw, rows, placed = [], [], []

    def add(k, source, source_type, community, ptype, niche, vec, status="ok", task="redline vendor contracts",
            automatable=0.9, pain="A firm pays someone to redline vendor contracts", tools=None, published=NOW):
        p = RawPost(source=source, source_type=source_type, route="own_api", url=f"https://x.example/{k}",
                    title=f"post {k}", text=f"text {k}", is_snippet=False, published_at=published - timedelta(days=k % 20),
                    community=community)
        e = unit(vec + rng.normal(scale=0.05, size=DIM))
        raw.append(p)
        rows.append(EnrichedPostRow(post_id=p.id, status=status, post_type=ptype, industry_path=["Legal"],
                                    pain_statement=pain, who_has_pain="legal operations team", intensity=0.5,
                                    buyer_likelihood=0.9, quality=0.7, toxicity=0.0, embedding=e, pain_embedding=e,
                                    task=task, task_automatable=automatable, task_embedding=e if task else None,
                                    tools_mentioned=tools, money_signal={"kind": "contract_value", "quote": "£40k"}
                                    if ptype == "paid_demand" else None, prompt_version="v9", enriched_at=NOW))
        placed.append(PostNicheRow(post_id=p.id, niche_id=niche, tree="industry", similarity=0.9))
        return p

    for k in range(3):
        add(k, "uk_tenders:contracts_finder", "government", f"Council {k}", "paid_demand", contract.id, task_vec)
    for k in range(3, 6):
        add(k, "job_boards", "paid_demand", f"Company {k}", "paid_demand", contract.id, task_vec,
            tools=["Ironclad"] if k == 3 else None)
    review = add(10, "wordpress:contracts-plugin", "review", None, "praise", contract.id, task_vec, status="dropped",
                 task=None, tools=["ContractBot"])
    # A tight cluster from one portal only (one source type): never a candidate.
    for k in range(20, 26):
        add(k, "sam_gov", "government", f"Agency {k}", "paid_demand", farm.id, other_vec, task="supply hose assemblies")
    url = f"sqlite:///{tmp_path / 'signal.db'}"
    Store(url).save_posts(raw, text_dedupe=False)
    with feed_store.session() as s, s.begin():
        s.add_all(rows)
        s.add_all(placed)
    from signal_feed.posts import PostsReader
    return PostsReader(url), contract.id, review, raw


def llm_with(feed_store, feed_config, *verdicts):
    return LLMTask(feed_store, feed_config.enrichment, FakeCritic(*verdicts), clock=lambda: NOW)


# --- detector -------------------------------------------------------------------

def test_derived_tasks_strip_the_buyer_and_lead_in():
    assert derived_task("The council needs a contractor to service and repair its HGV fleet.") == \
        "service and repair its HGV fleet"
    assert derived_task("The Defense Logistics Agency is soliciting bids to supply valves.") == "supply valves"
    assert derived_task(None) is None


def test_detector_finds_paid_workarounds_with_factual_evidence(market, feed_store, feed_config):
    reader, contract, review, _ = market
    cands, clusters = find_candidates(feed_store, reader, feed_config, NOW)
    assert clusters >= 2 and len(cands) == 1                 # the single-portal cluster doesn't qualify
    c = cands[0]
    assert c.niche_id == contract and c.task == "redline vendor contracts" and not c.derived
    assert c.hypothesis == "Legal operations teams pay people to redline vendor contracts by hand because no tool does it well."
    assert len(c.evidence) == 6 and {e.source_type for e in c.evidence} == {"government", "paid_demand"}
    assert all(e.what_it_shows.endswith("pays for: redline vendor contracts (£40k)") for e in c.evidence)
    assert any(x["post_id"] == review.id and "ContractBot" in x["what_it_shows"] for x in c.counter_evidence)
    assert any(x["what_it_shows"] == "Tools the buyers name: Ironclad" for x in c.counter_evidence)


def test_tasks_software_cant_do_are_skipped(market, feed_store, feed_config):
    reader, *_ = market
    with feed_store.session() as s, s.begin():
        for r in s.scalars(select(EnrichedPostRow).where(EnrichedPostRow.task == "redline vendor contracts")):
            r.task_automatable = 0.1
    assert find_candidates(feed_store, reader, feed_config, NOW)[0] == []


def test_confidence_levels_follow_17_6(feed_config):
    cfg = feed_config.inferred
    ev = lambda n, types: [Evidence(f"p{k}", "s", f"t{k % types}", "u", "w") for k in range(n)]  # noqa: E731
    assert confidence(ev(2, 2), cfg, "keep", False) is None                     # below the minimum
    assert confidence(ev(3, 1), cfg, "keep", False) is None
    assert confidence(ev(3, 2), cfg, None, False) == "low"                      # no critic
    assert confidence(ev(3, 2), cfg, "revise", False) == "medium"
    assert confidence(ev(8, 3), cfg, "keep", False) == "high"
    assert confidence(ev(8, 3), cfg, "keep", True) == "medium"                  # an existing tool was found
    assert confidence(ev(3, 2), cfg, None, True, insider_confirmations=2) == "high"


# --- pipeline -----------------------------------------------------------------------

def test_without_a_critic_nothing_is_published(market, feed_store, feed_config):
    reader, *_ = market
    report = run_detector(feed_store, reader, feed_config, "paid_workarounds", llm=None, clock=lambda: NOW)
    assert (report.candidates, report.held, report.published) == (1, 1, 0)
    with feed_store.session() as s:
        row = s.scalars(select(InferredProblemRow)).one()
        assert (row.status, row.confidence, row.critic_notes) == ("candidate", "low", {"critic": "not run"})
        assert s.query(InferredEvidenceRow).count() == 6 and s.query(DetectorStatsRow).one().candidates == 1


def test_critic_keeps_rejects_and_drops_evidence(market, feed_store, feed_config):
    reader, *_ = market
    critic = FakeCritic(verdict(6))
    run_detector(feed_store, reader, feed_config, "paid_workarounds", LLMTask(feed_store, feed_config.enrichment, critic,
                                                                              clock=lambda: NOW), clock=lambda: NOW)
    with feed_store.session() as s:
        row = s.scalars(select(InferredProblemRow)).one()
    assert (row.status, row.confidence) == ("published", "medium")       # 6 items, 2 types: not "high"
    assert row.critic_notes["verdict"] == "keep" and row.critic_notes["alternative_explanation"]
    # The critic sees the claim and the evidence, never the detector's numbers or reasoning.
    prompt = critic.prompts[0]
    assert "<claim>" in prompt and "pays for: redline vendor contracts" in prompt and "buyers" not in prompt.split("<possible")[0]
    # Re-run: the critic now vouches for only 2 items -> below the minimum -> rejected, same id.
    run_detector(feed_store, reader, feed_config, "paid_workarounds",
                 llm_with(feed_store, feed_config, verdict(6, supports=[True, True, False, False, False, False])),
                 clock=lambda: NOW)
    with feed_store.session() as s:
        again = s.scalars(select(InferredProblemRow)).one()
        dropped = s.query(InferredEvidenceRow).filter(InferredEvidenceRow.supported.is_(False)).count()
    assert again.id == row.id and again.status == "rejected" and dropped == 4
    assert "too few supported evidence items" in again.critic_notes["reasons"][-1]


def test_published_problems_survive_a_run_without_critic_and_expire_when_gone(market, feed_store, feed_config):
    reader, *_ = market
    run_detector(feed_store, reader, feed_config, "paid_workarounds", llm_with(feed_store, feed_config, verdict(6)),
                 clock=lambda: NOW)
    run_detector(feed_store, reader, feed_config, "paid_workarounds", llm=None, clock=lambda: NOW)
    with feed_store.session() as s:
        assert s.scalars(select(InferredProblemRow)).one().status == "published"   # no critic: keep the verdict
    later = NOW + timedelta(days=200)                                            # out of the 60-day window
    report = run_detector(feed_store, reader, feed_config, "paid_workarounds", llm=None, clock=lambda: later)
    assert report.expired == 1
    with feed_store.session() as s:
        assert s.scalars(select(InferredProblemRow)).one().status == "expired"


# --- feed -----------------------------------------------------------------------------

def publish(feed_store, feed_config, reader, conf_verdict):
    run_detector(feed_store, reader, feed_config, "paid_workarounds", llm_with(feed_store, feed_config, conf_verdict),
                 clock=lambda: NOW)
    with feed_store.session() as s:
        return s.scalars(select(InferredProblemRow)).one()


def test_inferred_problems_reach_the_feed_with_label_and_evidence(market, feed_store, feed_config):
    reader, contract, _, _ = market
    row = publish(feed_store, feed_config, reader, verdict(6))
    svc = FeedService(feed_store, feed_config, FeedDataCache(feed_store, reader), clock=lambda: NOW, seed=1)
    uid = onboard(feed_store, feed_config, ["Legal > Contract work"], [], clock=lambda: NOW)
    svc.page(uid, "cal")                                                      # calibration: posts only
    items = [i for k in range(3) for i in svc.page(uid, f"s{k}").items]
    inferred = [i for i in items if i.kind == "inferred"]
    assert len(inferred) == 1
    item = inferred[0]
    assert (item.item_type, item.item_id, item.post_id, item.confidence) == ("inferred", str(row.id), None, "medium")
    assert item.summary == row.hypothesis and len(item.evidence) == 3 and item.evidence_more == 3
    assert all(e["url"].startswith("https://x.example/") for e in item.evidence)
    assert all(i.kind == "reported" for i in items if i.item_type == "post")
    assert item.score_breakdown["features"]["inferred"] == 1.0
    # Events on the inferred item are accepted and move the niche; an unknown id is rejected.
    from signal_feed.events import EventBatch, EventIn, log_events
    stored = []
    ok = log_events(EventBatch(user_id=uid, events=[
        EventIn(client_event_id=uuid.uuid4().hex, session_id="s0", type="save", item_type="inferred",
                item_id=str(row.id), at=NOW),
        EventIn(client_event_id=uuid.uuid4().hex, session_id="s0", type="save", item_type="inferred",
                item_id="99999", at=NOW)]), feed_store, reader, feed_config, now=NOW, on_stored=stored.extend)
    assert ok.accepted == 1 and ok.rejected[0].error == "unknown inferred problem"
    before = load_state(feed_store, feed_config, uid, NOW).niche.get(contract)
    apply_events(feed_store, reader, feed_config, stored, clock=lambda: NOW)
    assert load_state(feed_store, feed_config, uid, NOW).niche[contract] > before


def test_low_confidence_inferred_problems_only_enter_through_exploration(market, feed_store, feed_config):
    reader, *_ = market
    cfg = feed_config.model_copy(update={"inferred": feed_config.inferred.model_copy(update={"publish_without_critic": True})})
    run_detector(feed_store, reader, cfg, "paid_workarounds", llm=None, clock=lambda: NOW)    # published, "low"
    data = load(feed_store, reader)
    key = next(it.post_id for it in data.items if it.item_type == "inferred")
    assert data.by_id[key].confidence == "low"
    uid = onboard(feed_store, cfg, ["Legal > Contract work"], [], clock=lambda: NOW)
    st = load_state(feed_store, cfg, uid, NOW)
    sources = [c.source for seed in range(5) for c in generate(data, st, cfg, feed_store, NOW, np.random.default_rng(seed))
               if data.items[c.index].post_id == key]
    # The user's own niche holds it, but it may only come in through exploration (never via niche match).
    assert sources and set(sources) == {EXPLORE}
