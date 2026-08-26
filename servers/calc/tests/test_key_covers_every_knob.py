"""Every setting that moves a number moves the key — asserted per setting, not per review.

`xtb_spec.py`'s module docstring states the rule this file enforces: "someone adds a knob and
forgets to key on it, and the next run silently serves a result computed under the old setting". The
`model_dump()` derivation makes a *spec field* keyed by construction; it does nothing at all for a
setting a compute path reads out of `settings` directly, and three did.

**Measured before the fix**, same input structure, same key, different geometry and different
energy:

    floor=1.0    key=xtb.opt@…:389b625b3220108a:5e9dada5819590e9  E=-11.394329102251229  steps=9
    floor=0.005  key=xtb.opt@…:389b625b3220108a:5e9dada5819590e9  E=-11.394339129461754  steps=25

That is a wrong-answer cache on the caller's side, which is the worst thing this seam can produce: a
`relax_structure` row written by one pod is served to another whose configuration would never have
produced that geometry, and `structure_id` is the `input_hash` of every downstream Hessian,
properties and scan key — so the fork propagates through the whole chain.

**The tests are written as pairs, and both directions are load-bearing.** A knob that reaches the
calculation must move the key (a false hit is a wrong answer); a knob that *cannot* reach it must
not (a false miss is CPU spent for nothing, and `xtb.sp`'s key is pinned against Chemclaw3 by
`test_key_contract.py`). Which of the two a knob is depends on the resolved backend, because that is
what decides whether the binary or the in-process library runs — so `unkeyed_fields` reads the
resolved engine and these tests read it with it.
"""

from __future__ import annotations

import pytest
from chemclaw_mcp_calc.engine.config import settings
from chemclaw_mcp_calc.engine.crest_search import EnsembleSpec
from chemclaw_mcp_calc.engine.structure import Structure
from chemclaw_mcp_calc.engine.xtb_hessian import HessianSpec
from chemclaw_mcp_calc.engine.xtb_opt import OptSpec
from chemclaw_mcp_calc.engine.xtb_spec import XtbSpec

# A geometry cheap enough to build in a loop: no RDKit embedding, no SCF, and every key derivation
# below is pure hashing over it.
WATER = Structure(
    elements=[8, 1, 1],
    positions=[[0.0, 0.0, 0.0], [0.96, 0.0, 0.0], [-0.24, 0.93, 0.0]],
    smiles="O",
)


def test_the_anc_curvature_floor_moves_the_optimisation_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The floor is the stand-in for the terms the pairwise model cannot see — it moves the answer.

    `anc.basis` used to read it from `settings` inside the optimizer loop, which put it in no key at
    all. Measured on ethanol, 1.0 and 0.005 relax to different geometries and different energies,
    so it is exactly the case `OptSpec.trust_radius`'s own comment describes: "it moves the answer
    and a setting that moves the answer belongs in the key".
    """
    monkeypatch.setattr(settings, "xtb_anc_curvature_floor", 1.0)
    default = OptSpec(engine="tblite").cache_key(WATER)
    monkeypatch.setattr(settings, "xtb_anc_curvature_floor", 0.005)
    tuned = OptSpec(engine="tblite").cache_key(WATER)
    assert default.params_hash != tuned.params_hash


def test_the_ancopt_convergence_level_moves_the_binary_optimisation_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--opt <level>` is xtb's own convergence criterion, so it decides where the run stops."""
    monkeypatch.setattr(settings, "xtb_cli_opt_level", "vtight")
    tight = OptSpec(engine="xtb").cache_key(WATER)
    monkeypatch.setattr(settings, "xtb_cli_opt_level", "crude")
    crude = OptSpec(engine="xtb").cache_key(WATER)
    assert tight.params_hash != crude.params_hash


