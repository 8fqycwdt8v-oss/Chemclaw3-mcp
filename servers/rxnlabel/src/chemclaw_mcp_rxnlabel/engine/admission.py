"""How much of this pod a batch may occupy at once — shed at admission, never queued.

`MAX_BATCH` bounds one request; this bounds how many are in flight, which matters because the caller
is a corpus drain sending many maximal batches as normal traffic.

A slot is a core, not a call. The RDKit path holds the GIL, so it costs one slot; with a mapper
installed the cost is `mapping.inference_threads()`, read from torch at call time, because torch
parallelises inside one forward pass. A cost above the ceiling is clamped so an oversized batch runs
exclusively rather than never. A full pod refuses with a `ValueError` (passed to the caller
verbatim) rather than queueing past `request_timeout`; the drain retries the same batch. This is
admission control, not a clock: cancelling the awaiting coroutine would not stop the worker thread.
"""

from __future__ import annotations

from mcp_server_kit.limits import Admission as KitAdmission

__all__ = ["ADMISSION_MARKER", "DEFAULT_MAX_CONCURRENT_BATCHES", "Admission"]

# The attribute `tools._admitted` stamps on a gated tool; the coverage test reads it instead of a
# hand-kept list.
ADMISSION_MARKER = "__admission_gated__"

#: Cores' worth of labelling that may be in flight; equals `deploy/deployment.yaml`'s `limits.cpu`,
#: which `tests/test_admission.py` checks. Overridable with
#: `CHEMCLAW_RXNLABEL_MAX_CONCURRENT_BATCHES`, read in `tools.py`.
DEFAULT_MAX_CONCURRENT_BATCHES = 2

#: How many reactions one request may carry, so one call stays well inside the caller's timeout.
#: Overridable with `CHEMCLAW_RXNLABEL_MAX_BATCH`, read in `tools.py`.
DEFAULT_MAX_BATCH = 500


class Admission(KitAdmission):
    """A budget of concurrent labelling slots, refused rather than queued past it.

    The mechanics are `mcp_server_kit.limits.Admission`'s; this subclass words the refusal.
    """

    unit = "batch"
    server = "rxnlabel"

    def acquire(self, what: str, cost: int = 1) -> int:
        """Take `cost` slots, or refuse in terms the caller can act on.

        Args:
            what: The tool being asked for, named in the refusal.
            cost: Slots this call occupies, clamped into `1..limit` by the base class.

        Returns:
            The slots actually taken, which `release` must be given back.

        Raises:
            AtCapacityError: The budget has no room; the caller may back off and re-send.
        """
        taken = self.take(cost)
        if taken.charged is None:
            raise self.refuse(
                f"this server has {taken.free} of its {self.limit} labelling slots free and "
                f"{what} needs {min(max(cost, 1), self.limit)}, so it was refused rather than "
                "queued: a slot is one core, a maximal batch is seconds to minutes of CPU, and a "
                "queued one would come back after the caller had stopped waiting for it. Re-send "
                "the identical batch once one finishes, or send a smaller one; raise "
                "CHEMCLAW_RXNLABEL_MAX_CONCURRENT_BATCHES only on a pod with more cores, "
                "because labelling is CPU-bound and a wider ceiling on the same pod makes "
                "every batch slower without labelling one extra reaction"
            )
        return taken.charged
