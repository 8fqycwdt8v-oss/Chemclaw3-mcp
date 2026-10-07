"""How many programs this server will run at once — shed at admission, never queued.

The ceiling is **cores**: a run is one child pinned to one thread, so a slot is a core, and
`deploy/deployment.yaml` sets `limits.cpu` to the same number (`tests/test_capacity.py` reads it).
Admitting more runs than cores turns a program's CPU budget into a wall-clock kill reported as
the program's fault. The same number divides the pod's memory limit (`limits.default_memory_bytes`).

Refused rather than queued: a queued program burns its wall clock waiting and is then killed. The
refusal is a `ValueError`, which `connector_app` passes to the caller verbatim.
"""

from __future__ import annotations

from mcp_server_kit.limits import Admission as KitAdmission

__all__ = ["ADMISSION_MARKER", "DEFAULT_MAX_CONCURRENT_RUNS", "Admission"]

# Stamped by `tools._admitted` on a gated tool; the coverage test reads it to find the gated tools.
ADMISSION_MARKER = "__admission_gated__"

# Default ceiling, and divisor of the pod's memory: one slot per core the Deployment grants.
# Overridable with `CHEMCLAW_PYEXEC_MAX_CONCURRENT_RUNS` (read in `tools.py`).
DEFAULT_MAX_CONCURRENT_RUNS = 2


class Admission(KitAdmission):
    """A count of sandboxed runs allowed at once, refused rather than queued past it.

    The counter and lock are `mcp_server_kit.limits.Admission`'s; this class supplies the wording.
    """

    unit = "run"
    server = "pyexec"

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
                f"this server is already running {self.limit} analyses, which is its "
                f"configured ceiling, so {what} was refused rather than queued: each run is a "
                "whole core for up to its wall-clock limit, and a queued one would spend that "
                "limit waiting and then be killed for exceeding it. Retry once one finishes, "
                "or raise CHEMCLAW_PYEXEC_MAX_CONCURRENT_RUNS on a pod with more cores and "
                "more memory — the per-run address-space bound is the pod's memory divided by "
                "this number."
            )
        return charged
