"""A wall clock for the calculations that run *in this process* — the half `run_isolated` cannot do.

Every wall clock on this server used to be a subprocess timeout, and the shipped image pins
`CHEMCLAW_XTB_ENGINE=tblite` — so the paths it actually takes for `opt` and `hess` had none. What
bounded them was `xtb_opt_max_steps`, which bounds *iterations*: one iteration on a large
substrate is unbounded in seconds.

**The manifest's `request_timeout` does not close that, and cannot.** It bounds the caller's wait.
Every tool body here runs its work in `asyncio.to_thread`, and cancelling the awaiting coroutine
does not stop the worker thread — so a caller that has given up leaves the CPU burning, and because
`cached_compute` is check-then-act, its retry starts a second identical burn beside the first. That
is the same argument this repository's `CLAUDE.md` makes against putting a timeout in the transport,
and the same reason `xtb_cli.run_isolated` kills a whole process group rather than trusting
`subprocess.run(timeout=...)`: the control has to sit where the cost is.

So a `Deadline` is threaded into the two in-process loops and checked between units of work — per
gradient in the optimizer, per displacement in the finite-difference Hessian. Between rather than
inside, because a single SCF is not interruptible: the granularity of the clock is one single point,
which on any system this server accepts is seconds rather than minutes.

**Why a `ValueError`.** `mcp_server_kit.connector_app` passes that family to the model verbatim and
replaces everything else with a generic notice. This refusal is the same statement as the atom cap —
*this calculation is too expensive to run inside a turn* — only reached late, and it is equally
actionable: run a smaller system, or configure a deployment that waits longer. `CliError` is
deliberately not reused: it exists to carry a subprocess's stderr tail, which is internal state.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

__all__ = ["Deadline"]


@dataclass(frozen=True)
class Deadline:
    """A budget in seconds, started when it is constructed.

    `monotonic` rather than wall time, because a clock adjustment must not lengthen or shorten a
    calculation's budget.
    """

    seconds: float
    started: float = field(default_factory=time.monotonic)

    @property
    def elapsed(self) -> float:
        """Seconds since this budget started."""
        return time.monotonic() - self.started

    def check(self, what: str) -> None:
        """Raise if the budget is spent, naming the calculation and both numbers.

        Args:
            what: The calculation in progress, phrased to complete "a <what> exceeded …" — the
                caller's only lever is its size, so the message has to say what was running.

        Raises:
            ValueError: the budget is spent. Worded for the model, which is what receives it.
        """
        if self.elapsed <= self.seconds:
            return
        raise ValueError(
            f"a {what} exceeded this server's inline budget of {self.seconds:g}s (spent "
            f"{self.elapsed:.1f}s). This calculation runs inside a conversation turn and nothing "
            "here is cached, so it is stopped rather than left burning CPU for an answer the "
            "caller has already stopped waiting for: run a smaller system, relax it first, or "
            "raise CHEMCLAW_XTB_INLINE_TIMEOUT_SECONDS on a deployment that waits longer"
        )
