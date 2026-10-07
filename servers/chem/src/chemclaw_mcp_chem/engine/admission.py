"""How many heavy calls this server accepts at once — one past the ceiling is refused, not held.

Every tool in `GATED_TOOLS` spends the one resource this pod has one of: RDKit holds the GIL (or
yields it but still runs serially), so admitted calls run one at a time and the thread pool buys
latency isolation — the event loop and `/healthz` keep answering — but no throughput. This server
scales by replicas, and the refusal says so rather than naming a knob that cannot help.

A single call's cost is bounded by the input bounds in `engine/species.py`, not by this ceiling.
The ceiling is derived from the `asyncio.to_thread` pool: the pool width less one, so every
admitted call has a worker immediately (admitted and running are the same set) and the ungated
tools keep a thread. `tests/test_depiction_bound.py` re-derives the pool from the Deployment.

Admission control, not a clock: cancelling the awaiting coroutine does not stop the worker thread,
while refusing before work starts orphans nothing. A queued call would return after
`request_timeout`. A refusal is a `ValueError`, which `connector_app` passes to the caller
verbatim. The shape mirrors `servers/calc`'s admission; servers never import one another.
"""

from __future__ import annotations

from mcp_server_kit.limits import Admission as KitAdmission

__all__ = [
    "ADMISSION_MARKER",
    "DEFAULT_MAX_CONCURRENT_HEAVY_CALLS",
    "GATED_TOOLS",
    "POD_THREAD_POOL_WIDTH",
    "PROBE_TIMEOUT_SECONDS",
    "RETIRED_VARIABLE",
    "VARIABLE",
    "WORST_RENDER_SECONDS",
    "Admission",
]

# Stamped by `tools._admitted` on a gated tool; the coverage test reads it to find the gated tools.
ADMISSION_MARKER = "__admission_gated__"

#: The tools that share the ceiling. `tests/test_admission.py` holds this against the served surface
#: in both directions, so a new heavy tool is either gated or argued there.
GATED_TOOLS = frozenset(
    {
        "render_structure",
        "describe_topology",
        "enumerate_tautomers",
        "enumerate_protonation_states",
        "enumerate_stereoisomers",
        "enumerate_degradants",
        "enumerate_substitutions",
    }
)

#: The environment variable the ceiling is read from, in `tools.py`.
VARIABLE = "CHEMCLAW_CHEM_MAX_CONCURRENT_HEAVY_CALLS"

#: The ceiling's former name. Refused at import rather than ignored, so a deployment that set it
#: does not silently run on the default.
RETIRED_VARIABLE = "CHEMCLAW_CHEM_MAX_CONCURRENT_RENDERS"

#: Yardstick, in seconds, that `tests/test_depiction_bound.py` holds a depiction regression against;
#: it does not derive the ceiling.
WORST_RENDER_SECONDS = 0.1

#: `deploy/deployment.yaml`'s `readinessProbe.timeoutSeconds`: the request that must not be starved.
PROBE_TIMEOUT_SECONDS = 3

#: Width of the default `asyncio.to_thread` pool on the shipped pod
#: (`mcp_server_kit.executor.thread_pool_size()` at `limits.cpu: "1"`).
#: `tests/test_depiction_bound.py` re-derives it from the Deployment.
POD_THREAD_POOL_WIDTH = 5

#: How many heavy calls may be in flight: the pool width less one, so admitted calls never queue
#: and ungated tools keep a thread. Overridable with `VARIABLE`, read in `tools.py`.
DEFAULT_MAX_CONCURRENT_HEAVY_CALLS = POD_THREAD_POOL_WIDTH - 1


class Admission(KitAdmission):
    """A count of heavy calls allowed in flight at once, refused rather than queued past it.

    The counter and lock are `mcp_server_kit.limits.Admission`'s; this class supplies the wording.
    """

    unit = "heavy call"
    server = "chem"

    def acquire(self, what: str) -> int:
        """Take a slot, or refuse in terms the caller can act on.

        Args:
            what: The tool being asked for, named in the refusal.

        Returns:
            The slot taken, for `Admission.admit` to give back when the work ends.

        Raises:
            AtCapacityError: the ceiling is already reached.
        """
        charged = self.take().charged
        if charged is None:
            raise self.refuse(
                f"this server is already running {self.limit} depictions or species "
                f"enumerations, which is its configured ceiling, so {what} was refused rather "
                "than queued: RDKit runs them one at a time on this pod's interpreter, so a "
                "queued one would come back after the caller had stopped waiting for it. Retry "
                f"once one finishes. Raising {VARIABLE} will not help — it admits more calls onto "
                "the same serialised interpreter, making every one of them slower; this server "
                "scales by replicas."
            )
        return charged
