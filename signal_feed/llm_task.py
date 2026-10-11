"""One structured Claude call outside tagging (naming niches, problem merge checks), metered and
budget-checked against the same monthly Claude budget as enrichment."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TypeVar

from pydantic import BaseModel

from signal_connectors.models import utcnow

from .config import EnrichmentConfig
from .db import FeedStore
from .enrichment.costs import CostLedger, claude_usd, claude_worst_case_usd
from .enrichment.llm import Tagger, TransientError

T = TypeVar("T", bound=BaseModel)


class LLMTask:
    def __init__(self, store: FeedStore, cfg: EnrichmentConfig, tagger: Tagger,
                 clock: Callable[[], datetime] = utcnow) -> None:
        self.cfg, self.tagger, self.clock = cfg, tagger, clock
        self.ledger = CostLedger(store)
        self.spent = 0.0

    def ask(self, purpose: str, system: str, user: str, output_model: type[T]) -> T | None:
        """The validated answer, or None when the model declined or returned invalid output.
        Raises BudgetExceededError before a call that could exceed the budget."""
        call = self.cfg.tagger
        worst = claude_worst_case_usd(call, len(system) + len(user), self.cfg)
        self.ledger.check("anthropic", worst, self.cfg.monthly_budget_usd, self.clock())
        try:
            attempt = self.tagger.tag(call, system, user, output_model=output_model)
        except TransientError:
            return None
        for usage in attempt.usages:
            usd = claude_usd(usage, self.cfg)
            self.spent += usd
            self.ledger.record(provider="anthropic", purpose=purpose, usage=usage, usd=usd,
                               request_id=attempt.request_id, at=self.clock())
        return attempt.output if attempt.error is None else None  # type: ignore[return-value]
