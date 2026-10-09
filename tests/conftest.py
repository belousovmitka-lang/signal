from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import vcr

from signal_connectors.config import SourcesFile

from .scrub import scrub_body
from signal_connectors.services import Services
from signal_connectors.settings import Settings
from signal_connectors.store import Store

CASSETTES = Path(__file__).parent / "cassettes"

os.environ.setdefault("AUTHOR_HASH_SALT", "test-salt-not-secret")

SECRET_RESPONSE_HEADERS = {"set-cookie", "authorization", "x-api-key"}


def _scrub_response(response: dict[str, Any]) -> dict[str, Any]:
    headers = response.get("headers", {})
    for name in list(headers):
        values = headers[name] if isinstance(headers[name], list) else [headers[name]]
        # Drop secrets, and headers httpx can't replay (WordPress.org sends "X-Olaf: ⛄").
        if name.lower() in SECRET_RESPONSE_HEADERS or not all(str(v).isascii() for v in values):
            del headers[name]
    return scrub_body(response)   # personal data never reaches a committed cassette


# Recorded-response tests. Re-record with: VCR_RECORD_MODE=once (delete the cassette first).
# Auth headers, key-like query params and personal fields in bodies are scrubbed before
# anything is written to disk (tests/scrub.py).
signal_vcr = vcr.VCR(
    cassette_library_dir=str(CASSETTES),
    record_mode=os.environ.get("VCR_RECORD_MODE", "none"),
    match_on=["method", "scheme", "host", "path", "query"],
    filter_headers=["authorization", "x-api-key", "x-subscription-token", "cookie", "set-cookie"],
    filter_query_parameters=["key", "api_key", "apikey", "token", "access_token", "client_secret", "user_key"],
    decode_compressed_response=True,
    before_record_response=_scrub_response,
)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        contact_email="tests@example.org",
        sources_file=tmp_path / "sources.yaml",
    )


@pytest.fixture
def store(settings: Settings) -> Store:
    return Store(settings.database_url)


Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def make_services(settings: Settings) -> Callable[..., Any]:
    """Open Services against a mock transport: `async with make_services(sources, handler) as svc`."""

    def factory(sources: dict[str, Any] | SourcesFile | None = None, handler: Handler | None = None):
        if not isinstance(sources, SourcesFile):
            sources = SourcesFile.model_validate(sources or {"sources": {}})
        transport = httpx.MockTransport(handler or (lambda r: httpx.Response(404)))
        return Services.open(settings, sources, transport=transport, backoff_initial=0.01)

    return factory


@pytest.fixture(autouse=True)
def _no_pacing_on_replay(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Replayed cassettes hit no server, so skip rate limits and crawl delays.

    Not applied while recording (VCR_RECORD_MODE=once), nor to the tests of the limiter itself.
    """
    if os.environ.get("VCR_RECORD_MODE", "none") != "none":
        return
    if request.module.__name__.rsplit(".", 1)[-1] in {"test_http", "test_robots"}:
        return
    if request.node.get_closest_marker("real_pacing"):
        return
    from signal_connectors.base import RateLimiter

    monkeypatch.setattr(RateLimiter, "rate", lambda self, domain: 1e9)
    monkeypatch.setattr(RateLimiter, "defer", lambda self, domain, seconds: None)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "real_pacing: keep rate limits and backoff deferrals during replay")
