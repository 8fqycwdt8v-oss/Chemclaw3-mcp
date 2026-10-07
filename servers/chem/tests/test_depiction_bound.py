"""`render_structure` must refuse a large molecule promptly, not lay it out for minutes.

`Compute2DCoords` is superlinear and cancellation does not stop it, so two bounds protect the pod:
a per-call atom ceiling (`MAX_DEPICTION_ATOMS`) and a concurrency ceiling (`Admission`). The
admission ceiling is derived from the kit's cgroup-sized `to_thread` pool (the pool less one), and
the pool tests re-derive that width from the Deployment so the two cannot drift apart.
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
from chemclaw_mcp_chem.engine import depiction
from chemclaw_mcp_chem.engine.admission import (
    DEFAULT_MAX_CONCURRENT_HEAVY_CALLS,
    POD_THREAD_POOL_WIDTH,
    WORST_RENDER_SECONDS,
    Admission,
)
from chemclaw_mcp_chem.engine.chem import InvalidSmilesError
from chemclaw_mcp_chem.engine.depiction import (
    MAX_DEPICTION_ATOMS,
    MAX_DEPICTION_CHARS,
    MINIMUM_RENDER_SIZE_PX,
    render_svg,
)
from mcp_server_kit import executor
from mcp_server_kit.testing import reimported
from rdkit import Chem
from rdkit.Chem.Draw import rdMolDraw2D

#: The file the pod's thread pool is decided by, read rather than transcribed.
DEPLOYMENT = Path(__file__).resolve().parents[1] / "deploy" / "deployment.yaml"

#: The worst case for `Compute2DCoords` that is legal under both bounds: a 76-atom polypeptide.
#: Shape, not size, is what costs. For this shape the character bound binds well before the atom
#: bound. `WORST_RENDER_SECONDS` is a regression yardstick, no longer the ceiling's basis.
WORST_LEGAL_MOLECULE = "NC(C)C(=O)" + "NC(C)C(=O)" * 14 + "O"


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
    with pytest.raises(ValueError, match=r"already running 1 depictions"):
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
    """A depiction holds the GIL, so threads buy no throughput.

    Asserted as "four threads take at least twice as long as one", which no machine speed can move.
    The direction decides whether the server scales by ceiling or by replicas, and the refusal
    message tells the caller which.
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
    cores = os.cpu_count()
    assert cores is not None and cores > 1, "a single-core runner cannot answer this question"
    assert four >= 2 * one, (
        f"four concurrent depictions took {four:.3f}s against {one:.3f}s for one. That is closer "
        "to parallel than to serial, so RDKit's GIL behaviour has changed and both the admission "
        "ceiling's derivation and the refusal message that tells callers to add a replica rather "
        "than raise the ceiling need re-deriving"
    )


