"""G5: XenForo, vBulletin, phpBB and other forums, read through their board RSS feeds.

Feeds come from industries/*.yaml forums that are collectable: live, robots.txt allows us,
terms read by hand and "allowed", and a feed detected (verify_industries.py). Most large
forums forbid automated or commercial use (VerticalScope, Shopify, Ziff Davis, CC BY-NC
licensed boards), so this list is short by design.

Posts are stored as source "forum:<host>", type community, with the board as community.
"""

from __future__ import annotations

from typing import Any

from ..models import SourceQuery
from . import register
from .rss import RssConnector


@register
class ClassicForumConnector(RssConnector):
    connector = "classic_forum"
    route = "rss"
    source_type = "community"

    def adapt(self, fields: dict[str, Any], entry: Any, query: SourceQuery) -> dict[str, Any]:
        boards = [t.get("term") for t in entry.get("tags", []) if t.get("term")]
        forum = query.params.get("community") or fields["community"]
        fields = dict(fields)
        fields["source"] = f"forum:{query.params.get('name')}"
        fields["source_type"] = "community"
        fields["community"] = f"{forum} / {boards[0]}" if boards else forum
        fields["raw"] = {**fields["raw"], "software": query.params.get("software"), "boards": boards}
        if "comments" in fields["engagement"]:
            fields["engagement"] = {"replies": fields["engagement"]["comments"]}
        return fields
