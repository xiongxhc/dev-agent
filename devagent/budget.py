"""Token + wall-clock + retry accounting. Hard ceilings, raised in code — never
prompt-enforced. One Budget instance is shared across all phases of a run."""

import time
from dataclasses import dataclass, field
from typing import Callable


class BudgetExceeded(Exception):
    def __init__(self, kind: str, limit: float, value: float):
        self.kind, self.limit, self.value = kind, limit, value
        super().__init__(f"budget exceeded: {kind} {value} > {limit}")


@dataclass
class Budget:
    max_tokens: int
    max_seconds: float
    max_retries: int
    max_cost_usd: float | None = None  # hard $ ceiling on real spend; None = no cost cap
    clock: Callable[[], float] = time.monotonic  # injected for deterministic tests
    tokens: int = 0
    retries: int = 0
    cost_usd: float = 0.0
    started_at: float = field(default=None)

    def __post_init__(self):
        if self.started_at is None:
            self.started_at = self.clock()

    def add_tokens(self, n: int) -> None:
        self.tokens += n
        self.check()

    def spend_retry(self) -> None:
        self.retries += 1
        self.check()

    def add_cost(self, usd: float | None) -> None:
        if usd:
            self.cost_usd += usd
        self.check()

    def tick(self) -> None:
        """Re-check time/retry/token ceilings without changing anything."""
        self.check()

    def elapsed(self) -> float:
        return self.clock() - self.started_at

    def remaining_tokens(self) -> int:
        return max(0, self.max_tokens - self.tokens)

    def check(self) -> None:
        if self.tokens > self.max_tokens:
            raise BudgetExceeded("tokens", self.max_tokens, self.tokens)
        if self.retries > self.max_retries:
            raise BudgetExceeded("retries", self.max_retries, self.retries)
        if self.max_cost_usd is not None and self.cost_usd > self.max_cost_usd:
            raise BudgetExceeded("cost_usd", self.max_cost_usd, self.cost_usd)
        if self.elapsed() > self.max_seconds:
            raise BudgetExceeded("seconds", self.max_seconds, self.elapsed())
