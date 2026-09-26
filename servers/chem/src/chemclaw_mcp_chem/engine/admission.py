"""How many heavy calls this server accepts at once — one past the ceiling is refused, not held.

**One ceiling for the whole CPU band, and it used to be a ceiling on the one tool that needed it
least.** This gate covered `render_structure` alone, at 8, while the worst depiction the output
bound admits measures 4.6 ms; the five species tools beside it had no ceiling at all.
`D-2026-09-26-one-ceiling-for-the-band-and-it-is-the-pool-not-the-probe` re-measured them in the
`cc3-gate` image on a loaded eight-thread host (engine CPU per call, one call each):

    tool                          PAMAM G4 (996 atoms)   worst of five 1,990-atom shapes
    describe_topology                  5,181 ms           18,030 ms  polyester, answered
    enumerate_tautomers                3,583 ms           19,351 ms  polyester, then refused
    enumerate_protonation_states         760 ms              255 ms  (input-bounded)
    enumerate_stereoisomers               23 ms           10,226 ms  polyol, then refused
    enumerate_degradants               2,338 ms           47,572 ms  polyol, then refused

and, against a 10 ms tick beside them, whether one call holds the interpreter:

    describe_topology       n=1  tick 2,900 ms late    n=4  5,714 ms late
    enumerate_tautomers     n=1  tick 2,575 ms late    n=4  6,701 ms late
    enumerate_degradants    n=1  tick    64 ms late    n=4    187 ms late
    enumerate_protonation   n=1  tick    13 ms late    n=4     40 ms late

Two of the five are single RDKit calls that hold the GIL for their whole duration — a legal
dendrimer stalls this process for 2.9 s against a 3 s `readinessProbe.timeoutSeconds` — and the
other three yield it but still run one at a time (n=4 costs 4x n=1). So all six tools in
`GATED_TOOLS` spend the one resource this pod has one of, and they share one ceiling.

**What the ceiling cannot be derived from is the probe, and saying so is the correction.** The
render ceiling was derived as "N x the worst call under a third of the probe budget". For the band
the worst single call exceeds the whole probe budget on its own, so no N >= 1 satisfies that
arithmetic; nor does "N x worst under half of `request_timeout`", since one refused degradant run
costs 47 s against a 30 s budget. Both are properties of *one call*, which a ceiling bounds nothing
about — only an input bound prices them, and that is queued in `docs/BACKLOG.md` with these figures
rather than papered over with a number here.

**So the ceiling is derived from the pool, which is the half a ceiling does control.** Admitted
calls run one at a time whatever the ceiling, so a slot buys no throughput, and a slot beyond the
process's `asyncio.to_thread` pool buys a *queue*: the call is admitted and waits for a worker,
which is the thing this gate promises never to do — and the old render ceiling of 8 over a pool of
5 did exactly that. `DEFAULT_MAX_CONCURRENT_HEAVY_CALLS` is therefore the pool width less one, so
every admitted call has a worker the moment it is admitted and the ungated tools (a compound lookup,
a torsion list — measured at most 0.55 s on the same 1,990-atom shapes, and yielding) keep one
thread of their own. `tests/test_depiction_bound.py` re-derives the pool from the Deployment and
holds the ceiling under it.

**RDKit does not release the GIL through a depiction, and this file used to say it did.** The claim
was that "the threads are real parallelism", and that a burst on a pod sized for four therefore
thrashes. Measured on a four-core box, laying out a 241-atom molecule at rising thread counts:

    threads= 1  wall=0.043s  cpu=0.050s  cpu_util=1.15x  throughput=23.1/s
    threads= 2  wall=0.103s  cpu=0.100s  cpu_util=0.97x  throughput=19.5/s
    threads= 4  wall=0.205s  cpu=0.170s  cpu_util=0.83x  throughput=19.5/s
    threads= 8  wall=0.430s  cpu=0.360s  cpu_util=0.84x  throughput=18.6/s
    threads=16  wall=0.977s  cpu=0.780s  cpu_util=0.80x  throughput=16.4/s

Wall clock scales linearly with the thread count and this process never exceeds one core's worth of
CPU at any of them. Whatever this gate admits **runs one at a time**, and Chemclaw3's own
independent measurement of RDKit fingerprinting on four threads (0.91x) says the same thing
about the same library. So the thread pool buys *latency isolation* — a render does not block
the event loop, and `/healthz` keeps answering — and it buys **no throughput at all**.

**What that changes is the remedy, which is the part worth getting right.** Since concurrency here
is serial, a bigger ceiling and a bigger `limits.cpu` add nothing: the only way this server serves
more depictions per second is more pods, which is what `deploy/hpa.yaml` is for. The refusal below
therefore names a replica rather than the knob, because telling a caller to raise a ceiling that
cannot help is worse than telling it nothing.

**The render-only argument this file made next is kept below because it is what changed.** It
derived 8 from "N x 97 ms under a third of the probe", which the output bound later made
conservative by 20x (the 97 ms molecule is now refused outright). It then measured the 5-wide pool
against that 8, end to end against a running server — eight concurrent worst-legal renders with a
kubelet-shaped `/healthz` poll beside them:

    pool= 5  p50 541 ms  p95 748 ms  refused 0/48  /healthz 127/127 ok, p50 4 ms, max 450 ms
    pool= 8  p50 532 ms  p95 540 ms  refused 0/24  /healthz  59/59  ok, p50 3 ms, max 364 ms
    pool=12  p50 601 ms  p95 785 ms  refused 0/48  /healthz 126/126 ok, p50 4 ms, max 408 ms

and concluded that renders 6 to 8 queueing for a worker was harmless, because the wait was bounded
by the same `8 x WORST_RENDER_SECONDS` the ceiling was derived from, and that lowering the ceiling
to 5 would refuse three callers the pod answered in 0.6 s. **Both halves rested on every admitted
call being a render.** Once the five species tools share the gate, the call waiting for a worker can
be waiting behind four calls that each hold the interpreter for seconds, and "bounded by the product
the ceiling was derived from" has no product left to be bounded by. So the ceiling moves under the
pool, and the refusal a sixth caller now gets is the answer the fleet's gates give everywhere else.

What stands from that measurement is the structural half, and it is still why the pool is not the
knob: this pod may spend one core, so a wider pool cannot make a call finish sooner, and the GIL
means a 12-wide pool never grew past 4 to 6 workers anyway. `MCP_THREAD_POOL_SIZE` stays unset, and
raising `limits.cpu` stays wrong for the reason `deploy/hpa.yaml` gives. **This server scales by
replicas**, which is what the refusal says.

**This is admission control, and it is deliberately not a clock.** Cancelling the awaiting coroutine
does not stop the worker thread, so a per-call wall clock returns an error to a caller who has gone
while the CPU burn continues. Refusing *before* any work starts is the other thing entirely: nothing
is running, nothing is orphaned, and the caller learns immediately that the pod is full. Refused
rather than queued, for the same reason `servers/calc` refuses — a call held at the back of a
queue comes back after `connector.yaml`'s `request_timeout` has expired, computed at the expense of
one somebody is still waiting for. A `ValueError` is the family `connector_app` passes to the caller
verbatim, so the refusal is a number Chemclaw3 can back off on.

This mirrors `servers/calc/src/chemclaw_mcp_calc/engine/admission.py` deliberately: the two servers
share no code (one server never imports another), so the shape is copied, not imported. The bound
worth copying is the design — refuse, do not queue — not the wording: this server's refusal names a
replica where `calc`'s names a knob, because on `calc` a slot really is a core and here it is not.
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

# The attribute `tools._admitted` stamps on a gated tool, and the only thing that tells the coverage
# test which tools are gated. A name rather than a hand-kept list, because the thing that must not
# be forgotten is exactly the thing a forgetful change adds.
ADMISSION_MARKER = "__admission_gated__"

#: The tools that share the ceiling: the depiction and the five species tools the module docstring
#: measures, plus `enumerate_substitutions`, which joined the band after that measurement — each of
#: its candidates is a product canonicalised over the whole graph, the degradant enumerator's
#: shape, and `engine/substitution.py` has its own frontier. `tests/test_admission.py` holds this
#: against the *served* surface in both directions, so a heavy tool added next year is either
#: gated or named there as the reason it need not be.
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

#: What the ceiling was called while it gated one tool. **Refused at import rather than ignored**:
#: a deployment that set it would otherwise run on the default while believing its own number, which
#: is the silent kind of wrong `mcp_server_kit.limits.env_bound` exists to make loud.
RETIRED_VARIABLE = "CHEMCLAW_CHEM_MAX_CONCURRENT_RENDERS"

#: The worst depiction the render bound was first derived from, in seconds: a 241-atom polypeptide
#: at 97 ms, since refused outright by the output bound (the worst legal one is 4.6 ms). Kept as the
#: yardstick `tests/test_depiction_bound.py` holds a depiction regression against — it no longer
#: derives the ceiling, which the module docstring explains.
WORST_RENDER_SECONDS = 0.1

#: `deploy/deployment.yaml`'s `readinessProbe.timeoutSeconds`. The kubelet's probe is the request
#: that must not be starved, and the one no ceiling protects from a single call that holds the
#: interpreter longer than this — see the module docstring and `docs/BACKLOG.md`.
PROBE_TIMEOUT_SECONDS = 3

#: The width of the process's default `asyncio.to_thread` pool on the shipped pod: what
#: `mcp_server_kit.executor.thread_pool_size()` returns for `deploy/deployment.yaml`'s
#: `limits.cpu: "1"`, which is `ceil(1)` plus the kit's four-thread headroom.
#: `tests/test_depiction_bound.py` re-derives it from the Deployment through the kit's own
#: arithmetic, so `limits.cpu`, an `MCP_THREAD_POOL_SIZE` added to the pod, or a change to the
#: kit's headroom cannot move without this number and the ceiling below being revisited.
POD_THREAD_POOL_WIDTH = 5

#: How many heavy calls may be in flight: **the pool width less one**, so every admitted call has a
#: worker the moment it is admitted — admitted and running are the same set, which is what "refused
#: rather than queued" has to mean — and the ungated tools keep a thread of their own. Overridable
#: with `VARIABLE`, read in `tools.py`; the number lives here beside the argument for it.
DEFAULT_MAX_CONCURRENT_HEAVY_CALLS = POD_THREAD_POOL_WIDTH - 1


class Admission(KitAdmission):
    """A count of heavy calls allowed in flight at once, refused rather than queued past it.

    The counter, the clamp and the lock are `mcp_server_kit.limits.Admission`'s; what stays here is
    the sentence, because the levers it names are this server's. See that class for why nothing
    waits and why the refusal is not shared.
    """

    unit = "heavy call"

    def acquire(self, what: str) -> int:
        """Take a slot, or refuse in terms the caller can act on.

        Args:
            what: The tool being asked for, named in the refusal.

        Returns:
            The slot taken, for `Admission.admit` to give back when the work ends.

        Raises:
            ValueError: the ceiling is already reached. Worded for whoever receives it — an agent
                reading a tool error, or Chemclaw3 backing off.
        """
        charged = self.take().charged
        if charged is None:
            raise ValueError(
                f"this server is already running {self.limit} depictions or species "
                f"enumerations, which is its configured ceiling, so {what} was refused rather "
                "than queued: RDKit runs them one at a time on this pod's interpreter, so a "
                "queued one would come back after the caller had stopped waiting for it. Retry "
                f"once one finishes. Raising {VARIABLE} will not help — it admits more calls onto "
                "the same serialised interpreter, making every one of them slower; this server "
                "scales by replicas."
            )
        return charged