@pytest.mark.parametrize("task", ["atomic", "surface"])
def test_the_cli_accuracy_moves_the_key_of_every_calculation_the_binary_runs(
    task: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--acc` scales xtb's SCF and integral thresholds, so it produces the numbers being stored.

    The per-atom panel *is* those numbers — charges, coordination numbers, C6 coefficients and
    polarisabilities — and the surface potential is a grid computed under the same threshold. Both
    tasks are binary-only (`_FIXED_BACKEND`), so there is no configuration in which the knob is
    inert here.
    """
    monkeypatch.setattr(settings, "xtb_cli_accuracy", 1.0)
    loose = XtbSpec(task=task, engine="xtb").cache_key(WATER)  # type: ignore[arg-type]
    monkeypatch.setattr(settings, "xtb_cli_accuracy", 0.05)
    tight = XtbSpec(task=task, engine="xtb").cache_key(WATER)  # type: ignore[arg-type]
    assert loose.params_hash != tight.params_hash


def test_the_cli_accuracy_moves_a_hessian_key_only_where_the_binary_takes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Hessian dispatches, so the same knob is keyed on one backend and inert on the other.

    Both halves in one test because they are one statement: the key names what runs. The in-process
    finite-difference path never passes `--acc` to anything — tblite has no such knob — so keying on
    it there would recompute every stored Hessian for a setting that could not have touched it.
    """
    monkeypatch.setattr(settings, "xtb_cli_accuracy", 1.0)
    binary_loose = HessianSpec(engine="xtb").cache_key(WATER)
    library_loose = HessianSpec(engine="tblite").cache_key(WATER)
    monkeypatch.setattr(settings, "xtb_cli_accuracy", 0.05)
    assert HessianSpec(engine="xtb").cache_key(WATER).params_hash != binary_loose.params_hash
    assert HessianSpec(engine="tblite").cache_key(WATER).params_hash == library_loose.params_hash


def test_a_knob_no_backend_of_this_calculation_reads_stays_out_of_its_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`sp`, `properties` and `fukui` are in-process whatever is configured, and crest reads none.

    The first three are pinned to tblite by `_FIXED_BACKEND`, so `--acc` cannot reach them; a CREST
    search shells out to `crest`, which this server hands no accuracy flag. Asserted rather than
    assumed, because `xtb.sp`'s key is pinned byte-for-byte against Chemclaw3's own derivation in
    `test_key_contract.py` — over-keying it would break the one contract this repository cannot
    check from the inside.
    """
    monkeypatch.setattr(settings, "xtb_cli_accuracy", 1.0)
    before = [XtbSpec(task=task).cache_key(WATER) for task in ("sp", "properties", "fukui")]
    before.append(EnsembleSpec(engine="xtb").cache_key(WATER))
    monkeypatch.setattr(settings, "xtb_cli_accuracy", 0.05)
    after = [XtbSpec(task=task).cache_key(WATER) for task in ("sp", "properties", "fukui")]
    after.append(EnsembleSpec(engine="xtb").cache_key(WATER))
    assert [key.as_str() for key in before] == [key.as_str() for key in after]


def test_a_frozen_atom_optimisation_is_keyed_as_the_backend_that_really_runs_it() -> None:
    """The binary cannot hold an atom fixed, so a constrained spec resolves in-process.

    `_optimize_with_binary` falls back to the Cartesian path whenever `frozen_atoms` is set — that
    is the only way frozen atoms work at all — but the fallback happened *after* the key was
    derived, so a scan point on a deployment with the binary was stored under a `calc_version`
    naming a program that had not run. `for_structure`'s whole job is to answer "what will actually
    run", and this is the third thing it has to answer it about, beside the fixed-backend tasks and
    the open-shell fallback.
    """
    free = OptSpec(engine="xtb")
    constrained = OptSpec(engine="xtb", frozen_atoms=(0,))
    assert free.for_structure(WATER).engine == "xtb"
    assert constrained.for_structure(WATER).engine == "tblite"
    assert "tblite" in constrained.cache_key(WATER).calc_version
    assert "+xtb+" not in constrained.cache_key(WATER).calc_version


def test_one_solvent_spelled_five_ways_is_one_calculation() -> None:
    """The name is matched case- and whitespace-insensitively, then hashed *as written*.

    So `"water"`, `"Water"` and `" water"` were three cache rows for one ALPB calculation, and
    `"h2o"` — the same entry in tblite's own table — was a fourth. On this server's cost profile
    that is the expensive kind of waste: a solvated Hessian or CREST search is minutes to hours,
    paid again for a number already on disk.
    """
    keys = {
        XtbSpec(task="properties", solvent=name).cache_key(WATER).params_hash
        for name in ("water", "Water", "WATER", " water", "h2o")
    }
    assert len(keys) == 1
