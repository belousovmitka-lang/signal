"""HTTP API (FEED_ALGORITHM.md section 12): onboarding, the feed and events.

No authentication yet: the user comes from the request until the app's auth exists.
"""

from __future__ import annotations

import os
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from sqlalchemy import select

from .config import FeedConfig, load_feed_config
from .db import FeedStore
from .enrichment.schema import niche_seeds, skill_vocabulary
from .events import BatchResult, BatchTooLargeError, EventBatch, UnknownUserError, log_events
from .feed.data import FeedDataCache
from .feed.service import FeedPage, FeedService
from .db import UserMapSnapshotRow
from .feed import profile as profiles
from .feed import usermap
from .feed.usermodel import UnknownNodeError, apply_events, onboard
from .niches.tree import seed_labels
from .posts import PostsReader
from .settings import FeedSettings


class Quiz(BaseModel):
    """Industries may name a branch: "Healthcare > Pharmacy"."""

    industries: list[str] = Field(min_length=1, max_length=20)
    skills: list[str] = Field(default_factory=list, max_length=20)
    resume_skills: list[str] = Field(default_factory=list, max_length=50)
    works_in: list[str] = Field(default_factory=list, max_length=10)   # industries they work in (insiders, 17.7)


class Onboarded(BaseModel):
    user_id: str


class NodeAction(BaseModel):
    user_id: str
    node_id: int


class PinAction(NodeAction):
    radius: float = Field(ge=0, le=1)          # 0 = the centre (affinity 1), 1 = the rim (affinity 0)
    angle: float | None = None                 # degrees; keeps the current angle when omitted


class RemoveAction(NodeAction):
    allow_retest: bool = False


class NudgeAction(NodeAction):
    direction: Literal["more", "less"]


class UserAction(BaseModel):
    user_id: str


class ModeChange(BaseModel):
    user_id: str
    mode: Literal["exploring", "committed"]
    problem_id: int | None = None          # required for committed


class Vote(BaseModel):
    user_id: str
    vote: Literal["yes", "no", "partly"]
    note: str | None = Field(default=None, max_length=300)


class FieldNote(BaseModel):
    user_id: str
    note: str = Field(min_length=1, max_length=400)
    share: bool                        # the builder's permission to attach it as evidence


class Decision(BaseModel):
    update_id: int
    approve: bool


class Decisions(BaseModel):
    user_id: str
    decisions: list[Decision] = Field(min_length=1, max_length=100)


class AutoPublish(BaseModel):
    user_id: str
    on: bool


