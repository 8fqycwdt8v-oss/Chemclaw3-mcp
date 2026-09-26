"""How many semi-batch integrations this server accepts at once — a caller past it is refused.

`semibatch_accumulation_profile` is the one tool here that does real work. The other five are closed
form and cost microseconds; this one integrates, and since `reactors._steps_for_stability` derives
the step count from the problem rather than fixing it, its cost is set by the *caller's* rate
constant and dose time. The argument that this server owed no concurrency ceiling was made at a
fixed 200 steps and 0.83 ms (`reactors.DEFAULT_INTEGRATION_STEPS`' comment), and it stopped being
true of every dose when the floor arrived: the worst legal call runs just under
`reactors.MAX_INTEGRATION_STEPS`.

**Measured, in the `cc3-gate` Linux image on a loaded 8-thread host (2026-09-26)**, a 1 h dose of
5 mol into 0.10 volume against a co-reagent at 60, CPU time per call:

    k = 0.02    1,552 steps    24 ms
    k = 0.05    3,878 steps    58 ms
    k = 0.5    38,780 steps   585 ms
    k = 2.578 199,946 steps   1.8-3.4 s   (the worst legal call; peak 49.6 MB traced)

The backlog row that queued this measured the same ceiling at ~1.2 s on a quieter machine, so the
cost is a range and `WORST_INTEGRATION_SECONDS` is taken from the top of it rather than the bottom.

**Two defects, not one, and the first is the one a ceiling alone would not have fixed.** The tool
was a plain `def`, and FastMCP 1.x calls a synchronous tool *on the event loop*
(`FuncMetadata.call_fn_with_arg_validation`: `return fn(**arguments)` with no thread hop). So one
worst-case call stopped every other request on the process — every other tool call and the kubelet's
`/healthz` probe, whose `timeoutSeconds` is 3 — for as long as it ran. A ceiling on a tool that runs
on the loop would never trip, because two of them cannot be in flight at once; they queue on the
loop instead, which is the thing this fleet's admission gates exist to refuse. So the tool now
offloads with `asyncio.to_thread`, like every heavy tool in the fleet, and *then* the ceiling means
something.

**A pure-Python integration holds the GIL, so admitted integrations run one at a time** — the same
shape `servers/chem/src/chemclaw_mcp_chem/engine/admission.py` measured for RDKit, for a different
reason: CPython hands the GIL between CPU-bound threads every switch interval (5 ms), so N of them
share one core's worth of interpreter however many cores the pod has. The offload buys latency
isolation (the loop and the probe keep answering between switch intervals) and no throughput; this
server scales by replicas.

**So the ceiling is derived from the caller's budget.** N admitted worst-case integrations finish,
serialised, after N x `WORST_INTEGRATION_SECONDS`; keeping that under half of `connector.yaml`'s
`request_timeout` leaves the other half to the transport and to a slower node than the one measured.
At 15 s and 2 s that is three. Memory agrees: three worst cases hold ~150 MB of profile points
against the pod's 512 Mi limit. `tests/test_admission.py` holds the arithmetic against the manifest
rather than a transcription of it.

**This is admission control, not a clock**, for the reason `CLAUDE.md` gives: cancelling the
awaiting coroutine does not stop the worker thread, so a wall clock would answer a caller who has
gone while the integration kept burning. Refusing before any work starts orphans nothing, and a
refusal is a `ValueError`, which `connector_app` passes to the caller verbatim.

**Why not a cheaper integrator instead**, which the backlog row offered as the alternative: an
implicit or exponentially-fitted step would put the stiff case back under the band and lift the
refusal a dose past `MAX_INTEGRATION_STEPS` now gets. It would not remove the need for this gate —
the realistic `k = 0.5` case already sits at hundreds of milliseconds, well past the 8.1 ms the old
comment put beside `chem`'s gated `render_structure` — and it replaces a scheme whose convergence
order was *measured* (`reactors.MAX_INTEGRATION_STEPS`' comment) with one that has not been. That
trade is its own decision, recorded in
`docs/decisions/D-2026-09-26-a-tool-that-runs-on-the-event-loop-cannot-be-gated.md`.
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

#: The worst legal integration, in seconds of CPU: the top of the range measured in the module
#: docstring, rounded to the measurement's precision rather than to its best run.
WORST_INTEGRATION_SECONDS = 2.0

#: How many integrations may be in flight. Derived in the module docstring from the request budget:
#: `floor(request_timeout / (2 * WORST_INTEGRATION_SECONDS))` at a 15 s budget. Overridable with
#: `CHEMCLAW_KINETICS_MAX_CONCURRENT_INTEGRATIONS`, read in `tools.py`.
DEFAULT_MAX_CONCURRENT_INTEGRATIONS = 3


class Admission(KitAdmission):
    """A count of integrations allowed in flight at once, refused rather than queued past it.

    The counter, the clamp and the lock are `mcp_server_kit.limits.Admission`'s; what stays here is
    the sentence, because the lever it names is this server's.
    """

    unit = "integration"

    def acquire(self, what: str) -> None:
        """Take a slot, or refuse in terms the caller can act on.

        Args:
            what: The tool being asked for, named in the refusal.

        Raises:
            ValueError: the ceiling is already reached. Worded for whoever receives it — an agent
                reading a tool error, or Chemclaw3 backing off.
        """
        if self.take().charged is None:
            raise ValueError(
                f"this server is already integrating {self.limit} semi-batch doses, which is its "
                f"configured ceiling, so {what} was refused rather than queued: the integration "
                "is pure Python and holds the interpreter, so admitted ones run one at a time and "
                "a queued one would come back after the caller had stopped waiting for it. Retry "
                "once one finishes. Raising CHEMCLAW_KINETICS_MAX_CONCURRENT_INTEGRATIONS will not "
                "help — it admits more onto the same serialised interpreter; this server scales "
                "by replicas."
            )