def test_the_worst_legal_depiction_still_costs_what_the_ceiling_was_derived_from() -> None:
    """The worst legal depiction still costs about what `WORST_RENDER_SECONDS` says.

    4x headroom: the regression worth catching is algorithmic, not a slower CI box.
    """
    render_svg(WORST_LEGAL_MOLECULE)  # warm.
    started = time.perf_counter()
    render_svg(WORST_LEGAL_MOLECULE)
    elapsed = time.perf_counter() - started
    assert elapsed <= 4 * WORST_RENDER_SECONDS, (
        f"the worst legal depiction now costs {elapsed * 1000:.0f} ms against the "
        f"{WORST_RENDER_SECONDS * 1000:.0f} ms yardstick; a depiction that slow belongs in "
        "engine/admission.py's measurement before it belongs in the gated band"
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

    The Deployment's cgroup is written to a file for `mcp_server_kit.executor`, and its `env:`
    applied, so the answer is for that pod, not the test box.
    """
    quota = tmp_path / "cpu.max"
    quota.write_text(f"{int(_pod_cpu_limit_cores() * 100_000)} 100000\n", encoding="utf-8")
    monkeypatch.setattr(executor, "_CGROUP_V2_CPU_MAX", quota)
    # Literal values only: a `valueFrom` entry (the bearer, from a Secret) is not a sizing knob.
    declared = {
        entry["name"]: str(entry["value"])
        for entry in _container().get("env", [])
        if "value" in entry
    }
    for knob in ("MCP_THREAD_POOL_SIZE", "MCP_THREAD_POOL_HEADROOM"):
        if knob in declared:
            monkeypatch.setenv(knob, declared[knob])
        else:
            monkeypatch.delenv(knob, raising=False)
    return executor.thread_pool_size()


def test_the_pod_pool_is_the_width_the_ceiling_was_argued_against(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`POD_THREAD_POOL_WIDTH` is re-derived from the pod it describes.

    From the Deployment's `limits.cpu` through `thread_pool_size()`, so a CPU, headroom or
    `MCP_THREAD_POOL_SIZE` change lands here.
    """
    width = _pod_thread_pool_width(tmp_path, monkeypatch)
    assert width == POD_THREAD_POOL_WIDTH, (
        f"this pod's to_thread pool is {width} threads, not the {POD_THREAD_POOL_WIDTH} that "
        "engine/admission.py derives the ceiling of "
        f"{DEFAULT_MAX_CONCURRENT_HEAVY_CALLS} from. Re-derive that argument before moving the "
        "constant"
    )


def test_threads_are_never_scarcer_than_the_cpu_this_pod_may_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Threads are never scarcer than the CPU this pod may spend.

    Only then is the gate, not the pool, the bound. Written against the CPU allowance rather than a
    literal.
    """
    width = _pod_thread_pool_width(tmp_path, monkeypatch)
    allowance = math.ceil(_pod_cpu_limit_cores())
    assert width > allowance, (
        f"this pod may spend {_pod_cpu_limit_cores()} cores and has {width} offload threads: the "
        "pool is now the scarcer of the two, so it — not the admission gate — decides how long an "
        "admitted render waits, and engine/admission.py's derivation no longer describes this pod"
    )


def test_every_admitted_call_has_a_worker_and_the_ungated_tools_keep_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every admitted call has a worker, and the ungated tools keep one.

    Admitted and running are the same set, which is what "refused rather than queued" means; a
    waiting call could sit behind several seconds-long species calls. One thread is left for ungated
    tools.
    """
    width = _pod_thread_pool_width(tmp_path, monkeypatch)
    assert width >= DEFAULT_MAX_CONCURRENT_HEAVY_CALLS + 1, (
        f"a ceiling of {DEFAULT_MAX_CONCURRENT_HEAVY_CALLS} over a {width}-thread pool admits "
        "calls that then wait for a worker, or leaves the ungated tools none — queueing behind "
        "a gate that promises not to queue"
    )
    assert DEFAULT_MAX_CONCURRENT_HEAVY_CALLS >= 1


# --- The output bound -------------------------------------------------------------------------
#
# The atom ceiling bounds cost, not output size. A large SVG would exceed the caller's per-result
# character cut, and a truncated SVG is no picture at all, still paid for in tokens.

ERYTHROMYCIN = (
    "CC[C@H]1OC(=O)[C@H](C)[C@@H](O[C@H]2C[C@@](C)(OC)[C@@H](O)[C@H](C)O2)[C@H](C)"
    "[C@@H](O[C@@H]2O[C@H](C)C[C@@H]([C@H]2O)N(C)C)[C@](C)(O)C[C@@H](C)C(=O)[C@H](C)"
    "[C@@H](O)[C@]1(C)O"
)


def test_a_depiction_over_the_character_bound_is_refused_not_truncated() -> None:
    """An oversized SVG is refused whole, in a message the caller can act on.

    `MAX_DEPICTION_ATOMS` admits this molecule, so this pins the *output* bound specifically.
    """
    oversize = "C" * MAX_DEPICTION_ATOMS
    with pytest.raises(InvalidSmilesError) as refusal:
        render_svg(oversize)
    message = str(refusal.value)
    assert "characters" in message
    assert str(MAX_DEPICTION_CHARS) in message
    assert "CHEMCLAW_CHEM_MAX_DEPICTION_CHARS" in message


def test_a_highlighted_depiction_is_measured_after_its_highlights() -> None:
    """Highlights roughly double the SVG, so the bound has to be read off the finished drawing."""
    smiles = "C" * 100
    assert len(render_svg(smiles)) < MAX_DEPICTION_CHARS
    with pytest.raises(InvalidSmilesError, match="highlight"):
        render_svg(smiles, highlight_atoms=list(range(100)))


def test_a_drug_sized_molecule_is_comfortably_inside_the_bound() -> None:
    """Erythromycin, 51 heavy atoms, is the size the default was set to admit with headroom."""
    svg = render_svg(ERYTHROMYCIN)
    assert "<svg" in svg
    assert len(svg) < MAX_DEPICTION_CHARS


def test_the_render_size_floor_is_the_size_below_which_a_depiction_says_nothing() -> None:
    """The render-size floor is the size below which a depiction says nothing, driven against RDKit.

    `MINIMUM_RENDER_SIZE_PX` is RDKit's `minFontSize`: below it the font is clamped, so no atom
    label fits. A zero canvas returns a well-formed SVG with `viewBox` `0 0 0 0`, a successful
    answer containing nothing.
    """
    molecule = Chem.MolFromSmiles("CCO")
    Chem.rdDepictor.Compute2DCoords(molecule)

    at_the_floor = rdMolDraw2D.MolDraw2DSVG(MINIMUM_RENDER_SIZE_PX, MINIMUM_RENDER_SIZE_PX)
    at_the_floor.DrawMolecule(molecule)
    at_the_floor.FinishDrawing()
    assert at_the_floor.FontSize() == pytest.approx(MINIMUM_RENDER_SIZE_PX), (
        "the floor is meant to be the size at which the glyph is exactly the canvas; if RDKit's "
        "clamp has moved, `MINIMUM_RENDER_SIZE_PX` still follows it but this argument needs "
        "re-reading"
    )

    empty = rdMolDraw2D.MolDraw2DSVG(0, 0)
    empty.DrawMolecule(molecule)
    empty.FinishDrawing()
    assert "viewBox='0 0 0 0'" in empty.GetDrawingText(), (
        "a zero canvas is supposed to be the silent failure this floor prevents; if RDKit now "
        "refuses it outright, the floor is cheaper than it was rather than wrong"
    )


def test_the_render_size_refuses_below_its_floor_and_accepts_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The render size refuses at `minimum - 1` and accepts the floor, via the module's import.

    The fleet test drives `0` and `-1`, which a floor of 1 would also reject; the interesting value
    here is between.
    """
    monkeypatch.setenv("CHEMCLAW_CHEM_RENDER_SIZE_PX", str(MINIMUM_RENDER_SIZE_PX - 1))
    with pytest.raises(ValueError, match="CHEMCLAW_CHEM_RENDER_SIZE_PX"):
        reimported(depiction)
    monkeypatch.setenv("CHEMCLAW_CHEM_RENDER_SIZE_PX", str(MINIMUM_RENDER_SIZE_PX))
    assert reimported(depiction).RENDER_SIZE_PX == MINIMUM_RENDER_SIZE_PX
