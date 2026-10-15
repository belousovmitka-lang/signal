"""Detector 3: gaps between tools (section 17.3). People saying they move data between two tools by hand
("export to CSV", "copy orders from Shopify into QuickBooks"). Triples (tool A, tool B, data) come from
the v9 `data_moved` field; posts are grouped by the tool pair, and a pair with enough mentions from
enough source types becomes a candidate. Starts as a `trial` detector (17.11 step 5)."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime

import numpy as np
from sqlalchemy import select

from ..config import FeedConfig
from ..db import EnrichedPostRow, FeedStore, PostNicheRow
from ..posts import PostsReader
from ..vectors import centroid, unit_matrix
from .common import Candidate, Evidence, counter_evidence, meets_minimum
from .paid_workarounds import SEARCHABLE

NAME = "tool_gaps"
TEMPLATE = "Users of {a} and {b} move {data} between them by hand."


def _tool(name: str) -> str:
    return " ".join(name.strip().split())


def find_candidates(store: FeedStore, posts: PostsReader, config: FeedConfig, now: datetime) -> tuple[list[Candidate], int]:
    cfg, icfg = config.inferred.tool_gaps, config.inferred
    with store.session() as s:
        rows = [r for r in s.scalars(select(EnrichedPostRow).where(EnrichedPostRow.data_moved.is_not(None)))
                if r.status in ("ok", "dropped")]          # reviews are often "praise" (kept out of the feed)
        catalog = list(s.scalars(select(EnrichedPostRow).where(EnrichedPostRow.post_type.in_(SEARCHABLE),
                                                               EnrichedPostRow.embedding.is_not(None))))
        leaf = dict(s.execute(select(PostNicheRow.post_id, PostNicheRow.niche_id)
                              .where(PostNicheRow.tree == "industry")).all())
    raw = {p.id: p for p in posts.get_posts([r.post_id for r in rows] + [r.post_id for r in catalog])}
    pairs: dict[tuple[str, str], list[tuple[EnrichedPostRow, str]]] = defaultdict(list)
    for r in rows:
        for d in r.data_moved or []:
            a, b = _tool(d.get("from_tool", "")), _tool(d.get("to_tool", ""))
            if a and b and a.lower() != b.lower() and r.post_id in raw:
                pairs[(a.lower(), b.lower())].append((r, d.get("data", "data").strip() or "data"))
    cat_vectors = unit_matrix([r.embedding for r in catalog]) if catalog else np.zeros((0, 1))
    out = []
    for (a_low, b_low), mentions in pairs.items():
        seen, uniq = set(), []
        for r, data in mentions:
            if r.post_id not in seen:
                seen.add(r.post_id)
                uniq.append((r, data))
        types = {raw[r.post_id].source_type for r, _ in uniq}
        if len(uniq) < cfg.min_mentions or len(types) < cfg.min_source_types:
            continue
        names = Counter()
        for r, _ in uniq:
            for d in r.data_moved or []:
                names[_tool(d.get("from_tool", ""))] += 1
                names[_tool(d.get("to_tool", ""))] += 1
        a = next(n for n, _ in names.most_common() if n.lower() == a_low)
        b = next(n for n, _ in names.most_common() if n.lower() == b_low)
        data = Counter(dt for _, dt in uniq).most_common(1)[0][0]
        evidence = [Evidence(post_id=r.post_id, source=raw[r.post_id].source, source_type=raw[r.post_id].source_type,
                             url=raw[r.post_id].url,
                             what_it_shows=f"{('A ' + r.who_has_pain) if r.who_has_pain else 'A user'} moves {dt} "
                                           f"from {a} to {b} by hand") for r, dt in uniq]
        if not meets_minimum(evidence, icfg):
            continue
        vecs = [r.embedding for r, _ in uniq if r.embedding]
        counters = counter_evidence(centroid(unit_matrix(vecs)), cat_vectors, catalog, raw, icfg,
                                    exclude=seen) if vecs else []
        # A listing or review naming both tools is the strongest sign an integration already exists.
        for r in catalog:
            tools = {t.lower() for t in (r.tools_mentioned or [])}
            if {a_low, b_low} <= tools and r.post_id in raw and r.post_id not in seen:
                counters.insert(0, {"post_id": r.post_id, "source": raw[r.post_id].source, "url": raw[r.post_id].url,
                                    "what_it_shows": f"Names both {a} and {b} (possible integration)"})
        niches = Counter(leaf[r.post_id] for r, _ in uniq if r.post_id in leaf)
        out.append(Candidate(detector=NAME, hypothesis=TEMPLATE.format(a=a, b=b, data=data), who=None,
                             task=f"move {data} from {a} to {b}",
                             niche_id=niches.most_common(1)[0][0] if niches else None, evidence=evidence,
                             counter_evidence=counters, rows=[r for r, _ in uniq],
                             posts=[raw[r.post_id] for r, _ in uniq]))
    return out, len(pairs)
