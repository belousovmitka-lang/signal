from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from sqlalchemy import select

from signal_connectors.models import RawPost
from signal_connectors.store import Store
from signal_feed.db import (
    EnrichedPostRow,
    FeedCostRow,
    NicheRow,
    NicheSupportRow,
    NicheVersionRow,
    PostNicheRow,
    ProblemPostRow,
    ProblemRow,
)
from signal_feed.enrichment.costs import Usage
from signal_feed.enrichment.llm import TagAttempt
from signal_feed.llm_task import LLMTask
from signal_feed.niches.growth import NicheGrower, week_of
from signal_feed.niches.naming import NicheName, NicheNamer, fallback_name
from signal_feed.enrichment.schema import niche_seeds
from signal_feed.niches.tree import INDUSTRY, SKILL, Tree, place_posts, placements, seed_labels, seed_tree
from signal_feed.posts import PostsReader
from signal_feed.problems.cluster import ProblemBuilder, SameProblem
from signal_feed.problems.score import Evidence, components, score

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
MAINTAINER = "ab" * 32          # an author hash (64 hex chars), as privacy.hash_author makes
DIM = 16
rng = np.random.default_rng(0)
TOPICS = {name: v / np.linalg.norm(v) for name, v in
          (("citations", rng.normal(size=DIM)), ("billing", rng.normal(size=DIM)), ("intake", rng.normal(size=DIM)))}


@pytest.fixture(autouse=True)
def _fresh_rng():
    """Each test draws the same vectors, whatever ran before it."""
    global rng
    rng = np.random.default_rng(1)


def vec(topic, spread=0.15):
    v = TOPICS[topic] + rng.normal(scale=spread, size=DIM)
    return (v / np.linalg.norm(v)).tolist()


class FakeTagger:
    def __init__(self, outputs):
        self.outputs, self.calls = list(outputs), []

    def tag(self, call, system, user, *, fallbacks=False, output_model=None):
        self.calls.append((output_model, user))
        out = self.outputs.pop(0) if self.outputs else None
        return TagAttempt(output=out, model=call.model, usages=[Usage(call.model, 500, 20)],
                          error=None if out is not None else "invalid_json")


def tweak(cfg, section, **values):
    return cfg.model_copy(update={section: getattr(cfg, section).model_copy(update=values)})


@pytest.fixture
def config(feed_config):
    return tweak(feed_config, "niches", split_min_support_90d=100, child_min_posts=30, child_min_authors=15,
                 pca_dims=8)


def build_corpus(tmp_path, feed_store, groups, spread=0.15):
    """groups: [(topic, n, industry_path, extra row fields)] -> PostsReader over matching raw posts."""
    raw, rows = [], []
    for g, (topic, n, path, extra) in enumerate(groups):
        extra = dict(extra)
        author = extra.pop("_author", None)          # raw-post field, not an enrichment one
        for i in range(n):
            p = RawPost(source=f"src{i % 2}", source_type="community" if i % 3 else "news", route="rss",
                        url=f"https://x.example/{g}/{i}", text=f"post {g} {i} about {topic}", is_snippet=False,
                        published_at=NOW - timedelta(days=i % 50), author_hash=author)
            raw.append(p)
            e = vec(topic, spread)
            rows.append(EnrichedPostRow(**({
                "post_id": p.id, "status": "ok", "post_type": "pain", "industry_path": path,
                "skills_needed": ["AI agents"], "pain_statement": f"{topic} problem {i}", "intensity": 0.6,
                "buyer_likelihood": 0.8, "embedding": e, "pain_embedding": e, "prompt_version": "v7",
                "enriched_at": NOW} | extra)))
    url = f"sqlite:///{tmp_path / 'signal.db'}"
    Store(url).save_posts(raw, text_dedupe=False)
    with feed_store.session() as s, s.begin():
        s.add_all(rows)
    return PostsReader(url)


LEGAL = [("citations", 60, ["Legal", "Citation checking"], {}), ("billing", 60, ["Legal", "Legal billing"], {})]


def nodes(feed_store, **where):
    with feed_store.session() as s:
        q = select(NicheRow)
        for k, v in where.items():
            q = q.where(getattr(NicheRow, k) == v)
        return list(s.scalars(q))


