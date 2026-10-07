"""How much CPU this server will run at once — shed at admission, never queued.

**A slot is a core.** An in-process calculation costs one (the image pins `OMP_NUM_THREADS=1`); a
CREST search costs `crest_threads`, the thread count the sampler is given. A cost above the
ceiling is clamped so the call runs exclusively rather than never.

**Refused, not queued**: a queued call would finish after its caller's `request_timeout`, at the
expense of calls still wanted. Admission composes with `budget.Deadline` (how long one may run) and
`xtb_cli.run_isolated` (killing an escaped process group).

**Gated set = the manifest's `state_changing` tools** (`tests/test_admission.py`);
`calculation_key`, `embed_structure` and `combine_structures` stay answerable while the pod is full.

**A full pod refuses with `AT_CAPACITY_MARKER` at the head of the message**, the only channel a
refused MCP tool call has, so Chemclaw3 can retry backpressure rather than treat it as bad data.
The slot is held until the worker finishes (`asyncio.shield`), not just while the caller waits.
"""

from __future__ import annotations

from mcp_server_kit.limits import Admission as KitAdmission
from mcp_server_kit.limits import AtCapacityError, at_capacity_marker

__all__ = ["ADMISSION_MARKER", "AT_CAPACITY_MARKER", "Admission", "AtCapacityError"]

# The attribute `tools._admitted` stamps on a gated tool, which the coverage test reads.
ADMISSION_MARKER = "__admission_gated__"

# "Full pod", not "bad input". At the head of the message, which survives transport wrapping and
# which a caller's quoted arguments cannot reach. Chemclaw3 transcribes it as
# `core/mcp_session.SERVER_AT_CAPACITY`; each repository pins only its own copy, so a reword must be
# carried across by hand.
AT_CAPACITY_MARKER = at_capacity_marker("calc")


class Admission(KitAdmission):
    """A budget of concurrent calculation slots, refused rather than queued past it.

    The counting is `mcp_server_kit.limits.Admission`'s; this adds the refusal, led by
    `AT_CAPACITY_MARKER` and typed `AtCapacityError`, both of which Chemclaw3 matches.
    """

    unit = "calculation"
    server = "calc"

    def acquire(self, what: str, cost: int = 1) -> int:
        """Take `cost` slots for the calculation `what`, or refuse in terms the caller can act on.

        Returns the slots actually taken (`cost` clamped into `1..limit`), which `release` must be
        given. Raises `AtCapacityError`, led by `AT_CAPACITY_MARKER`, when there is no room.
        """
        taken = self.take(cost)
        if taken.charged is None:
            raise self.refuse(
                f"this server has {taken.free} of its {self.limit} calculation slots free and "
                f"{what} needs {min(max(cost, 1), self.limit)}, so it was refused rather than "
                "queued: a slot is one core, the calculations here are seconds to hours of CPU, "
                "and a queued one would come back after the caller had stopped waiting for it. "
                "Retry once one finishes, or raise CHEMCLAW_CALC_MAX_CONCURRENT_REQUESTS on a pod "
                "sized for more"
            )
        return taken.charged
