"""How many semi-batch integrations this server accepts at once — a caller past it is refused.

`semibatch_accumulation_profile` is the one tool that does real work: its RK4 step count is set by
the caller's rate constant and dose time, so the worst legal call costs about
`WORST_INTEGRATION_SECONDS` of CPU. The tool offloads with `asyncio.to_thread` so it never blocks
the event loop (and `/healthz`); a pure-Python integration holds the GIL, so admitted ones run
effectively one at a time and the server scales by replicas.

The ceiling is derived from the caller's budget: N serialised worst-case integrations must finish
inside half of `connector.yaml`'s `request_timeout` (`tests/test_admission.py` holds this against
the manifest). It is admission control, not a clock: cancelling the awaiting coroutine does not
stop the worker thread, while refusing before work starts orphans nothing. A refusal is a
`ValueError`, which `connector_app` passes to the caller verbatim.
"""

from __future__ import annotations

from mcp_server_kit.limits import Admission as KitAdmission

__all__ = [
    "ADMISSION_MARKER",
    "DEFAULT_MAX_CONCURRENT_INTEGRATIONS",
    "WORST_INTEGRATION_SECONDS",
    "Admission",
]

# The attribute `tools._admitted` stamps on a gated tool, and the only thing that tells the coverage
# test which tools are gated — a name rather than a hand-kept list, as in `servers/chem`.
ADMISSION_MARKER = "__admission_gated__"

#: The worst legal RK4 integration, in seconds of CPU, taken from the top of its measured range.
WORST_INTEGRATION_SECONDS = 2.0

#: How many integrations may be in flight:
#: `floor(request_timeout / (2 * WORST_INTEGRATION_SECONDS))` at a 15 s budget. Overridable with
#: `CHEMCLAW_KINETICS_MAX_CONCURRENT_INTEGRATIONS`, read in `tools.py`.
DEFAULT_MAX_CONCURRENT_INTEGRATIONS = 3


class Admission(KitAdmission):
    """A count of integrations allowed in flight at once, refused rather than queued past it.

    The counter and lock are `mcp_server_kit.limits.Admission`'s; this class supplies the wording.
    """

    unit = "integration"
    server = "kinetics"

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
                f"this server is already integrating {self.limit} semi-batch doses, which is its "
                f"configured ceiling, so {what} was refused rather than queued: the integration "
                "is pure Python and holds the interpreter, so admitted ones run one at a time and "
                "a queued one would come back after the caller had stopped waiting for it. Retry "
                "once one finishes. Raising CHEMCLAW_KINETICS_MAX_CONCURRENT_INTEGRATIONS will not "
                "help — it admits more onto the same serialised interpreter; this server scales "
                "by replicas."
            )
        return charged
