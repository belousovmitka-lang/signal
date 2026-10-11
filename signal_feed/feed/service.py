"""One feed page: calibration on a new user's first visit (section 6.2 step 2), otherwise candidates ->
ranker -> composition. Every item shown is logged as an impression with its explanation and score
breakdown (section 16), and drop-ins spend one exposure per page they appear on."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from types import SimpleNamespace

import numpy as np
from pydantic import BaseModel
from sqlalchemy import func, select

from signal_connectors.models import utcnow

from ..config import FeedConfig
from ..db import FeedStore, ImpressionRow, PostNicheRow, UserDropInRow
from .candidates import DROP_IN, Candidate, generate
from .compose import compose
from .data import FeedData, FeedDataCache
from .ranker import RankerV1, RankerV2
from .usermodel import UserState, load_state

CALIBRATION = "calibration"


class FeedItem(BaseModel):
    impression_id: int
    position: int
    post_id: str
    problem_id: int | None
    similar_posts: int
    post_type: str
    title: str | None
    summary: str | None
    url: str
    source: str
    published_at: datetime
    niche_path: list[str]
    money: dict | None
    calibration: bool = False  # show the Interested / Not for me answer buttons
    ask_useful: bool = False   # after the user reads it, ask "Was this useful?" (about 1 item in 30)
    score: float | None
    # For debugging and evaluation only: includes the internal `reason` the item was picked for. The app
    # never shows why an item is in the feed.
    score_breakdown: dict


class FeedPage(BaseModel):
    user_id: str
    session_id: str
    items: list[FeedItem]
    cursor: str                # opaque: how many items this session has shown
    caught_up: bool            # strong items ran out: show "You're caught up" and offer "Explore more"
    calibration: bool


def _session_shown(store: FeedStore, user_id: str, session_id: str) -> list[str]:
    with store.session() as s:
        return list(s.scalars(select(ImpressionRow.item_id).where(
            ImpressionRow.user_id == user_id, ImpressionRow.session_id == session_id).order_by(ImpressionRow.position)))


def _has_impressions(store: FeedStore, user_id: str) -> bool:
    with store.session() as s:
        return bool(s.scalar(select(func.count()).select_from(ImpressionRow).where(ImpressionRow.user_id == user_id)))


def calibration_deck(data: FeedData, state: UserState, config: FeedConfig, rng: np.random.Generator) -> list[Candidate]:
    """Cards spanning the quiz's industries, round-robin, best problem first, plus wildcards from
    industries the user didn't pick."""
    um = config.user_model
    chosen = [n for n in state.niche if n in data.industry.nodes]
    wild = max(0, min(um.calibration_wildcards, um.calibration_cards))
    # Per chosen node, alternate a demand card and a non-demand card so the deck spans post types.
    def alternating(node: int) -> list[int]:
        ranked = sorted(data.subtree_items(node), key=lambda i: -data.items[i].problem_rank)
        demand = [i for i in ranked if data.items[i].post_type == "paid_demand"]
        other = [i for i in ranked if data.items[i].post_type != "paid_demand"]
        mixed = [x for pair in zip(other, demand) for x in pair]
        return mixed + other[len(demand):] + demand[len(other):]

    lists = [alternating(n) for n in chosen]
    deck: list[Candidate] = []
    seen_problems: set = set()

    def add(i: int, node: int) -> bool:
        it = data.items[i]
        key = it.problem_id or it.post_id
        if key in seen_problems or it.source in state.hidden_sources:
            return False
        seen_problems.add(key)
        deck.append(Candidate(i, CALIBRATION, node))
        return True

    want = um.calibration_cards - wild if chosen else um.calibration_cards
    while len(deck) < want and any(lists):
        for node, items in zip(chosen, lists):
            while items and not add(items.pop(0), node):
                pass
            if len(deck) >= want:
                break
    roots = [r for r in data.industry.roots.values() if not any(r in data.ancestors(n) for n in chosen)]
    rng.shuffle(roots)
    for root in roots:
        if len(deck) >= um.calibration_cards:
            break
        best = alternating(root)
        for i in best[:5]:
            if add(i, root):
                break
    return deck


def bubble_share(store: FeedStore, user_id: str, window: int) -> float:
    """Share of the user's last `window` impressions taken by their most-shown leaf niche."""
    with store.session() as s:
        recent = select(ImpressionRow.item_id).where(ImpressionRow.user_id == user_id) \
            .order_by(ImpressionRow.id.desc()).limit(window).subquery()
        leaves = list(s.scalars(select(PostNicheRow.niche_id).join(recent, recent.c.item_id == PostNicheRow.post_id)
                                .where(PostNicheRow.tree == "industry")))
    if len(leaves) < window:
        return 0.0
    return max(leaves.count(n) for n in set(leaves)) / len(leaves)