def test_seed_tree_is_idempotent(feed_store):
    labels = seed_labels()
    from signal_feed.enrichment.schema import industry_names
    assert len(labels[INDUSTRY]) == len(industry_names()) + 1 and labels[INDUSTRY][-1] == "Other"
    assert "Other skills" in labels[SKILL]
    total = len(labels[INDUSTRY]) + len(labels[SKILL]) + sum(len(b) for b in niche_seeds().values())
    assert seed_tree(feed_store) == total
    assert seed_tree(feed_store) == 0
    with feed_store.session() as s:
        assert s.query(NicheVersionRow).filter(NicheVersionRow.change == "created").count() == total


def test_growth_splits_a_busy_niche_into_named_children(tmp_path, feed_store, config):
    reader = build_corpus(tmp_path, feed_store, LEGAL)
    seed_tree(feed_store)
    stats = NicheGrower(feed_store, reader, config, NicheNamer(None), clock=lambda: NOW).run()
    assert sorted(c for c in stats.created if c.startswith("Legal")) == ["Legal > Citation checking",
                                                                          "Legal > Legal billing"]
    # Every post is tagged "AI agents", but the skill tree doesn't grow (grow_trees: [industry]).
    assert not any(c.startswith("AI agents > ") for c in stats.created)
    children = {n.label: n for n in nodes(feed_store, level=2, tree=INDUSTRY, seeded=False)}
    assert all(n.assign_min_cosine is not None and n.tree == INDUSTRY for n in children.values())
    with feed_store.session() as s:
        placed = dict(s.execute(select(PostNicheRow.post_id, PostNicheRow.niche_id)
                                .where(PostNicheRow.tree == INDUSTRY)).all())
        rows = {r.post_id: r for r in s.scalars(select(EnrichedPostRow))}
        legal = next(n for n in nodes(feed_store, label="Legal"))
        assert legal.version == 3 and s.query(NicheSupportRow).filter_by(niche_id=legal.id).one().support == 120
    hits = sum(placed[pid] == children[r.industry_path[1]].id for pid, r in rows.items())
    assert hits >= 100                   # most posts descend into their own child
    # A second run finds no new clusters: the posts already sit in the children.
    again = NicheGrower(feed_store, reader, config, NicheNamer(None), clock=lambda: NOW).run()
    assert again.created == [] and len(nodes(feed_store, level=2, tree=INDUSTRY, seeded=False)) == 2


def test_split_needs_enough_voices_and_sources(tmp_path, feed_store, config):
    config = tweak(config, "niches", child_min_sources=3)
    reader = build_corpus(tmp_path, feed_store, LEGAL)
    seed_tree(feed_store)
    assert NicheGrower(feed_store, reader, config, NicheNamer(None), clock=lambda: NOW).run().created == []


def test_close_siblings_merge_and_quiet_niches_retire(tmp_path, feed_store, config):
    reader = build_corpus(tmp_path, feed_store, LEGAL)
    seed_tree(feed_store)
    legal = nodes(feed_store, label="Legal")[0]
    c = TOPICS["intake"].tolist()
    with feed_store.session() as s, s.begin():
        big = NicheRow(tree=INDUSTRY, label="Client intake", parent_id=legal.id, level=2, centroid_embedding=c,
                       support=50, created_at=NOW, assign_min_cosine=0.99)
        small = NicheRow(tree=INDUSTRY, label="New client forms", parent_id=legal.id, level=2, centroid_embedding=c,
                         support=10, created_at=NOW, assign_min_cosine=0.99)
        s.add_all([big, small])
        s.flush()
        for w in range(8):
            s.add(NicheSupportRow(niche_id=small.id, week=week_of(NOW) - timedelta(weeks=w + 1), support=3))
    stats = NicheGrower(feed_store, reader, config, NicheNamer(None), clock=lambda: NOW).run(split=False)
    assert stats.retired == ["Legal > New client forms"] and stats.merged == []
    with feed_store.session() as s:
        assert s.get(NicheRow, small.id).status == "retired"
    # Merge: revive the retired one as a sibling with the same centroid.
    with feed_store.session() as s, s.begin():
        s.get(NicheRow, small.id).status = "active"
        s.query(NicheSupportRow).filter_by(niche_id=small.id).delete()
    stats = NicheGrower(feed_store, reader, config, NicheNamer(None), clock=lambda: NOW).run(split=False)
    assert stats.merged == ["Legal > New client forms -> Client intake"]
    with feed_store.session() as s:
        assert s.get(NicheRow, small.id).merged_into_id == big.id


