"""HTTP API (FEED_ALGORITHM.md section 12): sign-in, onboarding, the feed and events.

With an `Auth` (signal_feed/auth.py; `app_from_env` always has one unless SIGNAL_AUTH=off), every user-scoped
route needs a session and acts only as its own user: a `user_id` that isn't the session's is refused (403).
Without one (tests, local experiments) routes trust the `user_id` they are given.
"""

from __future__ import annotations

import os
import json
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from sqlalchemy import select

from signal_connectors.clips import Clip, ClipIn

from .auth import COOKIE, TOKEN_RE, Account, Auth, AuthError, auth_from_env
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


class SignInStart(BaseModel):
    email: str = Field(min_length=3, max_length=254)


class SignInVerify(BaseModel):
    token: str = Field(min_length=1, max_length=128)


class SignedInOut(BaseModel):
    session_token: str
    expires_at: str
    user_id: str | None            # None until onboarding


def _session_token(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.cookies.get(COOKIE)


def signed_in(request: Request) -> Account | None:
    """The session's account; None when the app runs without sign-in."""
    auth: Auth | None = request.app.state.auth
    if auth is None:
        return None
    account = auth.session(_session_token(request))
    if account is None:
        raise HTTPException(status_code=401, detail="sign in first")
    return account


Me = Annotated[Account | None, Depends(signed_in)]


CALLBACK_PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Sign in to Signal</title>
<style>body{font:16px system-ui;max-width:420px;margin:15vh auto;padding:0 16px;text-align:center}
button{font:inherit;padding:10px 28px;border-radius:8px;border:0;background:#222;color:#fff;cursor:pointer}</style>
<h1>Sign in to Signal</h1><p id=msg></p><button id=go>Sign in</button>
<script>
const token = %s;
document.getElementById("go").onclick = async (e) => {
  e.target.disabled = true;
  const r = await fetch("/auth/verify", {method: "POST", credentials: "same-origin",
    headers: {"Content-Type": "application/json"}, body: JSON.stringify({token})});
  document.getElementById("msg").textContent = r.ok ? "You're signed in. You can close this tab."
    : "This link has expired or was already used. Ask for a new one.";
  e.target.hidden = true;
};
</script>"""


def create_app(store: FeedStore, posts: PostsReader, config: FeedConfig, service: FeedService | None = None,
               clips_db_url: str | None = None, auth: Auth | None = None) -> FastAPI:
    app = FastAPI(title="Signal feed")
    app.state.auth = auth

    def _me(user_id: str, me: Account | None) -> None:
        if me is not None and me.user_id != user_id:
            raise HTTPException(status_code=403, detail="not your user")

    def _user(user_id: str, me: Account | None) -> None:
        _me(user_id, me)
        if not store.user_exists(user_id):
            raise HTTPException(status_code=404, detail="unknown user")

    if clips_db_url:      # "Clip to Signal" button: the connectors' per-user clips, behind the same sign-in
        _clip_routes(app, clips_db_url, _me)
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
    def post_onboarding(quiz: Quiz, me: Me) -> Onboarded:
        if me is not None and me.user_id is not None:
            raise HTTPException(status_code=409, detail="already onboarded")
        try:
            user_id = onboard(store, config, quiz.industries, quiz.skills, quiz.resume_skills, works_in=quiz.works_in)
        except UnknownNodeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        if me is not None:
            auth.bind_user(me.id, user_id)
        return Onboarded(user_id=user_id)

    @app.get("/feed")
    def get_feed(user_id: str, me: Me, session_id: str = Query(min_length=1, max_length=64),
                 cursor: str | None = None, explore: bool = False) -> FeedPage:
        # `cursor` is accepted for API compatibility: the session's own impressions say what was shown.
        _user(user_id, me)
        return service.page(user_id, session_id, explore_more=explore)

    def _act(fn, *args, **kw) -> dict:
        try:
            fn(store, config, *args, **kw)
        except usermap.MapError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        return {"ok": True}

    @app.get("/map")
    def get_map(user_id: str, me: Me) -> dict:
        _user(user_id, me)
        return usermap.public_view(store, config, user_id)

    @app.get("/map/history")
    def get_map_history(user_id: str, me: Me) -> dict:
        """Weekly snapshots for Replay. The map is silent: no change log, no statuses."""
        _user(user_id, me)
        keep = ("node_id", "label", "path", "ring", "radius", "angle")
        with store.session() as s:
            snaps = list(s.scalars(select(UserMapSnapshotRow).where(UserMapSnapshotRow.user_id == user_id)
                                   .order_by(UserMapSnapshotRow.week)))
        return {"snapshots": [{"week": x.week.isoformat(),
                               "nodes": [{k: n[k] for k in keep if k in n} for n in x.data.get("nodes", [])]}
                              for x in snaps]}

    @app.get("/map/node/{node_id}")
    def get_map_node(node_id: int, user_id: str, me: Me) -> dict:
        _user(user_id, me)
        try:
            return usermap.node_detail(store, config, user_id, node_id)
        except usermap.MapError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @app.post("/map/pin")
    def post_pin(a: PinAction, me: Me) -> dict:
        _user(a.user_id, me)
        return _act(usermap.pin, a.user_id, a.node_id, a.radius, a.angle)

    @app.post("/map/unpin")
    def post_unpin(a: NodeAction, me: Me) -> dict:
        _user(a.user_id, me)
        return _act(usermap.unpin, a.user_id, a.node_id)

    @app.post("/map/drop-in")
    def post_drop_in(a: NodeAction, me: Me) -> dict:
        _user(a.user_id, me)
        return _act(usermap.drop_in, a.user_id, a.node_id)

    @app.post("/map/remove")
    def post_remove(a: RemoveAction, me: Me) -> dict:
        _user(a.user_id, me)
        return _act(usermap.remove, a.user_id, a.node_id, a.allow_retest)

    @app.post("/map/nudge")
    def post_nudge(a: NudgeAction, me: Me) -> dict:
        _user(a.user_id, me)
        return _act(usermap.nudge, a.user_id, a.node_id, a.direction)

    @app.post("/map/reset")
    def post_reset(a: UserAction, me: Me) -> dict:
        _user(a.user_id, me)
        return _act(usermap.reset, a.user_id)

    @app.get("/inferred/prompt")
    def get_insider_prompt(user_id: str, me: Me) -> dict:
        """17.7: at most one question a week for insiders: "Is this real where you work?"."""
        _user(user_id, me)
        from .inferred.validation import next_prompt
        return {"prompt": next_prompt(store, config, user_id)}

    @app.get("/inferred/{inferred_id}")
    def get_inferred(inferred_id: int, user_id: str, me: Me) -> dict:
        """The full evidence chain ("+N more", 17.8) and what was found against it."""
        _user(user_id, me)
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
    def post_vote(inferred_id: int, v: Vote, me: Me) -> dict:
        _user(v.user_id, me)
        from .inferred.validation import ValidationError, record_vote
        try:
            return {"ok": True, "insider": record_vote(store, config, v.user_id, inferred_id, v.vote, v.note)}
        except ValidationError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @app.post("/inferred/{inferred_id}/field-notes")
    def post_field_note(inferred_id: int, n: FieldNote, me: Me) -> dict:
        _user(n.user_id, me)
        from .inferred.validation import ValidationError, add_field_note
        try:
            add_field_note(store, config, n.user_id, inferred_id, n.note, n.share)
        except ValidationError as exc:
            raise HTTPException(status_code=422 if "permission" in str(exc) else 404, detail=str(exc)) from None
        return {"ok": True}

    @app.post("/mode")
    def post_mode(m: ModeChange, me: Me) -> dict:
        """Section 6.7: committed narrows the feed to one problem; exploring is the default."""
        _user(m.user_id, me)
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
    def get_profile_pending(user_id: str, me: Me) -> dict:
        _user(user_id, me)
        return {"updates": profiles.pending_updates(store, config, user_id)}

    @app.post("/profile/niches/approve")
    def post_profile_approve(d: Decisions, me: Me) -> dict:
        """Approve (approve=true) or decline (approve=false) pending updates."""
        _user(d.user_id, me)
        try:
            return {"decided": profiles.decide(store, d.user_id, [(x.update_id, x.approve) for x in d.decisions])}
        except profiles.ProfileError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @app.get("/profile/niches")
    def get_profile_niches(user_id: str, me: Me,
                           audience: Literal["public", "connection", "self"] = "public") -> dict:
        _user(user_id, me)
        return profiles.profile_niches(store, user_id, audience)

    @app.post("/profile/niches/unpublish")
    def post_profile_unpublish(a: NodeAction, me: Me) -> dict:
        _user(a.user_id, me)
        if not profiles.unpublish(store, a.user_id, a.node_id):
            raise HTTPException(status_code=404, detail="not on the profile")
        return {"ok": True}

    @app.post("/profile/auto-publish")
    def post_auto_publish(a: AutoPublish, me: Me) -> dict:
        _user(a.user_id, me)
        profiles.set_auto_publish(store, a.user_id, a.on)
        return {"ok": True}

    @app.post("/events")
    def post_events(batch: EventBatch, me: Me) -> BatchResult:
        _me(batch.user_id, me)
        try:
            return log_events(batch, store, posts, config,
                              on_stored=lambda rows: apply_events(store, posts, config, rows))
        except UnknownUserError:
            raise HTTPException(status_code=404, detail="unknown user") from None
        except BatchTooLargeError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from None

    if auth is not None:
        @app.post("/auth/start")
        def post_auth_start(body: SignInStart) -> dict:
            """Email a sign-in link. The same answer whether or not the address has an account."""
            try:
                auth.start(body.email)
            except AuthError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from None
            except OSError:
                raise HTTPException(status_code=503, detail="couldn't send the email; try again") from None
            return {"ok": True}

        @app.get("/auth/callback", response_class=HTMLResponse)
        def get_auth_callback(token: str = "") -> str:
            """The emailed link: a page with a button, so a mail scanner opening the link doesn't use it."""
            if not TOKEN_RE.match(token):
                raise HTTPException(status_code=400, detail="invalid link")
            return CALLBACK_PAGE % json.dumps(token)

        @app.post("/auth/verify")
        def post_auth_verify(body: SignInVerify, response: Response) -> SignedInOut:
            try:
                s = auth.verify(body.token)
            except AuthError as exc:
                raise HTTPException(status_code=401, detail=str(exc)) from None
            response.set_cookie(COOKIE, s.session_token, max_age=config.auth.session_days * 86400, httponly=True,
                                samesite="lax", secure=auth.secure_cookie)
            return SignedInOut(session_token=s.session_token, expires_at=s.expires_at.isoformat(),
                               user_id=s.account.user_id)

        @app.get("/auth/me")
        def get_auth_me(me: Me) -> dict:
            return {"email": me.email, "user_id": me.user_id}

        @app.post("/auth/sign-out")
        def post_sign_out(request: Request, response: Response) -> dict:
            auth.sign_out(_session_token(request))
            response.delete_cookie(COOKIE)
            return {"ok": True}

    return app


def _clip_routes(app: FastAPI, clips_db_url: str, _me) -> None:
    """The connectors' clip routes (signal_connectors.clips), with the feed's sign-in in front."""
    from signal_connectors.clips import delete_clip, list_clips, save_clip
    from signal_connectors.store import Store

    clips = Store(clips_db_url)

    @app.post("/clips")
    def post_clip(clip: ClipIn, me: Me) -> Clip:
        _me(clip.user_id, me)
        return save_clip(clips, clip)

    @app.get("/clips")
    def get_clips(me: Me, user_id: str = Query(min_length=1, max_length=64),
                  limit: int = Query(200, ge=1, le=1000)) -> list[Clip]:
        _me(user_id, me)
        return list_clips(clips, user_id, limit)

    @app.delete("/clips/{clip_id}")
    def remove_clip(clip_id: str, me: Me, user_id: str = Query(min_length=1, max_length=64)) -> dict:
        _me(user_id, me)
        if not delete_clip(clips, user_id, clip_id):
            raise HTTPException(status_code=404, detail="unknown clip")
        return {"ok": True}


def app_from_env() -> FastAPI:
    """Factory for uvicorn: `uvicorn signal_feed.api:app_from_env --factory`."""
    settings = FeedSettings.from_env()
    store, config = FeedStore(settings.feed_database_url), load_feed_config(settings.config_file)
    return create_app(store, PostsReader(settings.posts_database_url), config,
                      clips_db_url=settings.posts_database_url, auth=auth_from_env(store, config))
