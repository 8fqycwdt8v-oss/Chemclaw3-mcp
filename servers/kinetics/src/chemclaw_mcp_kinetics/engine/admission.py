"""How many semi-batch integrations this server runs at once — past the ceiling a caller is refused.

**This server was argued out of a ceiling, and the argument stopped being true**
(`D-2026-09-26-a-cost-the-caller-sets-is-a-cost-that-needs-a-ceiling`). It rested on the
integrator costing 836 µs at its 200-step default. That is still the default, but the step count is
no longer the default's to set: `reactors._steps_for_stability` derives a floor from the caller's
rate constant, and `MAX_INTEGRATION_STEPS` was raised to 200,000 so a realistic stiff dose is
answered rather than refused. Measured on a 1 h dose of 5 mol into 0.1 against a co-reagent at 60,
`k = 2.5` integrates ~194,000 steps in **0.42 s of pure-Python RK4** — five hundred times the figure
the exemption quoted, from an input any caller controls.

**Pure Python holds the GIL**, so offloading to a thread buys latency isolation and no throughput:
the event loop and `/healthz` get their turns at every switch interval, and N admitted integrations
run one at a time. The ceiling below is therefore the same arithmetic `servers/chem` uses for its
depictions: N in-flight calls at the worst legal cost must stay well inside the kubelet probe's
`timeoutSeconds` of 3 — two at 0.42 s is under a third of it. A pod that needs more throughput
needs more replicas, not a wider ceiling.

**Refused rather than queued, and admission rather than a clock.** Cancelling the awaiting
coroutine does not stop the worker thread, so a per-call timeout would answer a caller who has gone
while the CPU kept burning; refusing before any work starts orphans nothing. A `ValueError` is the
family `connector_app` passes to the caller verbatim.

The counter, the clamp and the lock are `mcp_server_kit.limits.Admission`'s; the sentence is this
server's, because the levers it names are.
"""

from __future__ import annotations

from mcp_server_kit.limits import Admission as KitAdmission

__all__ = ["DEFAULT_MAX_CONCURRENT_INTEGRATIONS", "Admission"]

#: Two worst-case integrations at 0.42 s each hold the interpreter for 0.84 s, under a third of the
#: kubelet probe's 3 s budget. See the module docstring.
DEFAULT_MAX_CONCURRENT_INTEGRATIONS = 2


class Admission(KitAdmission):
    """A budget of concurrent semi-batch integrations, refused rather than queued past it."""

    unit = "integration"

    def acquire(self, what: str) -> None:
        """Take one slot, or refuse in terms the caller can act on.

        Args:
            what: The tool being asked for, named in the refusal.

        Raises:
            ValueError: the budget has no room.
        """
        if self.take().charged is None:
            raise ValueError(
                f"this server is already running {self.limit} semi-batch integrations, so "
                f"{what} was refused rather than queued: a stiff dose is up to half a second of "
                "CPU that holds the interpreter, and a queued one would come back after the caller "
                "had stopped waiting. Retry once one finishes. The pod answers more of these by "
                "running more replicas, not by raising "
                "CHEMCLAW_KINETICS_MAX_CONCURRENT_INTEGRATIONS"
            )