def test_descend_stops_below_a_childs_floor(feed_store):
    seed_tree(feed_store)
    legal = nodes(feed_store, label="Legal")[0]
    with feed_store.session() as s, s.begin():
        s.add(NicheRow(tree=INDUSTRY, label="Citation checking", parent_id=legal.id, level=2,
                       centroid_embedding=TOPICS["citations"].tolist(), created_at=NOW, assign_min_cosine=0.8))
    t = Tree.load(feed_store, INDUSTRY)
    near, far = np.array(vec("citations", 0.05)), np.array(vec("billing", 0.05))
    assert t.nodes[t.descend(legal.id, near)[0]].label == "Citation checking"
    assert t.descend(legal.id, far)[0] == legal.id


def test_namer_retries_bad_names_then_falls_back(feed_store, feed_config):
    tagger = FakeTagger([NicheName(label="Legal"), NicheName(label="citation verification.")])
    llm = LLMTask(feed_store, feed_config.enrichment, tagger, clock=lambda: NOW)
    assert NicheNamer(llm).name(["Legal"], ["Billing"], ["x"], []) == "Citation verification"
    assert "<existing_children>Billing</existing_children>" in tagger.calls[0][1]
    with feed_store.session() as s:
        assert s.query(FeedCostRow).filter(FeedCostRow.purpose == "name").count() == 2
    llm = LLMTask(feed_store, feed_config.enrichment, FakeTagger([]), clock=lambda: NOW)
    assert NicheNamer(llm).name(["Legal"], [], ["x"], ["Court filing", "Court filing", "Billing"]) == "Court filing"
    assert fallback_name([], ["Cluster 1"], "Legal") == "Cluster 2"
    # A long parent name used to loop forever here ("<parent> group N" is never 2-4 words).
    assert fallback_name(["IT and developer tools"], [], "IT and developer tools") == "Cluster 1"
    # One-word roles aren't valid names; the next most common role is used.
    assert fallback_name([], [], "Legal", roles=["paralegal", "litigation paralegal"]) == "Litigation paralegal"


def test_problems_cluster_merge_borderline_and_keep_ids(tmp_path, feed_store, config):
    reader = build_corpus(tmp_path, feed_store, [
        ("citations", 8, ["Legal"], {"money_signal": {"kind": "budget"}}),
        ("billing", 6, ["Legal"], {"post_type": "tool_gap", "tools_mentioned": ["Clio"]}),
    ], spread=0.03)
    seed_tree(feed_store)
    with feed_store.session() as s:
        place_posts(feed_store, list(s.scalars(select(EnrichedPostRow))))
    config = tweak(config, "problems", cluster_cosine_threshold=0.8, borderline_cosine=0.0)
    tagger = FakeTagger([SameProblem(same=False)] * 10)
    first = ProblemBuilder(feed_store, reader, config, LLMTask(feed_store, config.enrichment, tagger, clock=lambda: NOW),
                           clock=lambda: NOW).run()
    assert first.problems == 2 and first.new == 2 and first.merge_checks == 1 and first.merges == 0
    with feed_store.session() as s:
        probs = {p.post_types.get("tool_gap", 0) > 0: p for p in s.scalars(select(ProblemRow))}
    billing, citations = probs[True], probs[False]
    assert billing.competitors == ["Clio"] and billing.score_breakdown["tool_gap"] == 1.0
    assert citations.score_breakdown["money_signal"] == 1.0 and citations.post_count == 8
    assert citations.distinct_authors == 8 and citations.title.startswith("citations problem")
    assert 0 < citations.score < 1 and citations.confidence < config.problems.early_signal_below + 0.3
    # Rebuild: same posts keep the same ids; a "yes" from the merge check joins the two.
    again = ProblemBuilder(feed_store, reader, config, clock=lambda: NOW).run()
    assert (again.kept_ids, again.new, again.gone) == (2, 0, 0)
    merged = ProblemBuilder(feed_store, reader, config,
                            LLMTask(feed_store, config.enrichment, FakeTagger([SameProblem(same=True)]),
                                    clock=lambda: NOW), clock=lambda: NOW).run()
    assert merged.problems == 1 and merged.kept_ids == 1 and merged.gone == 1
    with feed_store.session() as s:
        assert s.query(ProblemPostRow).count() == 14
        assert {p.status for p in s.scalars(select(ProblemRow))} == {"active", "gone"}


