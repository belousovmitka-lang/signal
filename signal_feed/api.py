"""HTTP API (FEED_ALGORITHM.md section 12). Phase 0: POST /events.

No authentication yet: the user comes from the request body until the app's auth exists.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from .config import FeedConfig, load_feed_config
from .db import FeedStore
from .events import BatchResult, BatchTooLargeError, EventBatch, UnknownUserError, log_events
from .posts import PostsReader
from .settings import FeedSettings


def create_app(store: FeedStore, posts: PostsReader, config: FeedConfig) -> FastAPI:
    app = FastAPI(title="Signal feed")

    @app.post("/events")
    def post_events(batch: EventBatch) -> BatchResult:
        try:
            return log_events(batch, store, posts, config)
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
