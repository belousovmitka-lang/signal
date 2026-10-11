"""Claude calls for tagging: one structured-output request per post, sync or via the Batch API.

Responses are validated here rather than with `messages.parse()`, so usage (and cost) is known
even when the JSON fails validation. Tests swap `AnthropicTagger` for a fake with the same shape.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import cache
from typing import Any, Protocol

import anthropic
from pydantic import BaseModel, ValidationError

from ..config import ModelCall
from .costs import Usage
from .schema import tag_output_model

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class TransientError(RuntimeError):
    """Rate limits, overloads, network trouble after the SDK's retries: try the post again next run."""


@dataclass
class TagAttempt:
    output: BaseModel | None
    model: str                       # the model that produced the final message
    usages: list[Usage] = field(default_factory=list)   # one per model that ran (fallbacks add more)
    error: str | None = None         # invalid_json | refusal | max_tokens | bad_request
    request_id: str | None = None


class Tagger(Protocol):
    def tag(self, call: ModelCall, system: str, user: str, *, fallbacks: bool = False) -> TagAttempt: ...


@cache
def output_schema() -> dict[str, Any]:
    return anthropic.transform_schema(tag_output_model())


def request_params(call: ModelCall, system: str, user: str) -> dict[str, Any]:
    """Messages API parameters for one post (also the `params` of a batch request)."""
    return {
        "model": call.model,
        "max_tokens": call.max_tokens,
        # Fixed per prompt_version: cached across posts once it is long enough to qualify.
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user}],
        "output_config": {"effort": call.effort, "format": {"type": "json_schema", "schema": output_schema()}},
    }


def _usage_rows(message: Any) -> list[Usage]:
    usage = message.usage
    iterations = getattr(usage, "iterations", None)
    if iterations:
        return [Usage(it.model, it.input_tokens, it.output_tokens, it.cache_read_input_tokens or 0,
                      it.cache_creation_input_tokens or 0)
                for it in iterations if getattr(it, "model", None)]
    return [Usage(message.model, usage.input_tokens, usage.output_tokens, usage.cache_read_input_tokens or 0,
                  usage.cache_creation_input_tokens or 0)]


def read_message(message: Any, request_id: str | None = None) -> TagAttempt:
    """Turn a Messages API response (sync or from a batch) into a validated TagAttempt."""
    attempt = TagAttempt(output=None, model=message.model, usages=_usage_rows(message), request_id=request_id)
    if message.stop_reason == "refusal":
        attempt.error = "refusal"
        return attempt
    if message.stop_reason == "max_tokens":
        attempt.error = "max_tokens"
        return attempt
    text = next((b.text for b in message.content if b.type == "text"), None)
    try:
        attempt.output = tag_output_model().model_validate(json.loads(text or ""))
    except (json.JSONDecodeError, ValidationError):
        attempt.error = "invalid_json"
    return attempt


class AnthropicTagger:
    def __init__(self, client: anthropic.Anthropic | None = None) -> None:
        self.client = client or anthropic.Anthropic(max_retries=4)

    def tag(self, call: ModelCall, system: str, user: str, *, fallbacks: bool = False) -> TagAttempt:
        params = request_params(call, system, user)
        try:
            if fallbacks:
                message = self.client.beta.messages.create(**params, betas=[FALLBACK_BETA], fallbacks="default")
            else:
                message = self.client.messages.create(**params)
        except anthropic.BadRequestError as exc:
            # Not retryable as-is (e.g. a post the API won't accept): record and move on.
            return TagAttempt(output=None, model=call.model, error="bad_request",
                              request_id=getattr(exc, "request_id", None))
        except (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APIConnectionError) as exc:
            raise TransientError(str(exc)) from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500 or exc.status_code == 429:
                raise TransientError(str(exc)) from exc
            raise
        return read_message(message, message._request_id)
