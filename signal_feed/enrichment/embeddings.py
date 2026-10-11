"""Embeddings through Voyage AI's REST API (Anthropic has no embeddings endpoint).

Called with httpx directly: the voyageai package pulls in ~30 dependencies for one POST.
"""

from __future__ import annotations

import os
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from ..config import EmbeddingsConfig

VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
CHARS_PER_TOKEN = 3  # conservative estimate for pacing; Voyage averages closer to 4 for English


class EmbeddingError(RuntimeError):
    pass


@dataclass
class EmbedResult:
    vectors: list[list[float]]
    total_tokens: int
    model: str


class Embedder(Protocol):
    def embed(self, texts: Sequence[str], input_type: str = "document") -> EmbedResult: ...


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return isinstance(exc, httpx.TransportError)


class VoyageEmbedder:
    def __init__(self, cfg: EmbeddingsConfig, api_key: str | None = None,
                 transport: httpx.BaseTransport | None = None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> None:
        key = api_key or os.environ.get("VOYAGE_API_KEY")
        if not key:
            raise EmbeddingError("VOYAGE_API_KEY is not set")
        self.cfg = cfg
        self.client = httpx.Client(timeout=60, transport=transport,
                                   headers={"Authorization": f"Bearer {key}"})
        self.clock, self.sleep = clock, sleep
        self._sent: deque[tuple[float, int]] = deque()  # (time, estimated tokens) in the last minute

    @retry(retry=retry_if_exception(_retryable), stop=stop_after_attempt(5),
           wait=wait_exponential_jitter(initial=1, max=30), reraise=True)
    def _post(self, texts: Sequence[str], input_type: str = "document") -> dict:
        response = self.client.post(VOYAGE_URL, json={
            "input": list(texts), "model": self.cfg.model, "input_type": input_type,
            "output_dimension": self.cfg.dimensions, "truncation": True})
        response.raise_for_status()
        return response.json()

    def _requests(self, texts: Sequence[str]) -> list[list[str]]:
        """Split texts so no request is estimated above the per-minute token limit."""
        cap = self.cfg.tokens_per_minute
        groups: list[list[str]] = [[]]
        used = 0
        for text in texts:
            est = len(text) // CHARS_PER_TOKEN + 1
            if groups[-1] and cap and used + est > cap:
                groups.append([])
                used = 0
            groups[-1].append(text)
            used += est
        return groups

    def _pace(self, tokens: int) -> None:
        rpm, tpm = self.cfg.requests_per_minute, self.cfg.tokens_per_minute
        while True:
            now = self.clock()
            while self._sent and now - self._sent[0][0] >= 60:
                self._sent.popleft()
            over_rpm = rpm is not None and len(self._sent) >= rpm
            over_tpm = tpm is not None and self._sent and sum(t for _, t in self._sent) + tokens > tpm
            if not (over_rpm or over_tpm):
                self._sent.append((now, tokens))
                return
            self.sleep(60 - (now - self._sent[0][0]) + 0.5)

    def embed(self, texts: Sequence[str], input_type: str = "document") -> EmbedResult:
        """`input_type` "document" for posts; "query" for short descriptions matched against posts."""
        vectors: list[list[float]] = []
        total, model = 0, self.cfg.model
        for group in self._requests(texts):
            self._pace(sum(len(t) // CHARS_PER_TOKEN + 1 for t in group))
            try:
                data = self._post(group, input_type)
            except httpx.HTTPError as exc:
                raise EmbeddingError(f"Voyage request failed: {exc}") from exc
            rows = sorted(data["data"], key=lambda d: d["index"])
            vectors += [r["embedding"] for r in rows]
            total += int(data["usage"]["total_tokens"])
            model = data["model"]
        return EmbedResult(vectors, total, model)
