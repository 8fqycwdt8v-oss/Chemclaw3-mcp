"""A wall clock for the calculations that run *in this process* — the half `run_isolated` cannot do.

The tblite optimiser and finite-difference Hessian run in a worker thread, which cancelling the
caller does not stop, and `xtb_opt_max_steps` bounds iterations, not seconds. So a `Deadline` is
checked between units of work (per gradient, per displacement); a single SCF is not
interruptible. The stop is a `ValueError` so the model can act on it (run a smaller system).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from chemclaw_mcp_calc.engine.metrics import INLINE_BUDGET_EXCEEDED

__all__ = ["TIME_BUDGET_MARKER", "Deadline", "TimeBudgetError"]

logger = logging.getLogger(__name__)

# "Stopped by this pod's clock", not "bad input": a stop depends on load, so a caller must not
# report it as a property of the molecule. Placed at the head of the message like
# `admission.AT_CAPACITY_MARKER`; Chemclaw3 transcribes it as `core/mcp_session.SERVER_TIME_BUDGET`.
TIME_BUDGET_MARKER = "[calc-time-budget]"


class TimeBudgetError(ValueError):
    """The inline wall clock stopped this calculation; the input itself was not refused.

    A `ValueError` so `connector_app` passes it verbatim; a subclass so it can be caught precisely.
    """


@dataclass(frozen=True)
class Deadline:
    """A budget in seconds, started when it is constructed, on the monotonic clock."""

    seconds: float
    started: float = field(default_factory=time.monotonic)

    @property
    def elapsed(self) -> float:
        """Seconds since this budget started."""
        return time.monotonic() - self.started

    def check(self, what: str, progress: Callable[[], str] | None = None) -> None:
        """Raise if the budget is spent, naming the calculation, both numbers and how far it got.

        Logged at WARNING and counted before raising, because an undersized budget is a deployment
        problem an operator must see, not only the model.

        Args:
            what: The calculation in progress, completing "a <what> exceeded …". Also a metric
            label, so
                it must be a literal at the call site, never caller-derived.
            progress: How far the loop got, completing "stopped after"; called only once the budget
            is
                spent, so a caller can tell nearly-converged from hopeless.

        Raises:
            TimeBudgetError: the budget is spent; led by `TIME_BUDGET_MARKER`.
        """
        if self.elapsed <= self.seconds:
            return
        spent = self.elapsed
        reached = f"; stopped after {progress()}" if progress is not None else ""
        INLINE_BUDGET_EXCEEDED.labels(what).inc()
        logger.warning(
            "inline budget exceeded: a %s spent %.1fs of a %gs budget and was stopped%s",
            what,
            spent,
            self.seconds,
            reached,
        )
        raise TimeBudgetError(
            f"{TIME_BUDGET_MARKER} "
            f"a {what} exceeded this server's inline budget of {self.seconds:g}s (spent "
            f"{spent:.1f}s{reached}). This calculation runs inside a conversation turn and nothing "
            "here is cached, so it is stopped rather than left burning CPU for an answer the "
            "caller has already stopped waiting for: run a smaller system, relax it first, or "
            "raise CHEMCLAW_XTB_INLINE_TIMEOUT_SECONDS on a deployment that waits longer"
        )