def test_score_components_trend_and_buyers(feed_config):
    cfg = feed_config.problems

    def ev(days_ago, **row):
        p = RawPost(source="s", source_type="community", route="rss", url=f"https://x.example/{days_ago}/{len(row)}",
                    text="t", is_snippet=False, published_at=NOW - timedelta(days=days_ago))
        return Evidence(EnrichedPostRow(post_id=p.id, status="ok", post_type=row.get("post_type", "pain"),
                                        intensity=0.5, buyer_likelihood=row.get("buyer", 0.9), prompt_version="v",
                                        enriched_at=NOW), p)

    rising = [ev(d) for d in (1, 2, 3, 4, 5, 40)]
    falling = [ev(d) for d in (1, 35, 40, 45, 50, 55)]
    up, down = components(rising, cfg, NOW, True), components(falling, cfg, NOW, True)
    assert up["trend"] > 0.5 > down["trend"]
    assert up["buyer_likelihood"] == 1.0 and up["source_diversity"] == 1 / cfg.source_types_full_at
    # A tender is no longer money evidence by its type alone; demand items have their own components.
    assert components([ev(1, buyer=0.2, post_type="paid_demand")], cfg, NOW, True)["money_signal"] == 0.0
    assert set(components(rising, cfg, NOW, True, "demand")) == {"frequency", "trend", "money_signal",
                                                                  "source_diversity"}


def test_posts_tagged_with_a_seeded_branch_land_in_it(tmp_path, feed_store, config):
    reader = build_corpus(tmp_path, feed_store, [("citations", 5, ["Legal", "litigation support", "Citation checks"], {}),
                                                 ("billing", 3, ["Legal", "Made-up branch"], {})])
    seed_tree(feed_store)
    with feed_store.session() as s:
        placed = place_posts(feed_store, list(s.scalars(select(EnrichedPostRow))))
    labels = {n.id: n.label for n in nodes(feed_store)}
    got = sorted(labels[p.niche_id] for p in placed if p.tree == INDUSTRY)
    assert got == ["Legal"] * 3 + ["Litigation support"] * 5      # unknown branch names stay on the industry
    assert len(nodes(feed_store, label="Litigation support", seeded=True)) == 1


def test_seeds_are_never_retired_and_survive_merges(tmp_path, feed_store, config):
    reader = build_corpus(tmp_path, feed_store, LEGAL)
    seed_tree(feed_store)
    seed = nodes(feed_store, label="Contract work")[0]
    c = TOPICS["intake"].tolist()
    with feed_store.session() as s, s.begin():
        s.get(NicheRow, seed.id).centroid_embedding = c
        grown = NicheRow(tree=INDUSTRY, label="Contract drafting", parent_id=seed.parent_id, level=2,
                         centroid_embedding=c, support=90, created_at=NOW, assign_min_cosine=0.99)
        s.add(grown)
        s.flush()
        for w in range(8):
            s.add(NicheSupportRow(niche_id=seed.id, week=week_of(NOW) - timedelta(weeks=w + 1), support=0))
    stats = NicheGrower(feed_store, reader, config, NicheNamer(None), clock=lambda: NOW).run(split=False)
    assert stats.retired == []
    assert stats.merged == ["Legal > Contract drafting -> Contract work"]
    with feed_store.session() as s:
        assert s.get(NicheRow, seed.id).status == "active" and s.get(NicheRow, grown.id).merged_into_id == seed.id


def test_tenders_form_demand_items_scored_apart_from_pain(tmp_path, feed_store, config):
    # Same topic, same niche: pain posts and tenders still never share a cluster.
    reader = build_corpus(tmp_path, feed_store, [
        ("citations", 4, ["Legal"], {}),
        ("citations", 3, ["Legal"], {"post_type": "paid_demand", "money_signal": {"kind": "contract_value"}}),
    ], spread=0.02)
    seed_tree(feed_store)
    with feed_store.session() as s:
        place_posts(feed_store, list(s.scalars(select(EnrichedPostRow))))
    stats = ProblemBuilder(feed_store, reader, config, clock=lambda: NOW).run()
    assert (stats.problems, stats.demand) == (1, 1)
    with feed_store.session() as s:
        by_kind = {p.kind: p for p in s.scalars(select(ProblemRow))}
    demand, pain = by_kind["demand"], by_kind["pain"]
    assert demand.post_count == 3 and set(demand.score_breakdown) == {"frequency", "trend", "money_signal",
                                                                        "source_diversity"}
    assert demand.score == pytest.approx(score(demand.score_breakdown, config.problems, "demand"), abs=1e-3)
    assert pain.post_count == 4 and pain.score_breakdown["money_signal"] == 0.0


