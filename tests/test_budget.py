import threading

import pytest

from devagent.budget import Budget, BudgetExceeded


class FakeClock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def make_budget(**kw):
    defaults = dict(max_tokens=1000, max_seconds=60.0, max_retries=2, clock=FakeClock())
    defaults.update(kw)
    return Budget(**defaults)


def test_under_ceilings_does_not_raise():
    b = make_budget()
    b.add_tokens(500)
    b.spend_retry()
    b.tick()  # no raise
    assert b.remaining_tokens() == 500


def test_token_ceiling_raises():
    b = make_budget(max_tokens=100)
    with pytest.raises(BudgetExceeded) as ei:
        b.add_tokens(101)
    assert ei.value.kind == "tokens"


def test_retry_ceiling_raises():
    b = make_budget(max_retries=1)
    b.spend_retry()
    with pytest.raises(BudgetExceeded) as ei:
        b.spend_retry()
    assert ei.value.kind == "retries"


def test_wallclock_ceiling_raises():
    clock = FakeClock(now=0.0)
    b = make_budget(max_seconds=10.0, clock=clock)
    clock.now = 5.0
    b.tick()  # still under
    clock.now = 11.0
    with pytest.raises(BudgetExceeded) as ei:
        b.tick()
    assert ei.value.kind == "seconds"


def test_cost_ceiling_raises():
    b = make_budget(max_cost_usd=10.0)
    b.add_cost(6.0)   # under
    with pytest.raises(BudgetExceeded) as ei:
        b.add_cost(5.0)   # 11.0 > 10.0
    assert ei.value.kind == "cost_usd"


def test_no_cost_ceiling_by_default_never_raises_on_cost():
    b = make_budget()  # max_cost_usd defaults to None
    b.add_cost(9999.0)
    b.tick()  # no raise


def test_budget_is_shared_accumulates_across_calls():
    b = make_budget(max_tokens=300)
    b.add_tokens(100)
    b.add_tokens(100)
    with pytest.raises(BudgetExceeded):
        b.add_tokens(150)


def test_add_tokens_is_thread_safe_under_concurrent_sub_builds():
    # M20: one Budget is shared across concurrent per-service sub-builds; concurrent
    # add_tokens calls must not race and under-count spend.
    n_threads, adds_per_thread = 8, 1000
    b = make_budget(max_tokens=n_threads * adds_per_thread * 10)  # high ceiling, no raises

    def worker():
        for _ in range(adds_per_thread):
            b.add_tokens(1)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert b.tokens == n_threads * adds_per_thread
