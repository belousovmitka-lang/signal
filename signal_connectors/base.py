"""Connector protocol, shared HTTP client and per-domain rate limiter."""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from .models import Cadence, HealthStatus, RawPost, Route, SourceQuery, SourceType

if TYPE_CHECKING:
    from .config import SourceConfig
    from .services import Services

log = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


def domain_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


class RateLimiter:
    """Spaces requests to each domain at least 1/rps seconds apart."""

    def __init__(self, default_rps: float = 1.0) -> None:
        self.default_rps = default_rps
        self._rates: dict[str, float] = {}
        self._next: dict[str, float] = defaultdict(float)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def set_rate(self, domain: str, rps: float) -> None:
        self._rates[domain.lower()] = rps

    def rate(self, domain: str) -> float:
        return self._rates.get(domain.lower(), self.default_rps)

    def defer(self, domain: str, seconds: float) -> None:
        """Push the next slot for a domain back, e.g. after a 429 with Retry-After."""
        self._next[domain] = max(self._next[domain], time.monotonic() + seconds)

    async def acquire(self, domain: str) -> None:
        async with self._locks[domain]:
            now = time.monotonic()
            start = max(now, self._next[domain])
            if start > now:
                await asyncio.sleep(start - now)
            self._next[domain] = start + 1.0 / self.rate(domain)


class RetryableResponse(Exception):
    def __init__(self, response: httpx.Response) -> None:
        super().__init__(f"HTTP {response.status_code} from {response.request.url}")
        self.response = response


def retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


class HttpClient:
    """One shared httpx.AsyncClient with per-domain rate limits, retries with jitter and a fixed User-Agent.

    Retries on 429, 5xx and transport errors with exponential backoff, honoring Retry-After.
    After the last attempt the final response is returned (callers decide whether to raise);
    a transport error after the last attempt is raised.
    """

    def __init__(
        self,
        *,
        user_agent: str,
        default_rps: float = 1.0,
        timeout: float = 20.0,
        max_attempts: int = 5,
        backoff_initial: float = 1.0,
        backoff_max: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.limiter = RateLimiter(default_rps)
        self.max_attempts = max_attempts
        self.backoff_max = backoff_max
        self._backoff = wait_exponential_jitter(multiplier=backoff_initial, jitter=backoff_initial, max=backoff_max)
        self._client = httpx.AsyncClient(
            headers={"User-Agent": user_agent},
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )

    async def __aenter__(self) -> HttpClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _wait(self, state: RetryCallState) -> float:
        exc = state.outcome.exception() if state.outcome else None
        if isinstance(exc, RetryableResponse):
            after = retry_after_seconds(exc.response)
            if after is not None:
                return min(after, self.backoff_max)
        return self._backoff(state)

    async def request(self, method: str, url: str, *, attempts: int | None = None, **kwargs: Any) -> httpx.Response:
        """`attempts` overrides max_attempts for APIs that penalize retries (GDELT)."""
        domain = domain_of(url)
        retrying = AsyncRetrying(
            stop=stop_after_attempt(attempts or self.max_attempts),
            wait=self._wait,
            retry=retry_if_exception_type((RetryableResponse, httpx.TransportError)),
            reraise=True,
        )
        try:
            async for attempt in retrying:
                with attempt:
                    await self.limiter.acquire(domain)
                    response = await self._client.request(method, url, **kwargs)
                    if response.status_code in RETRY_STATUSES:
                        after = retry_after_seconds(response)
                        if after:
                            self.limiter.defer(domain, min(after, self.backoff_max))
                        log.warning("%s %s -> %s (attempt %d)", method, url.split("?")[0], response.status_code,
                                    attempt.retry_state.attempt_number)
                        raise RetryableResponse(response)
                    return response
        except RetryableResponse as exc:
            return exc.response
        raise AssertionError("unreachable")

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        response = await self.get(url, **kwargs)
        response.raise_for_status()
        return response.json()

    async def get_text(self, url: str, **kwargs: Any) -> str:
        response = await self.get(url, **kwargs)
        response.raise_for_status()
        return response.text


@runtime_checkable
class Connector(Protocol):
    name: str
    route: str
    cadence: str

    def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]: ...

    async def healthcheck(self) -> HealthStatus: ...


class BaseConnector(ABC):
    """Shared plumbing for connectors.

    Subclasses set `connector` (the registry key), `route` and `source_type`, and implement
    `fetch` as an async generator. One instance serves one entry of sources.yaml; its `name`
    is that entry's key and is what costs, runs and RawPost.source are recorded under.
    """

    connector: ClassVar[str]
    route: ClassVar[Route]
    source_type: ClassVar[SourceType]
    default_cadence: ClassVar[Cadence] = "daily"
    # True when the source only gives dates (no time of day): the runner then compares dates, so
    # items from the cursor's own day aren't dropped for being stamped at midnight.
    day_granular: ClassVar[bool] = False
    # False for structured records (complaints, tenders, rules) whose text repeats across distinct
    # records: they dedupe on id only, never on text hash.
    text_dedupe: ClassVar[bool] = True
    # False when the connector deliberately reads before `since` (sources that publish late, like
    # openFDA's MAUDE): the runner then doesn't drop items older than the cursor.
    filter_since: ClassVar[bool] = True
    # >1 when queries hit different sites (one feed per query): the runner runs that many at once.
    # Per-domain rate limits still apply.
    parallel_queries: ClassVar[int] = 1

    def __init__(self, source: SourceConfig, services: Services) -> None:
        self.source = source
        self.services = services
        self.http = services.http
        self.name = source.name
        self.cadence = source.cadence or self.default_cadence
        self.invalid = 0

    @abstractmethod
    def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]: ...

    async def healthcheck(self) -> HealthStatus:
        costs = self.services.costs
        quota_left = None
        if self.source.quota:
            q = self.source.quota
            quota_left = q.units - costs.units_used(self.name, q.window)
        return HealthStatus(
            ok=True,
            quota_left=quota_left,
            budget_left_usd=costs.remaining(self.name),
            last_success=self.services.store.last_success(self.name),
        )

    def make_post(self, **fields: Any) -> RawPost | None:
        """Build a RawPost with this connector's source, route and type filled in.

        Returns None (and counts it as invalid) when the item can't form a valid record,
        so one malformed item never stops a run.
        """
        fields.setdefault("source", self.name)
        fields.setdefault("route", self.route)
        fields.setdefault("source_type", self.source.source_type or self.source_type)
        try:
            return RawPost(**fields)
        except (ValidationError, ValueError) as exc:
            self.invalid += 1
            log.warning("%s: skipping invalid item %s: %s", self.name, fields.get("url"), exc)
            return None

    async def fetch_page(self, url: str, **kwargs: Any) -> httpx.Response:
        """GET a public page after checking robots.txt (required for public_pages connectors)."""
        await self.services.robots.require(url)
        return await self.http.get(url, **kwargs)