def test_one_authors_demand_posts_are_one_demand_item(tmp_path, feed_store, config):
    paid = {"post_type": "paid_demand"}
    reader = build_corpus(tmp_path, feed_store, [
        ("citations", 2, ["Legal"], paid | {"_author": MAINTAINER}),
        ("billing", 2, ["Legal"], paid | {"_author": MAINTAINER}),
        ("intake", 2, ["Legal"], paid),                        # authorless tenders: never joined
        ("intake", 1, ["Legal"], {"post_type": "pain", "_author": MAINTAINER}),
    ], spread=0.02)
    seed_tree(feed_store)
    with feed_store.session() as s:
        place_posts(feed_store, list(s.scalars(select(EnrichedPostRow))))
    stats = ProblemBuilder(feed_store, reader, config, clock=lambda: NOW).run()
    assert stats.same_author_merges == 1 and (stats.demand, stats.problems) == (2, 1)
    with feed_store.session() as s:
        joined = next(p for p in s.scalars(select(ProblemRow)) if p.kind == "demand" and p.post_count == 4)
    assert joined.distinct_authors == 1


def test_grown_niches_need_a_real_match_to_take_a_post(feed_store):
    seed_tree(feed_store)
    other = nodes(feed_store, label="Other")[0]
    with feed_store.session() as s, s.begin():
        s.get(NicheRow, other.id).centroid_embedding = TOPICS["billing"].tolist()
        s.add(NicheRow(tree=INDUSTRY, label="Loose bucket", parent_id=other.id, level=2,
                       centroid_embedding=TOPICS["citations"].tolist(), created_at=NOW, assign_min_cosine=0.1))
    t = Tree.load(feed_store, INDUSTRY)
    halfway = TOPICS["citations"] * 0.5 + TOPICS["intake"] * 0.5
    halfway /= np.linalg.norm(halfway)
    near = np.array(vec("citations", 0.02))
    assert t.nodes[t.descend(other.id, near, 0.55)[0]].label == "Loose bucket"
    assert t.descend(other.id, halfway, 0.95)[0] == other.id               # below the fixed floor
    toward_parent = TOPICS["billing"] * 0.8 + TOPICS["citations"] * 0.2
    toward_parent /= np.linalg.norm(toward_parent)
    assert t.descend(other.id, toward_parent, 0.0)[0] == other.id          # closer to the parent


def test_loose_grown_niches_are_retired(tmp_path, feed_store, config):
    reader = build_corpus(tmp_path, feed_store, [("citations", 20, ["Other"], {}), ("billing", 20, ["Other"], {}),
                                                 ("intake", 20, ["Other"], {})])
    seed_tree(feed_store)
    other = nodes(feed_store, label="Other")[0]
    mixed = sum(TOPICS.values())
    with feed_store.session() as s, s.begin():
        loose = NicheRow(tree=INDUSTRY, label="Grab bag", parent_id=other.id, level=2,
                         centroid_embedding=(mixed / np.linalg.norm(mixed)).tolist(), created_at=NOW,
                         assign_min_cosine=0.0)
        s.add(loose)
    stats = NicheGrower(feed_store, reader, config, NicheNamer(None), clock=lambda: NOW).run(split=False)
    assert "Other > Grab bag" in stats.retired
    with feed_store.session() as s:
        version = s.query(NicheVersionRow).filter_by(niche_id=loose.id, change="retired").one()
    assert version.detail["reason"] == "loose" and version.detail["cohesion"] < config.niches.child_min_cohesion


class FakeEmbedder:
    """Embeds "<Industry> > <Branch>: ..." as a topic vector chosen by branch name."""

    def __init__(self, by_branch):
        self.by_branch, self.calls = by_branch, []

    def embed(self, texts, input_type="document"):
        from signal_feed.enrichment.embeddings import EmbedResult
        self.calls.append((list(texts), input_type))
        vecs = [self.by_branch.get(t.split(" > ")[1].split(":")[0], TOPICS["intake"] * -1).tolist() for t in texts]
        return EmbedResult(vecs, 10 * len(texts), "voyage-4")


