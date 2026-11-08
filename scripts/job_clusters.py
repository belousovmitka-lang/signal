"""Step 2 of OPPORTUNITIES.md: build job clusters and write a review page.

    python scripts/job_clusters.py build      # embeds new needs (Voyage, cents), rebuilds every cluster
    python scripts/job_clusters.py export     # rewrite data/review/job_clusters.html only
"""

import argparse
import html
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402
from sqlalchemy import select  # noqa: E402

from signal_feed.config import load_feed_config  # noqa: E402
from signal_feed.db import EnrichedPostRow, FeedStore, JobClusterPostRow, JobClusterRow  # noqa: E402
from signal_feed.opportunities.clusters import JobClusterBuilder  # noqa: E402
from signal_feed.posts import PostsReader, _chunks  # noqa: E402
from signal_feed.settings import REPO_ROOT, FeedSettings  # noqa: E402

OUT = REPO_ROOT / "data" / "review" / "job_clusters.html"


def export(store: FeedStore, posts: PostsReader) -> Path:
    e = html.escape
    with store.session() as s:
        clusters = list(s.scalars(select(JobClusterRow)))
        members = list(s.scalars(select(JobClusterPostRow)))
        ids = [m.post_id for m in members]
        rows = {r.post_id: r for part in _chunks(ids)
                for r in s.scalars(select(EnrichedPostRow).where(EnrichedPostRow.post_id.in_(part)))}
    raw = {p.id: p for part in _chunks(ids) for p in posts.get_posts(part)}
    by = {}
    for m in members:
        by.setdefault(m.cluster_id, []).append(m)
    clusters.sort(key=lambda c: (-(c.core_posts + c.supporting_posts), -c.best_score))
    multi = sum(1 for c in clusters if c.core_posts + c.supporting_posts > 1)
    cards = []
    for c in clusters:
        lines = []
        for m in sorted(by.get(c.id, []), key=lambda m: (m.role != "core", -(rows[m.post_id].project_score or 0))):
            r, p = rows.get(m.post_id), raw.get(m.post_id)
            if r is None or p is None:
                continue
            lines.append(f'<li class="{m.role}"><span class="sc">{r.project_score}</span> <b>{m.role}</b> · '
                         f'{e(p.source)} · {m.cosine:.2f} · <a href="{e(p.url)}" target="_blank" rel="noopener noreferrer">'
                         f'{e((r.plain_summary or p.title or "")[:160])}</a></li>')
        price = c.price and f"{c.price['count']} USD amounts, median ${c.price['median']:,.0f}"
        size = c.core_posts + c.supporting_posts
        cards.append(f"""<article data-size="{'multi' if size > 1 else 'single'}"><h2>{e(c.project_idea or c.need)}</h2>
<p class="need">{e(c.need)}</p>
<p class="meta">{c.core_posts} core + {c.supporting_posts} supporting · {c.distinct_buyers} buyers ·
{e(', '.join(f'{k} {v}' for k, v in c.sources.items()))}{' · ' + e(price) if price else ''}
{' · recurring ' + format(c.recurring_share, '.0%') if c.recurring_share is not None else ''} · best score {c.best_score}</p>
<ul>{''.join(lines)}</ul></article>""")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Job clusters</title><style>
:root{{--bg:#f5f5f3;--card:#fff;--ink:#141414;--muted:#5f6064;--line:#e6e6e2;--acc:#e8590c}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151515;--card:#1f1f1f;--ink:#eee;--muted:#a0a0a0;--line:#333}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/22px system-ui,sans-serif}}main{{max-width:900px;margin:0 auto;padding:24px 16px}}
h1{{font-size:22px;margin:0 0 4px}}h2{{font-size:17px;margin:0 0 4px}}.meta,.need{{font-size:13px;color:var(--muted);margin:2px 0}}
article{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:0 0 12px}}
ul{{margin:8px 0 0;padding:0;list-style:none}}li{{font-size:14px;padding:4px 0;border-top:1px solid var(--line);overflow-wrap:anywhere}}
li a{{color:inherit}}li.supporting{{color:var(--muted)}}.sc{{display:inline-block;min-width:28px;font-weight:700}}li.core .sc{{color:var(--acc)}}
</style></head><body><main><h1>{len(clusters)} job clusters, {multi} with more than one post</h1>
<p class="meta">Core = 80+ and shown-eligible; supporting = 60-79. Biggest first. <label><input type="checkbox" id="m" checked> only clusters with 2+ posts</label></p>
{''.join(cards)}</main><script>const m=document.getElementById('m'),a=[...document.querySelectorAll('article')];
function f(){{a.forEach(x=>x.hidden=m.checked&&x.dataset.size!=='multi')}}m.onchange=f;f();</script></body></html>""", encoding="utf-8")
    return OUT


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["build", "export"])
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    load_dotenv(REPO_ROOT / ".env")
    settings = FeedSettings.from_env()
    config = load_feed_config(settings.config_file)
    store, posts = FeedStore(settings.feed_database_url), PostsReader(settings.posts_database_url)
    if args.cmd == "build":
        embedder = None
        if os.environ.get("VOYAGE_API_KEY"):
            from signal_feed.enrichment.embeddings import VoyageEmbedder
            embedder = VoyageEmbedder(config.embeddings)
        clusters = JobClusterBuilder(store, posts, config, embedder=embedder).build()
        print(f"{len(clusters)} clusters; {sum(c.core_posts + c.supporting_posts > 1 for c in clusters)} with 2+ posts")
    print(f"review: {export(store, posts)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
