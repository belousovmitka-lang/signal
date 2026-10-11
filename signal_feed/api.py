"""HTTP API (FEED_ALGORITHM.md section 12): onboarding, the feed and events.

No authentication yet: the user comes from the request until the app's auth exists.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from .config import FeedConfig, load_feed_config
from .db import FeedStore
from .enrichment.schema import niche_seeds, skill_vocabulary
from .events import BatchResult, BatchTooLargeError, EventBatch, UnknownUserError, log_events
from .feed.data import FeedDataCache
from .feed.service import FeedPage, FeedService
from .feed.usermodel import UnknownNodeError, apply_events, onboard
from .niches.tree import seed_labels
from .posts import PostsReader
from .settings import FeedSettings


class Quiz(BaseModel):
    """Industries may name a branch: "Healthcare > Pharmacy"."""

    industries: list[str] = Field(min_length=1, max_length=20)
    skills: list[str] = Field(default_factory=list, max_length=20)
    resume_skills: list[str] = Field(default_factory=list, max_length=50)


class Onboarded(BaseModel):
    user_id: str


def create_app(store: FeedStore, posts: PostsReader, config: FeedConfig, service: FeedService | None = None) -> FastAPI:
    app = FastAPI(title="Signal feed")
    service = service or FeedService(store, config, FeedDataCache(store, posts))

    @app.get("/onboarding/options")
    def onboarding_options() -> dict:
        seeds = niche_seeds()
        return {"industries": [{"name": n, "branches": [b for b, _ in seeds.get(n, ())]}
                               for n in seed_labels()["industry"]],
                "skills": [{"name": n, "covers": c} for n, c in skill_vocabulary()]}

    @app.post("/onboarding")
    def post_onboarding(quiz: Quiz) -> Onboarded:
        try:
            return Onboarded(user_id=onboard(store, config, quiz.industries, quiz.skills, quiz.resume_skills))
        except UnknownNodeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @app.get("/feed")
    def get_feed(user_id: str, session_id: str = Query(min_length=1, max_length=64),
                 cursor: str | None = None, explore: bool = False) -> FeedPage:
        # `cursor` is accepted for API compatibility: the session's own impressions say what was shown.
        if not store.user_exists(user_id):
            raise HTTPException(status_code=404, detail="unknown user")
        return service.page(user_id, session_id, explore_more=explore)

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
                      load_feed_config(settings.config_file))
