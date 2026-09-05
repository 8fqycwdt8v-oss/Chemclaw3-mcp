"""`render_structure` must refuse a large molecule promptly, not lay it out for minutes.

`Compute2DCoords` is superlinear: a ~1500-atom molecule took 672 s of one worker thread, and the
offload thread's cancellation does not stop it, so the caller's timeout frees nothing. Two bounds
protect the pod — a per-call atom ceiling (`MAX_DEPICTION_ATOMS`) and a concurrency ceiling
(`Admission`). These pin both, and that the refusal is *fast* rather than a hang.

**A third bound exists that this server does not own**: the process's default `to_thread` pool,
which `mcp_server_kit` sizes from the pod's cgroup and which is *narrower* than the admission
ceiling. `engine/admission.py` argues why that is sound and why neither number moves; the last
three tests here are what stop the two drifting apart, by re-deriving the pool's width from the
Deployment through the kit's own arithmetic rather than trusting a comment that says they agree.
Both numbers already carried a comment, which is exactly why a comment is not the check.
"""

from __future__ import annotations

import math
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from chemclaw_mcp_chem.engine.admission import (
    DEFAULT_MAX_CONCURRENT_RENDERS,
    POD_THREAD_POOL_WIDTH,
    PROBE_TIMEOUT_SECONDS,
    WORST_RENDER_SECONDS,
    Admission,
)
from chemclaw_mcp_chem.engine.chem import InvalidSmilesError
from chemclaw_mcp_chem.engine.depiction import MAX_DEPICTION_ATOMS, render_svg
from mcp_server_kit import executor

#: The two files the pod's thread pool is decided by, read rather than transcribed.
DEPLOYMENT = Path(__file__).resolve().parents[1] / "deploy" / "deployment.yaml"
MANIFEST = Path(__file__).resolve().parents[1] / "connector.yaml"

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


def _container() -> dict[str, Any]:
    """The shipped Deployment's one container, where both the CPU limit and any `env:` live."""
    loaded = yaml.safe_load(DEPLOYMENT.read_text(encoding="utf-8"))
    container = loaded["spec"]["template"]["spec"]["containers"][0]
    assert isinstance(container, dict)
    return container


def _pod_cpu_limit_cores() -> float:
    """`limits.cpu` in cores. Kubernetes accepts `"500m"`, `"1"` and `1` for the same field."""
    declared = str(_container()["resources"]["limits"]["cpu"])
    return float(declared[:-1]) / 1000 if declared.endswith("m") else float(declared)


def _pod_thread_pool_width(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    """The `to_thread` pool width the shipped pod gets, computed by the kit's own arithmetic.

    The cgroup this container runs under is written to a file and `mcp_server_kit.executor` is
    pointed at it, so `thread_pool_size()` answers for *that* pod rather than for the box running
    the test — and any `env:` the Deployment grows is applied on the way in, so a future
    `MCP_THREAD_POOL_SIZE` is picked up here instead of silently overtaking the number
    `engine/admission.py` argues against.
    """
    quota = tmp_path / "cpu.max"
    quota.write_text(f"{int(_pod_cpu_limit_cores() * 100_000)} 100000\n", encoding="utf-8")
    monkeypatch.setattr(executor, "_CGROUP_V2_CPU_MAX", quota)
    declared = {entry["name"]: str(entry["value"]) for entry in _container().get("env", [])}
    for knob in ("MCP_THREAD_POOL_SIZE", "MCP_THREAD_POOL_HEADROOM"):
        if knob in declared:
            monkeypatch.setenv(knob, declared[knob])
        else:
            monkeypatch.delenv(knob, raising=False)
    return executor.thread_pool_size()


def test_the_pod_pool_is_the_width_the_ceiling_was_argued_against(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`POD_THREAD_POOL_WIDTH` is a claim about a pod, so it is re-derived from that pod.

    The two ceilings — 8 admitted renders, 5 offload threads — were each written with a comment
    saying what the other one was, and nothing checked either. This is the check: the width comes
    from the Deployment's `limits.cpu` through `mcp_server_kit`'s own `thread_pool_size()`, so a
    change to the CPU limit, to the kit's headroom, or an `MCP_THREAD_POOL_SIZE` added to the pod
    lands here rather than in a paragraph that goes on describing the old arrangement.
    """
    width = _pod_thread_pool_width(tmp_path, monkeypatch)
    assert width == POD_THREAD_POOL_WIDTH, (
        f"this pod's to_thread pool is {width} threads, not the {POD_THREAD_POOL_WIDTH} that "
        "engine/admission.py's argument for a ceiling of "
        f"{DEFAULT_MAX_CONCURRENT_RENDERS} was measured against. Re-derive that argument before "
        "moving the constant: it is what says the ceiling may exceed the pool"
    )


def test_threads_are_never_scarcer_than_the_cpu_this_pod_may_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The invariant that keeps the *gate* the bound rather than the pool.

    An admission ceiling above the pool is sound only while the pool is not itself the scarce
    resource — with `limits.cpu: "1"` and five threads, a render that waits waits for a core it
    would have waited for anyway. Set `MCP_THREAD_POOL_SIZE` below the CPU allowance and that stops
    being true: threads would run out before the core did, and the number that decided how long a
    caller waited would be one nobody had derived. Written against the allowance rather than a
    literal so it holds for whatever `limits.cpu` this pod is later given.
    """
    width = _pod_thread_pool_width(tmp_path, monkeypatch)
    allowance = math.ceil(_pod_cpu_limit_cores())
    assert width > allowance, (
        f"this pod may spend {_pod_cpu_limit_cores()} cores and has {width} offload threads: the "
        "pool is now the scarcer of the two, so it — not the admission gate — decides how long an "
        "admitted render waits, and engine/admission.py's derivation no longer describes this pod"
    )


def test_a_render_the_pool_makes_wait_still_answers_inside_the_callers_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the ceiling exceeding the pool actually costs, held against the caller's own budget.

    With a pool narrower than the ceiling, the last few admitted renders wait for a worker thread
    rather than for the CPU — queued, which is the one thing this gate promises it never does. The
    wait is bounded by the same product the ceiling was derived from, because nothing else runs
    while a render holds the interpreter, so the whole burst is answered within
    `DEFAULT_MAX_CONCURRENT_RENDERS x WORST_RENDER_SECONDS` whichever of the two a caller is
    waiting on. That has to stay inside `connector.yaml`'s `request_timeout` — a queued render that
    comes back after the caller has stopped waiting is precisely the outcome refusing exists to
    avoid, and it is the ceiling, not the pool, that would take it there.
    """
    width = _pod_thread_pool_width(tmp_path, monkeypatch)
    queued = max(0, DEFAULT_MAX_CONCURRENT_RENDERS - width)
    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    budget = float(manifest["endpoint"]["request_timeout"])
    worst_wait = DEFAULT_MAX_CONCURRENT_RENDERS * WORST_RENDER_SECONDS
    assert worst_wait <= budget, (
        f"{queued} of {DEFAULT_MAX_CONCURRENT_RENDERS} admitted renders wait for one of {width} "
        f"worker threads, and the burst takes {worst_wait:.1f}s against a {budget:.0f}s "
        "request_timeout. Past that, the gate admits work whose answer nobody is still waiting "
        "for — lower the ceiling or raise the pool, and say in engine/admission.py which"
    )
