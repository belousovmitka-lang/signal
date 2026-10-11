"""Cost meter and budget guard for the feed's paid calls (Claude tagging, Voyage embeddings).

Before a call: its worst case (estimated input, full max_tokens of output) must fit in what is
left of the month's budget, counting open batches' reservations. After it: actual usage is priced
from config and written to feed_costs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select

from signal_connectors.models import utcnow

from ..config import EnrichmentConfig, ModelCall
from ..db import EnrichmentBatchRow, FeedCostRow, FeedStore

log = logging.getLogger(__name__)

# Conservative: English runs ~4 characters per token, Greek or Cyrillic text closer to 2.
CHARS_PER_TOKEN_WORST = 2.0


class BudgetExceededError(RuntimeError):
    pass


@dataclass(frozen=True)
class Usage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


def month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def claude_usd(usage: Usage, cfg: EnrichmentConfig, *, batch: bool = False) -> float:
    price = cfg.prices_per_mtok.get(usage.model)
    if price is None:
        # A server-side fallback can answer with a model we have no price for: bill at the dearest known.
        log.warning("no price for %s; using the highest configured price", usage.model)
        price = max(cfg.prices_per_mtok.values(), key=lambda p: p.input + p.output)
    usd = (usage.input_tokens * price.input
           + usage.cache_write_tokens * price.input * cfg.cache_write_multiplier
           + usage.cache_read_tokens * price.input * cfg.cache_read_multiplier
           + usage.output_tokens * price.output) / 1_000_000
    return usd * (cfg.batch_discount if batch else 1.0)


def claude_worst_case_usd(call: ModelCall, prompt_chars: int, cfg: EnrichmentConfig, *, batch: bool = False) -> float:
    usage = Usage(call.model, input_tokens=int(prompt_chars / CHARS_PER_TOKEN_WORST) + 1, output_tokens=call.max_tokens)
    return claude_usd(usage, cfg, batch=batch)


class CostLedger:
    def __init__(self, store: FeedStore) -> None:
        self.store = store

    def month_spend(self, provider: str, now: datetime | None = None) -> float:
        """Spent this calendar month (UTC), plus reservations held by open batches."""
        start = month_start(now or utcnow())
        with self.store.session() as s:
            spent = s.scalar(select(func.sum(FeedCostRow.usd)).where(
                FeedCostRow.provider == provider, FeedCostRow.at >= start)) or 0.0
            reserved = 0.0
            if provider == "anthropic":
                reserved = s.scalar(select(func.sum(EnrichmentBatchRow.reserved_usd)).where(
                    EnrichmentBatchRow.status == "open")) or 0.0
        return float(spent) + float(reserved)

    def check(self, provider: str, usd: float, budget: float, now: datetime | None = None) -> None:
        spent = self.month_spend(provider, now)
        if spent + usd > budget:
            raise BudgetExceededError(
                f"{provider}: ${spent:.4f} spent or reserved this month + ${usd:.4f} for this call "
                f"exceeds the ${budget:.2f} budget")

    def record(self, *, provider: str, purpose: str, usage: Usage, usd: float, batch: bool = False,
               post_id: str | None = None, batch_id: str | None = None, request_id: str | None = None,
               at: datetime | None = None) -> None:
        with self.store.session() as s, s.begin():
            s.add(FeedCostRow(
                at=at or utcnow(), provider=provider, purpose=purpose, model=usage.model, batch=batch,
                input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_tokens, cache_write_tokens=usage.cache_write_tokens,
                usd=usd, post_id=post_id, batch_id=batch_id, request_id=request_id))
