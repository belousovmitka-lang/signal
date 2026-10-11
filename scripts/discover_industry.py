"""Draft an industry seed map with the discovery agent (Phase 6), for human review.

    python scripts/discover_industry.py veterinary --name "Veterinary clinics" \
        --roles "veterinarian,vet tech,practice manager" --keywords "vet clinic,veterinary practice" \
        --tools "ezyVet,Cornerstone" --budget 0.25

Writes industries/drafts/<key>.yaml (refuses to overwrite without --force). Drafts are not loaded by
anything; approving one = moving it to industries/ and setting discovery.needs_review: false. Searches are metered against
the "discovery" budget and the shared Brave/Exa caps.
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_connectors.discovery_agent import Seeds, discover  # noqa: E402
from signal_connectors.industries import INDUSTRIES_DIR, save_industry  # noqa: E402
from signal_connectors.services import Services  # noqa: E402


def split(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


async def main(args: argparse.Namespace) -> int:
    out = Path(args.out) if args.out else INDUSTRIES_DIR / "drafts" / f"{args.key}.yaml"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists() and not args.force:
        print(f"{out} exists; use --force to replace it")
        return 1
    seeds = Seeds(args.key, args.name, split(args.roles), split(args.keywords), split(args.tools))
    async with Services.open() as services:
        industry = await discover(services, seeds, budget_usd=args.budget, max_sites=args.max_sites)
    save_industry(industry, out)
    d = industry.discovery
    print(f"wrote {out}: {len(industry.forums)} forums, {len(industry.rss_feeds)} feeds, "
          f"{len(industry.associations)} associations from {d.candidates_checked} sites, {d.queries} searches, ${d.cost_usd}")
    for f in industry.forums:
        print(f"  forum  {f.status:11} {f.software:9} {f.name[:50]:50} terms: {f.terms_url or '?'}")
    for f in industry.rss_feeds:
        print(f"  feed   {f.status:11} {f.name:20} {f.url}")
    for a in industry.associations:
        print(f"  assoc  {a.status:11} {a.name[:60]}")
    print(f"  jargon candidates: {', '.join(industry.vocabulary.jargon)}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    p = argparse.ArgumentParser()
    p.add_argument("key")
    p.add_argument("--name", required=True)
    p.add_argument("--roles")
    p.add_argument("--keywords")
    p.add_argument("--tools")
    p.add_argument("--budget", type=float, default=0.25)
    p.add_argument("--max-sites", type=int, default=40)
    p.add_argument("--force", action="store_true")
    p.add_argument("--out", help="write here instead of industries/<key>.yaml")
    sys.exit(asyncio.run(main(p.parse_args())))
