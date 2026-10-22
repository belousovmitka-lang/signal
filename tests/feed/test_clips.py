""""Clip to Signal" clips as interest signals (signal_feed/feed/clips.py)."""

import numpy as np
import pytest
from sqlalchemy import select

from signal_connectors.clips import ClipIn, delete_clip, save_clip
from signal_connectors.store import Store
from signal_feed.db import FeedCostRow, UserClipRow, UserNicheAffinityRow, UserSkillAffinityRow
from signal_feed.enrichment.embeddings import EmbedResult
from signal_feed.feed.clips import sync_clips
from signal_feed.feed.data import FeedDataCache, load
from signal_feed.feed.ranker import RankerV1
from signal_feed.feed.service import FeedService
from signal_feed.feed.usermodel import load_state, onboard

from .test_feed import NOW, world  # noqa: F401  (fixture)


class FakeEmbedder:
    """Returns the vector of the first topic word found in the text (else a random direction)."""

    def __init__(self, topics):
        self.topics, self.calls = topics, []

    def embed(self, texts, input_type="document"):
        self.calls.append(list(texts))
        rng = np.random.default_rng(len(self.calls))
        out = [next((v for word, v in self.topics.items() if word in t), None) for t in texts]
        out = [v if v is not None else rng.normal(size=len(next(iter(self.topics.values())))) for v in out]
        return EmbedResult(vectors=[(np.asarray(v) / np.linalg.norm(v)).tolist() for v in out],
                           total_tokens=10 * len(texts), model="voyage-test")


@pytest.fixture
def setup(world, feed_store, feed_config, tmp_path):  # noqa: F811
    reader, nodes, skills, raw, _ = world
    data = load(feed_store, reader, feed_config)
    pharmacy = [it.index for it in data.items if it.niche_id == nodes["Healthcare > Pharmacy"]]
    topics = {"pharmacy": data.vectors[pharmacy].mean(axis=0)}
    uid = onboard(feed_store, feed_config, ["Legal"], [], clock=lambda: NOW)
    return reader, nodes, skills, data, uid, Store(f"sqlite:///{tmp_path / 'signal.db'}"), FakeEmbedder(topics)


def affinity(feed_store, cls, uid, node):
    with feed_store.session() as s:
        r = s.get(cls, (uid, node))
        return None if r is None else r.alpha / (r.alpha + r.beta)


def test_a_clip_moves_the_niche_of_its_nearest_posts_and_ranks_like_a_save(setup, feed_store, feed_config):
    reader, nodes, skills, data, uid, signal, emb = setup
    leaf, parent = nodes["Healthcare > Pharmacy"], nodes["Healthcare"]
    save_clip(signal, ClipIn(user_id=uid, url="https://forum.example/t/9", title="Refills",
                             text="Our pharmacy still phones every refill request in by hand", note="this"))
    assert sync_clips(feed_store, reader, feed_config, data, emb, uid, clock=lambda: NOW) == 1
    assert affinity(feed_store, UserNicheAffinityRow, uid, leaf) > feed_config.user_model.default_affinity
    assert affinity(feed_store, UserNicheAffinityRow, uid, parent) > feed_config.user_model.default_affinity
    assert affinity(feed_store, UserSkillAffinityRow, uid, skills["AI agents"]) > feed_config.user_model.default_affinity
    with feed_store.session() as s:
        row = s.scalars(select(UserClipRow)).one()
        assert row.user_id == uid and row.embedding and "refill" not in str(row.niche_deltas)   # no clip text kept
        assert s.scalars(select(FeedCostRow.purpose)).all() == ["embed_clip"]

    # Nothing new: no embedding call.
    assert sync_clips(feed_store, reader, feed_config, data, emb, uid, clock=lambda: NOW) == 0 and len(emb.calls) == 1

    # The clip ranks posts like it the way a saved post would.
    state = load_state(feed_store, feed_config, uid, NOW)
    assert len(state.clips) == 1
    from signal_feed.feed.candidates import Candidate
    farm = next(it.index for it in data.items if it.niche_id == nodes["Agriculture > Farm equipment"])
    pharm = next(it.index for it in data.items if it.niche_id == leaf)
    scored = RankerV1(feed_config).score(data, state, [Candidate(pharm, "fill"), Candidate(farm, "fill")], NOW)
    sims = {s.candidate.index: s.breakdown["features"]["save_sim"] for s in scored}
    assert sims[pharm] > 0.5 > sims[farm]


def test_deleting_a_clip_takes_its_evidence_back(setup, feed_store, feed_config):
    reader, nodes, _, data, uid, signal, emb = setup
    leaf = nodes["Healthcare > Pharmacy"]
    clip = save_clip(signal, ClipIn(user_id=uid, url="https://forum.example/t/9", text="pharmacy refills by hand"))
    sync_clips(feed_store, reader, feed_config, data, emb, uid, clock=lambda: NOW)
    assert affinity(feed_store, UserNicheAffinityRow, uid, leaf) > feed_config.user_model.default_affinity
    delete_clip(signal, uid, clip.id)
    assert sync_clips(feed_store, reader, feed_config, data, None, uid, clock=lambda: NOW) == 1   # no embedder needed
    assert affinity(feed_store, UserNicheAffinityRow, uid, leaf) == pytest.approx(feed_config.user_model.default_affinity)
    with feed_store.session() as s:
        assert s.scalars(select(UserClipRow)).all() == []


def test_an_unplaceable_clip_or_unknown_user_moves_no_interests(setup, feed_store, feed_config):
    reader, nodes, _, data, uid, signal, emb = setup
    save_clip(signal, ClipIn(user_id=uid, url="https://news.example/a", text="something about lighthouses"))
    save_clip(signal, ClipIn(user_id="not-a-user", url="https://news.example/b", text="pharmacy refills by hand"))
    assert sync_clips(feed_store, reader, feed_config, data, emb, None, clock=lambda: NOW) == 1
    with feed_store.session() as s:
        row = s.scalars(select(UserClipRow)).one()
        assert row.user_id == uid and row.niche_deltas == {} and row.embedding       # kept for ranking only
        assert s.get(UserNicheAffinityRow, (uid, nodes["Healthcare > Pharmacy"])) is None


def test_the_feed_learns_a_new_clip_on_the_next_page(setup, feed_store, feed_config):
    reader, nodes, _, _, uid, signal, emb = setup
    save_clip(signal, ClipIn(user_id=uid, url="https://forum.example/t/9", text="pharmacy refills by hand"))
    service = FeedService(feed_store, feed_config, FeedDataCache(feed_store, reader), clock=lambda: NOW, seed=1,
                          clip_embedder=emb)
    service.page(uid, "s1")
    assert affinity(feed_store, UserNicheAffinityRow, uid, nodes["Healthcare > Pharmacy"]) \
        > feed_config.user_model.default_affinity