def create_app(store: FeedStore, posts: PostsReader, config: FeedConfig, service: FeedService | None = None,
               clips_db_url: str | None = None) -> FastAPI:
    app = FastAPI(title="Signal feed")
    if clips_db_url:      # "Clip to Signal" button: the connectors' per-user clips (POST/GET/DELETE /clips)
        from signal_connectors.clips import router as clips_router
        from signal_connectors.store import Store
        app.include_router(clips_router(Store(clips_db_url)))
    if service is None:
        embedder = None
        if clips_db_url and os.environ.get("VOYAGE_API_KEY"):      # clips move interests (feed/clips.py)
            from .enrichment.embeddings import VoyageEmbedder
            embedder = VoyageEmbedder(config.embeddings)
        service = FeedService(store, config, FeedDataCache(store, posts), clip_embedder=embedder)

    @app.get("/onboarding/options")
    def onboarding_options() -> dict:
        seeds = niche_seeds()
        return {"industries": [{"name": n, "branches": [b for b, _ in seeds.get(n, ())]}
                               for n in seed_labels()["industry"]],
                "skills": [{"name": n, "covers": c} for n, c in skill_vocabulary()]}

    @app.post("/onboarding")
    def post_onboarding(quiz: Quiz) -> Onboarded:
        try:
            return Onboarded(user_id=onboard(store, config, quiz.industries, quiz.skills, quiz.resume_skills,
                                             works_in=quiz.works_in))
        except UnknownNodeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @app.get("/feed")
    def get_feed(user_id: str, session_id: str = Query(min_length=1, max_length=64),
                 cursor: str | None = None, explore: bool = False) -> FeedPage:
        # `cursor` is accepted for API compatibility: the session's own impressions say what was shown.
        if not store.user_exists(user_id):
            raise HTTPException(status_code=404, detail="unknown user")
        return service.page(user_id, session_id, explore_more=explore)

    def _user(user_id: str) -> None:
        if not store.user_exists(user_id):
            raise HTTPException(status_code=404, detail="unknown user")

    def _act(fn, *args, **kw) -> dict:
        try:
            fn(store, config, *args, **kw)
        except usermap.MapError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        return {"ok": True}

    @app.get("/map")
    def get_map(user_id: str) -> dict:
        _user(user_id)
        return usermap.public_view(store, config, user_id)

    @app.get("/map/history")
    def get_map_history(user_id: str) -> dict:
        """Weekly snapshots for Replay. The map is silent: no change log, no statuses."""
        _user(user_id)
        keep = ("node_id", "label", "path", "ring", "radius", "angle")
        with store.session() as s:
            snaps = list(s.scalars(select(UserMapSnapshotRow).where(UserMapSnapshotRow.user_id == user_id)
                                   .order_by(UserMapSnapshotRow.week)))
        return {"snapshots": [{"week": x.week.isoformat(),
                               "nodes": [{k: n[k] for k in keep if k in n} for n in x.data.get("nodes", [])]}
                              for x in snaps]}

    @app.get("/map/node/{node_id}")
    def get_map_node(node_id: int, user_id: str) -> dict:
        _user(user_id)
        try:
            return usermap.node_detail(store, config, user_id, node_id)
        except usermap.MapError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @app.post("/map/pin")
    def post_pin(a: PinAction) -> dict:
        _user(a.user_id)
        return _act(usermap.pin, a.user_id, a.node_id, a.radius, a.angle)

    @app.post("/map/unpin")
    def post_unpin(a: NodeAction) -> dict:
        _user(a.user_id)
        return _act(usermap.unpin, a.user_id, a.node_id)

    @app.post("/map/drop-in")
    def post_drop_in(a: NodeAction) -> dict:
        _user(a.user_id)
        return _act(usermap.drop_in, a.user_id, a.node_id)

    @app.post("/map/remove")
    def post_remove(a: RemoveAction) -> dict:
        _user(a.user_id)
        return _act(usermap.remove, a.user_id, a.node_id, a.allow_retest)

    @app.post("/map/nudge")
    def post_nudge(a: NudgeAction) -> dict:
        _user(a.user_id)
        return _act(usermap.nudge, a.user_id, a.node_id, a.direction)

    @app.post("/map/reset")
    def post_reset(a: UserAction) -> dict:
        _user(a.user_id)
        return _act(usermap.reset, a.user_id)

    @app.get("/inferred/prompt")
    def get_insider_prompt(user_id: str) -> dict:
        """17.7: at most one question a week for insiders: "Is this real where you work?"."""
        _user(user_id)
        from .inferred.validation import next_prompt
        return {"prompt": next_prompt(store, config, user_id)}

    @app.get("/inferred/{inferred_id}")
    def get_inferred(inferred_id: int, user_id: str) -> dict:
        """The full evidence chain ("+N more", 17.8) and what was found against it."""
        _user(user_id)
        from .db import InferredEvidenceRow, InferredProblemRow
        from .inferred.common import shown_evidence
        with store.session() as s:
            p = s.get(InferredProblemRow, inferred_id)
            if p is None or p.status != "published":
                raise HTTPException(status_code=404, detail="unknown inferred problem")
            ev = [e for e in s.scalars(select(InferredEvidenceRow).where(InferredEvidenceRow.inferred_id == p.id))
                  if e.supported is not False]
            return {"inferred_id": p.id, "kind": "inferred", "confidence": p.confidence, "hypothesis": p.hypothesis,
                    "who": p.who, "evidence": [shown_evidence(e.url, e.source, e.what_it_shows) for e in ev],
                    "counter_evidence": [shown_evidence(c.get("url"), c.get("source") or "", c.get("what_it_shows"))
                                         for c in p.counter_evidence or []]}

    @app.post("/inferred/{inferred_id}/vote")
    def post_vote(inferred_id: int, v: Vote) -> dict:
        _user(v.user_id)
        from .inferred.validation import ValidationError, record_vote
        try:
            return {"ok": True, "insider": record_vote(store, config, v.user_id, inferred_id, v.vote, v.note)}
        except ValidationError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @app.post("/inferred/{inferred_id}/field-notes")
    def post_field_note(inferred_id: int, n: FieldNote) -> dict:
        _user(n.user_id)
        from .inferred.validation import ValidationError, add_field_note
        try:
            add_field_note(store, config, n.user_id, inferred_id, n.note, n.share)
        except ValidationError as exc:
            raise HTTPException(status_code=422 if "permission" in str(exc) else 404, detail=str(exc)) from None
        return {"ok": True}

    @app.post("/mode")
    def post_mode(m: ModeChange) -> dict:
        """Section 6.7: committed narrows the feed to one problem; exploring is the default."""
        _user(m.user_id)
        from .db import ProblemRow, UserRow
        with store.session() as s, s.begin():
            if m.mode == "committed":
                problem = s.get(ProblemRow, m.problem_id) if m.problem_id is not None else None
                if problem is None or problem.status != "active":
                    raise HTTPException(status_code=422, detail="committed mode needs an active problem_id")
            user = s.get(UserRow, m.user_id)
            user.mode = m.mode
            user.committed_problem_id = m.problem_id if m.mode == "committed" else None
        return {"ok": True, "mode": m.mode}

    @app.get("/profile/niches/pending")
    def get_profile_pending(user_id: str) -> dict:
        _user(user_id)
        return {"updates": profiles.pending_updates(store, config, user_id)}

    @app.post("/profile/niches/approve")
    def post_profile_approve(d: Decisions) -> dict:
        """Approve (approve=true) or decline (approve=false) pending updates."""
        _user(d.user_id)
        try:
            return {"decided": profiles.decide(store, d.user_id, [(x.update_id, x.approve) for x in d.decisions])}
        except profiles.ProfileError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @app.get("/profile/niches")
    def get_profile_niches(user_id: str, audience: Literal["public", "connection", "self"] = "public") -> dict:
        _user(user_id)
        return profiles.profile_niches(store, user_id, audience)

    @app.post("/profile/niches/unpublish")
    def post_profile_unpublish(a: NodeAction) -> dict:
        _user(a.user_id)
        if not profiles.unpublish(store, a.user_id, a.node_id):
            raise HTTPException(status_code=404, detail="not on the profile")
        return {"ok": True}

    @app.post("/profile/auto-publish")
    def post_auto_publish(a: AutoPublish) -> dict:
        _user(a.user_id)
        profiles.set_auto_publish(store, a.user_id, a.on)
        return {"ok": True}

    @app.post("/events")
    def post_events(batch: EventBatch) -> BatchResult:
        try:
            return log_events(batch, store, posts, config,
                              on_stored=lambda rows: apply_events(store, posts, config, rows))
        except UnknownUserError:
            raise HTTPException(status_code=404, detail="unknown user") from None
        except BatchTooLargeError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from None

    return app


def app_from_env() -> FastAPI:
    """Factory for uvicorn: `uvicorn signal_feed.api:app_from_env --factory`."""
    settings = FeedSettings.from_env()
    return create_app(FeedStore(settings.feed_database_url), PostsReader(settings.posts_database_url),
                      load_feed_config(settings.config_file), clips_db_url=settings.posts_database_url)
