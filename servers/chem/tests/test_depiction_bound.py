"""`render_structure` must refuse a large molecule promptly, not lay it out for minutes.

`Compute2DCoords` is superlinear: a ~1500-atom molecule took 672 s of one worker thread, and the
offload thread's cancellation does not stop it, so the caller's timeout frees nothing. Two bounds
protect the pod — a per-call atom ceiling (`MAX_DEPICTION_ATOMS`) and a concurrency ceiling
(`Admission`). These pin both, and that the refusal is *fast* rather than a hang.
"""

from __future__ import annotations

import os
import threading
import time

import pytest
from chemclaw_mcp_chem.engine.admission import (
    DEFAULT_MAX_CONCURRENT_RENDERS,
    PROBE_TIMEOUT_SECONDS,
    WORST_RENDER_SECONDS,
    Admission,
)
from chemclaw_mcp_chem.engine.chem import InvalidSmilesError
from chemclaw_mcp_chem.engine.depiction import MAX_DEPICTION_ATOMS, render_svg

#: A large-but-legal molecule, and the worst case for `Compute2DCoords`: a 241-atom polypeptide.
#: Shape rather than size is what costs — a 249-atom alkane draws in 22 ms and a 201-atom macrocycle
#: in 19 ms — so the ceiling is derived against this one.
WORST_LEGAL_MOLECULE = "NC(C)C(=O)" + "NC(C)C(=O)" * 38 + "O"


def test_a_molecule_over_the_depiction_bound_is_refused_fast() -> None:
    """A molecule above `MAX_DEPICTION_ATOMS` is refused before `Compute2DCoords`, under 1 s.

    The atom count (just over the bound) is below the parse-level bounds, so this exercises the
    depiction ceiling specifically rather than the SMILES-length or atom-count parse guards.
    """
    oversize = "C" * (MAX_DEPICTION_ATOMS + 20)
    start = time.monotonic()
    with pytest.raises(InvalidSmilesError, match=r"depiction limit|above the"):
        render_svg(oversize)
    assert time.monotonic() - start < 1.0


def test_the_runaway_string_is_refused_by_the_length_bound() -> None:
    """The verified PoC (`"C" * 6000`) is refused by the length bound before it ever parses."""
    start = time.monotonic()
    with pytest.raises(InvalidSmilesError):
        render_svg("C" * 6000)
    assert time.monotonic() - start < 1.0


def test_a_real_molecule_still_draws() -> None:
    """The bound must not touch an ordinary structure."""
    svg = render_svg("CCO")
    assert "<svg" in svg


def test_a_real_molecule_with_a_highlight_still_draws() -> None:
    """A highlighted depiction — the torsion-confirmation path — is unaffected."""
    svg = render_svg("CC(=O)Nc1ccccc1", highlight_atoms=[0, 1])
    assert "<svg" in svg


def test_admission_refuses_past_the_ceiling() -> None:
    """The concurrency ceiling refuses rather than queues, in terms the caller can act on."""
    gate = Admission(limit=1)
    gate.acquire("render_structure")
    assert gate.in_flight == 1
    with pytest.raises(ValueError, match=r"already rendering 1 structures"):
        gate.acquire("render_structure")
    gate.release()
    assert gate.in_flight == 0
    # A slot is reusable once released.
    gate.acquire("render_structure")
    gate.release()


def test_admission_rejects_a_ceiling_below_one() -> None:
    """A ceiling of zero would refuse every depiction — caught at construction."""
    with pytest.raises(ValueError):
        Admission(limit=0)


def test_a_depiction_holds_the_gil_so_threads_buy_no_throughput() -> None:
    """The claim `engine/admission.py` used to make, checked instead of believed.

    It said "RDKit releases the GIL for the heavy passes, so the threads are real parallelism" —
    and the ceiling of 8 was justified as protecting a four-core pod from eight parallel renders.
    Measured, the eight run one at a time: 1 to 16 threads on a four-core box all sat at cpu_util
    0.80-1.15x, wall clock scaled linearly, and throughput stayed flat at 16-23/s.

    Asserted as "four threads take at least twice as long as one", which is a bound no machine's
    speed can move and no scheduler noise can cross: real parallelism on any box with two or more
    cores would put four renders at roughly the wall clock of one. It is the *direction* of the
    claim that this test exists to hold, because the direction is what decides whether this server
    is scaled with a bigger ceiling or with another pod — and the refusal message tells a caller
    which.
    """
    render_svg(WORST_LEGAL_MOLECULE)  # warm RDKit; the first call pays for its lazy imports.

    def timed(threads: int) -> float:
        workers = [
            threading.Thread(target=render_svg, args=(WORST_LEGAL_MOLECULE,))
            for _ in range(threads)
        ]
        started = time.perf_counter()
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        return time.perf_counter() - started

    one, four = timed(1), timed(4)
    assert os.cpu_count() and os.cpu_count() > 1, "a single-core runner cannot answer this question"
    assert four >= 2 * one, (
        f"four concurrent depictions took {four:.3f}s against {one:.3f}s for one. That is closer "
        "to parallel than to serial, so RDKit's GIL behaviour has changed and both the admission "
        "ceiling's derivation and the refusal message that tells callers to add a replica rather "
        "than raise the ceiling need re-deriving"
    )


def test_the_ceiling_leaves_the_readiness_probe_its_budget() -> None:
    """Why the ceiling is 8, now that "one core each" is not the reason.

    A render holds the GIL, so admitted renders delay *everything else in this process* — including
    the kubelet's `/healthz` probe, whose `timeoutSeconds` is 3. N of them can hold the interpreter
    for N x the worst legal render, and a probe that times out three times takes the pod out of
    service, which is the outage the gate exists to prevent rather than cause.

    A third of the probe's budget, so two thirds are left for the probe's own work and for whatever
    else the loop owes. That gives a ceiling of 10; the shipped 8 is inside it.
    """
    held = DEFAULT_MAX_CONCURRENT_RENDERS * WORST_RENDER_SECONDS
    assert held <= PROBE_TIMEOUT_SECONDS / 3, (
        f"{DEFAULT_MAX_CONCURRENT_RENDERS} renders can hold this interpreter for {held:.2f}s "
        f"against a {PROBE_TIMEOUT_SECONDS}s probe timeout"
    )


def test_the_worst_legal_depiction_still_costs_what_the_ceiling_was_derived_from() -> None:
    """`WORST_RENDER_SECONDS` is an input to the ceiling, so a regression in it moves the ceiling.

    Checked with 4x headroom rather than tightly: this runs on whatever CI box it is given, and the
    failure worth catching is an algorithmic one — a depiction that got an order of magnitude
    slower — not a slower machine.
    """
    render_svg(WORST_LEGAL_MOLECULE)  # warm.
    started = time.perf_counter()
    render_svg(WORST_LEGAL_MOLECULE)
    elapsed = time.perf_counter() - started
    assert elapsed <= 4 * WORST_RENDER_SECONDS, (
        f"the worst legal depiction now costs {elapsed * 1000:.0f} ms against the "
        f"{WORST_RENDER_SECONDS * 1000:.0f} ms the admission ceiling was derived from"
    )
