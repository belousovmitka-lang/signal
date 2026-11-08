"""Step 2: job clusters. The core is always 80+ posts; 60-79 posts only support."""

from datetime import datetime, timezone

from signal_connectors.models import RawPost
from signal_connectors.store import Store
from signal_feed.db import EnrichedPostRow, JobClusterPostRow, JobClusterRow
from signal_feed.enrichment.embeddings import EmbedResult
from signal_feed.opportunities.clusters import JobClusterBuilder
from signal_feed.posts import PostsReader

NOW = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)


def test_cores_form_clusters_and_60s_only_attach(feed_store, feed_config, tmp_path):
    version = feed_config.enrichment.opportunity_fields.version
    url = f"sqlite:///{tmp_path / 'sig.db'}"
    specs = [  # (source, score, need vector, money)
        ("hackernews", 85, [1, 0, 0], {"kind": "budget", "amount_min": 400, "amount_max": 600, "currency": "USD"}),
        ("github", 90, [0.98, 0.1, 0], None),              # same need as the first: one cluster
        ("bluesky", 70, [0.95, 0.05, 0], None),            # 60-79: supports that cluster
        ("lemmy", 72, [0, 0, 1], None),                    # 60-79 with no core near it: no cluster of its own
        ("mastodon", 82, [0, 1, 0], None),                 # a second core need
        ("ted", 75, [0.97, 0, 0.1], None),                 # non-US may support a US core
        ("forum", 55, [1, 0, 0], None),                    # under 60: not evidence at all
    ]
    posts = [RawPost(source=src, source_type="community", route="own_api", url=f"https://e.example/{i}",
                     text=f"post {i}", is_snippet=False, author_hash=f"{i:064x}",
                     published_at=datetime(2026, 10, 1 + i, tzinfo=timezone.utc)) for i, (src, *_) in enumerate(specs)]
    Store(url).save_posts(posts, text_dedupe=False)
    with feed_store.session() as s, s.begin():
        for p, (src, score, vec, money) in zip(posts, specs, strict=True):
            s.add(EnrichedPostRow(post_id=p.id, status="ok", post_type="pain", prompt_version="v9", enriched_at=NOW,
                                  opp_version=version, project_score=score, project_type="software",
                                  opp_confidence=0.8, market="local" if src == "ted" else "global",
                                  us_version=f"need {vec}", buyer_type="small_business", recurrence="recurring",
                                  money_signal=money, need_embedding=vec))

    class NoCalls:
        def embed(self, texts, input_type="document"):
            raise AssertionError("every need is embedded already")
            return EmbedResult([], 0, "x")
    clusters = JobClusterBuilder(feed_store, PostsReader(url), feed_config, embedder=NoCalls(), clock=lambda: NOW).build()
    with feed_store.session() as s:
        members = {(m.cluster_id, m.post_id): m.role for m in s.scalars(select_all(JobClusterPostRow))}
        rows = {r.id: r for r in s.scalars(select_all(JobClusterRow))}
    assert len(clusters) == 2 and all(r.core_posts >= 1 for r in rows.values())
    big = next(r for r in rows.values() if r.core_posts == 2)
    roles = {pid: role for (cid, pid), role in members.items() if cid == big.id}
    assert roles == {posts[0].id: "core", posts[1].id: "core", posts[2].id: "supporting", posts[5].id: "supporting"}
    assert (big.supporting_posts, big.distinct_buyers, big.best_score) == (2, 4, 90)
    assert big.price == {"count": 1, "median": 500.0, "p25": 500.0, "p75": 500.0}
    assert posts[3].id not in {pid for _, pid in members} and posts[6].id not in {pid for _, pid in members}
    assert all(role == "core" for (cid, _), role in members.items() if rows[cid].core_posts == 1
               and rows[cid].supporting_posts == 0)


def select_all(model):
    from sqlalchemy import select
    return select(model)