def test_untagged_posts_go_to_the_clearly_nearest_branch(tmp_path, feed_store, config):
    from signal_feed.niches.tree import embed_seed_labels
    reader = build_corpus(tmp_path, feed_store, [("citations", 3, ["Legal", "Brief checking"], {}),
                                                 ("billing", 2, ["Legal"], {})], spread=0.02)
    seed_tree(feed_store)
    embedder = FakeEmbedder({"Litigation support": TOPICS["citations"], "Law firm operations": TOPICS["billing"]})
    total = sum(len(b) for b in niche_seeds().values())
    assert embed_seed_labels(feed_store, embedder, config, clock=lambda: NOW) == total
    assert embedder.calls[0][1] == "query" and embed_seed_labels(feed_store, embedder, config) == 0
    with feed_store.session() as s:
        rows = list(s.scalars(select(EnrichedPostRow)))
        assert s.query(FeedCostRow).filter(FeedCostRow.provider == "voyage").count() == 1
    labels = {n.id: n.label for n in nodes(feed_store)}

    def placed_labels(margin):
        return sorted(labels[p.niche_id] for p in placements(rows, Tree.load(feed_store, INDUSTRY),
                                                             Tree.load(feed_store, SKILL), 0.0, margin)
                      if p.tree == INDUSTRY)

    assert placed_labels(0.04) == ["Law firm operations"] * 2 + ["Litigation support"] * 3
    assert placed_labels(None) == ["Legal"] * 5             # tag only: no branch named "Brief checking"
    assert placed_labels(5.0) == ["Legal"] * 5              # an impossible margin: every call is too close


def test_editing_a_branch_description_re_embeds_it(feed_store, config, monkeypatch):
    from signal_feed.niches import tree as tree_mod
    seed_tree(feed_store)
    embedder = FakeEmbedder({})
    tree_mod.embed_seed_labels(feed_store, embedder)
    seeds = dict(tree_mod.niche_seeds())
    seeds["Legal"] = tuple((n, "now also covers e-discovery") if n == "Litigation support" else (n, c)
                           for n, c in seeds["Legal"])
    monkeypatch.setattr(tree_mod, "niche_seeds", lambda: seeds)
    assert tree_mod.embed_seed_labels(feed_store, embedder) == 1
    assert embedder.calls[-1][0] == ["Legal > Litigation support: now also covers e-discovery"]


def test_a_branch_removed_from_the_config_is_retired(feed_store, monkeypatch):
    from signal_feed.niches import tree as tree_mod
    seed_tree(feed_store)
    with feed_store.session() as s, s.begin():
        s.query(NicheRow).filter_by(label="Contract work").one().support = 12
    seeds = dict(tree_mod.niche_seeds())
    seeds["Legal"] = tuple(b for b in seeds["Legal"] if b[0] not in ("Contract work", "Immigration law"))
    monkeypatch.setattr(tree_mod, "niche_seeds", lambda: seeds)
    seed_tree(feed_store)
    with feed_store.session() as s:
        empty = s.query(NicheRow).filter_by(label="Immigration law").one()
        busy = s.query(NicheRow).filter_by(label="Contract work").one()
        assert (empty.status, empty.seeded) == ("retired", False)
        # Posts reached it only by name, so it could never fill again: retired too (state moves to the parent).
        assert (busy.status, busy.seeded) == ("retired", False)


def test_growth_can_include_the_skill_tree(tmp_path, feed_store, config):
    reader = build_corpus(tmp_path, feed_store, LEGAL)
    seed_tree(feed_store)
    config = tweak(config, "niches", grow_trees=["industry", "skill"])
    stats = NicheGrower(feed_store, reader, config, NicheNamer(None), clock=lambda: NOW).run()
    assert any(c.startswith("AI agents > ") for c in stats.created)
    # Turning skill growth off retires what it grew.
    config = tweak(config, "niches", grow_trees=["industry"])
    stats = NicheGrower(feed_store, reader, config, NicheNamer(None), clock=lambda: NOW).run()
    assert stats.retired and all(r.startswith("AI agents > ") for r in stats.retired)