class FeedService:
    def __init__(self, store: FeedStore, config: FeedConfig, cache: FeedDataCache,
                 clock: Callable[[], datetime] = utcnow, seed: int | None = None) -> None:
        self.store, self.config, self.cache, self.clock = store, config, cache, clock
        self.rng = np.random.default_rng(seed)
        self._models: tuple = ()
        self.ranker = RankerV1(config)

    def current_ranker(self) -> RankerV1:
        """v1, or v2 once a trained model was activated (after beating v1 offline); either one blends in
        the active value model. Models are reloaded only when the active ones change."""
        from ..db import ModelRow
        from ..learning.models import V2Model, ValueModel
        with self.store.session() as s:
            active = tuple(s.execute(select(ModelRow.kind, ModelRow.id).where(ModelRow.status == "active")
                                     .order_by(ModelRow.id)).all())
        if active != self._models:
            value = ValueModel.load_active(self.store)
            v2 = V2Model.load(self.store) if any(k == "v2" for k, _ in active) else None
            self.ranker = RankerV2(self.config, v2, value) if v2 is not None else RankerV1(self.config, value)
            self._models = active
        return self.ranker

    def page(self, user_id: str, session_id: str, explore_more: bool = False) -> FeedPage:
        now = self.clock()
        data = self.cache.get()
        state = load_state(self.store, self.config, user_id, now, self.config.ranking.v1.recent_saves)
        shown = _session_shown(self.store, user_id, session_id)
        ranker = self.current_ranker()
        if not _has_impressions(self.store, user_id):
            deck = calibration_deck(data, state, self.config, self.rng)
            scored = ranker.score(data, state, deck, now)
            rows = [(s, "calibration", "calibration") for s in scored]
            return self._log(data, user_id, session_id, rows, len(shown), caught_up=False, calibration=True)
        # Filter-bubble guard (section 10): one leaf over half of the last 100 items -> 20% exploration.
        lc = self.config.learning
        boost = lc.bubble_exploration if bubble_share(self.store, user_id, lc.bubble_window_impressions) \
            > lc.bubble_leaf_share else None
        candidates = generate(data, state, self.config, self.store, now, self.rng, explore_more, boost)
        scored = ranker.score(data, state, candidates, now, self.rng)
        previous = [SimpleNamespace(index=data.by_id[p].index) for p in shown[-10:] if p in data.by_id]
        placed, caught_up = compose(scored, data, state, self.config, now, previous, explore_more, boost)
        rows = [(p.scored, p.explanation, p.forced) for p in placed]
        return self._log(data, user_id, session_id, rows, len(shown), caught_up, calibration=False,
                         bubble_guard=boost is not None)

    def _log(self, data: FeedData, user_id: str, session_id: str, rows, offset: int, caught_up: bool,
             calibration: bool, bubble_guard: bool = False) -> FeedPage:
        now = self.clock()
        items, impressions = [], []
        every = self.config.ranking.survey_every_n_items
        with self.store.session() as s, s.begin():
            seen_before = s.scalar(select(func.count()).select_from(ImpressionRow).where(ImpressionRow.user_id == user_id)) or 0
            for k, (scored, explanation, rule) in enumerate(rows):
                it = data.items[scored.index]
                breakdown = scored.breakdown | {"reason": explanation} | ({"rule": rule} if rule else {}) \
                    | ({"bubble_guard": True} if bubble_guard else {})
                imp = ImpressionRow(user_id=user_id, session_id=session_id, item_type="post", item_id=it.post_id,
                                    position=offset + k, candidate_source=scored.candidate.source,
                                    explanation=explanation, score=scored.score, score_breakdown=breakdown,
                                    shown_at=now, config_version=self.config.version)
                s.add(imp)
                impressions.append((imp, scored, it, explanation, breakdown))
            s.flush()
            for node in {sc.candidate.node for _, sc, _, _, _ in impressions if sc.candidate.source == DROP_IN}:
                row = s.get(UserDropInRow, (user_id, node))
                if row is not None and row.exposures_left > 0:
                    row.exposures_left -= 1
            for imp, scored, it, explanation, breakdown in impressions:
                items.append(FeedItem(
                    impression_id=imp.id, position=imp.position, post_id=it.post_id, problem_id=it.problem_id,
                    similar_posts=it.similar_posts, post_type=it.post_type, title=it.title, summary=it.summary,
                    url=it.url, source=it.source, published_at=it.published_at, niche_path=data.path(it.niche_id),
                    money=it.money, calibration=calibration, score=scored.score,
                    ask_useful=not calibration and (seen_before + imp.position - offset + 1) % every == 0,
                    score_breakdown=breakdown))
        return FeedPage(user_id=user_id, session_id=session_id, items=items, cursor=str(offset + len(items)),
                        caught_up=caught_up, calibration=calibration)
