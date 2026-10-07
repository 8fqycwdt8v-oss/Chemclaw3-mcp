"""What stops a call costing more than anybody agreed to pay: a size bound and a wall clock.

- **Atom bound.** `Structure` refuses above `xtb_max_atoms`. Optimizer memory is quadratic in the
  atom count while the body cap is linear, so the ceiling is derived from the pod's memory
  (`test_a_full_pod_of_calculations_at_the_ceiling_fits_the_memory_limit_it_declares`).
- **Wall clock.** The shipped image runs `opt` and `hess` in process, where the subprocess
  timeouts do not apply and `max_steps` bounds iterations, not seconds; an inline budget does.

The manifest's `request_timeout` bounds only the caller's wait: cancelling the coroutine does not
stop the worker thread, and a retry would start a second burn. These tests are the in-process
counterpart of `xtb_cli.run_isolated`'s process-group kill.
"""

from __future__ import annotations

import time
import types
from pathlib import Path
from typing import Any

import chemclaw_mcp_calc.engine.budget as budget_module
import chemclaw_mcp_calc.engine.structure as structure_module
import chemclaw_mcp_calc.engine.xtb_engine as xtb_engine
import chemclaw_mcp_calc.engine.xtb_opt as xtb_opt
import numpy as np
import pytest
import yaml
from chemclaw_mcp_calc.engine.config import settings
from chemclaw_mcp_calc.engine.pka import PkaInput, _conjugate_bases, _predict_acid_pka, predict_pka
from chemclaw_mcp_calc.engine.structure import Structure, structure_from_smiles
from chemclaw_mcp_calc.engine.xtb_engine import evaluate_point, geometry, parse_molecule
from chemclaw_mcp_calc.engine.xtb_hessian import HessianSpec, compute_hessian
from chemclaw_mcp_calc.engine.xtb_opt import OptSpec, optimize_structure
from chemclaw_mcp_calc.engine.xtb_props import PropertiesSpec, compute_properties
from mcp_server_kit.sessions import DEFAULT_MAX_SESSIONS, SESSION_COST_BYTES
from rdkit import Chem


def a_structure_of(atom_count: int) -> Structure:
    """A valid `Structure` of exactly `atom_count` atoms, built without RDKit.

    Helium rather than hydrogen: every atom is a closed shell on its own, so any count is a valid
    electron count and the size check is what refuses, not the multiplicity validator.
    """
    return Structure(
        elements=[2] * atom_count,
        positions=[[3.0 * index, 0.0, 0.0] for index in range(atom_count)],
    )


def test_a_structure_larger_than_the_ceiling_is_refused_before_any_engine_sees_it() -> None:
    """The bound is on `Structure`, so every primitive inherits it as the electron count is.

    A per-tool check is one somebody forgets. The message names the count and the limit.
    """
    with pytest.raises(ValueError, match=r"exceeds this server's limit of"):
        a_structure_of(settings.xtb_max_atoms + 1)

    at_the_limit = a_structure_of(settings.xtb_max_atoms)
    assert len(at_the_limit.elements) == settings.xtb_max_atoms


def test_the_ceiling_s_refusal_names_the_way_forward_its_own_docstring_states() -> None:
    """The atom-ceiling refusal names the remedies: a smaller system or a larger deployment.

    The `ValueError` message is all the model and chemist see. The environment variable is asserted
    by name because it is the remedy a caller cannot guess.
    """
    with pytest.raises(ValueError) as raised:
        a_structure_of(settings.xtb_max_atoms + 1)
    message = str(raised.value)
    assert "CHEMCLAW_XTB_MAX_ATOMS" in message
    assert "smaller system" in message


def test_a_smiles_over_the_ceiling_is_refused_before_it_is_embedded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SMILES path refuses at parse time, not after ETKDG and MMFF have paid for the geometry.

    The embedder is replaced by one that fails if reached, so this asserts the ordering. The count
    is hydrogen-inclusive (an n-carbon alkane is 3n + 2 atoms).
    """

    def must_not_embed(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("embedded a molecule the ceiling should have refused first")

    ceiling = settings.xtb_max_atoms
    over = "C" * ((ceiling - 2) // 3 + 1)  # 3n + 2 > ceiling
    monkeypatch.setattr(structure_module, "geometry", must_not_embed)
    with pytest.raises(ValueError, match=r"exceeds this server's limit of") as raised:
        structure_from_smiles(over)
    message = str(raised.value)
    assert f"{3 * len(over) + 2} atoms" in message
    assert "CHEMCLAW_XTB_MAX_ATOMS" in message
    assert len(message) < 1_000  # the SMILES is echoed through `limits.echo`, not verbatim

    monkeypatch.undo()
    under = "C" * ((ceiling - 2) // 3)  # 3n + 2 <= ceiling: the pre-check must let it through
    assert structure_module.atom_ceiling_error(3 * len(under) + 2, subject="x") is None


class _NoEmbedding:
    """Stands in for `xtb_engine.AllChem`: any embedding or force-field call fails the test."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"AllChem.{name} reached for a molecule the ceiling should refuse")


def _refuse_every_scf(*_args: object, **_kwargs: object) -> Any:
    raise AssertionError("a tblite calculator was built for a system the ceiling should refuse")


#: Acetic acid is 8 atoms H-inclusive (its anion 7); propionic acid is 11. With the ceiling set to
#: 8 the pair straddles it, so both directions run in milliseconds rather than the 452-atom acid's
#: minutes — the ceiling's *value* is not what these tests are about.
_ACETIC, _PROPIONIC = "CC(=O)O", "CCC(=O)O"


def test_geometry_itself_refuses_over_the_ceiling_before_embedding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`geometry()` is the one place this server embeds, so it is where the ceiling is enforced.

    Paths that never build a `Structure` (e.g. `pka`'s acid branch) cannot skip it. Hydrogens count
    whether explicit or implicit.
    """
    monkeypatch.setattr(settings, "xtb_max_atoms", 8)
    monkeypatch.setattr(xtb_engine, "AllChem", _NoEmbedding())
    for mol in (parse_molecule(_PROPIONIC), Chem.MolFromSmiles(_PROPIONIC)):
        with pytest.raises(ValueError, match=r"a molecule of 11 atoms exceeds this server's limit"):
            geometry(mol, seed=1)


def test_every_scf_refuses_over_the_ceiling() -> None:
    """`make_calculator` is where every SCF starts, so it is the backstop for coordinates that
    never passed through `Structure` or `geometry()`."""
    over = settings.xtb_max_atoms + 1
    numbers = np.full(over, 2)
    positions = np.array([[3.0 * index, 0.0, 0.0] for index in range(over)])
    with pytest.raises(ValueError, match=rf"a system of {over} atoms exceeds"):
        xtb_engine.make_calculator(settings.xtb_method, numbers, positions)


def test_a_pka_acid_over_the_ceiling_is_refused_before_it_is_embedded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`predict_pka`'s acid branch refuses an oversize acid without embedding.

    Asserted through `predict_pka` and through `_predict_acid_pka` directly, where only
    `geometry()`'s check stands before the SCF.
    """
    monkeypatch.setattr(settings, "xtb_max_atoms", 8)
    monkeypatch.setattr(xtb_engine, "AllChem", _NoEmbedding())
    monkeypatch.setattr(xtb_engine, "Calculator", _refuse_every_scf)
    with pytest.raises(ValueError, match=r"the molecule 'CCC\(=O\)O' of 11 atoms exceeds"):
        predict_pka(PkaInput(smiles=_PROPIONIC))

    acid = parse_molecule(_PROPIONIC)
    with pytest.raises(ValueError, match=r"of 11 atoms exceeds this server's limit of 8"):
        _predict_acid_pka(_PROPIONIC, acid, _conjugate_bases(acid), "v", "k")


def test_a_pka_acid_at_the_ceiling_still_predicts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The check refuses only what is over the line: an acid of exactly the ceiling computes."""
    monkeypatch.setattr(settings, "xtb_max_atoms", 8)
    result = predict_pka(PkaInput(smiles=_ACETIC))
    assert result.site == "acid"
    assert np.isfinite(result.pka)


#: This server's own Deployment, which is where the memory the ceiling is derived from is declared.
_DEPLOYMENT = Path(__file__).resolve().parents[1] / "deploy" / "deployment.yaml"

#: Peak MiB per atom squared for one in-process relaxation. Quadratic because geomeTRIC
#: eigendecomposes an (`nprim`, `nprim`) matrix with `nprim` linear in atoms. Measured out of
#: process as peak RSS on linear alkanes:
#:
#:   atoms   119     239     509
#:   peak    67.2    253.7   978.9    MiB above the process's own resident set
#:   /N**2   4.74e-3 4.44e-3 3.78e-3
#:
#: The value near the ceiling is the one that bounds it.
PEAK_MIB_PER_ATOM_SQUARED = 3.78e-3

#: What a finished calculation does not give back: glibc keeps the arena, and a slot is released at
#: completion, so a steady-state slot is charged 6% above a cold call's peak.
RETAINED_ARENA_FACTOR = 1.06

#: The server process's own resident set before any calculation, measured on the real app and
#: rounded up.
SERVER_RESIDENT_MIB = 200

#: The densest primitive set geomeTRIC builds per atom over realistic shapes (linear alkane is the
#: densest). The memory coefficient assumes it; a denser set would move the peak by the square of
#: this ratio.
PRIMITIVES_PER_ATOM = 9.0


def _container_memory_limit_mib() -> int:
    """The `limits.memory` this server's own Deployment declares, in MiB."""
    manifest = yaml.safe_load(_DEPLOYMENT.read_text(encoding="utf-8"))
    containers = manifest["spec"]["template"]["spec"]["containers"]
    declared = str(containers[0]["resources"]["limits"]["memory"])
    assert declared.endswith("Gi"), f"unhandled memory unit in {declared!r}"
    return int(declared[:-2]) * 1024


def _peak_mib_for(atoms: int) -> float:
    """What one admitted slot costs the pod at `atoms` atoms, retention included."""
    return RETAINED_ARENA_FACTOR * PEAK_MIB_PER_ATOM_SQUARED * atoms**2


def test_a_full_pod_of_calculations_at_the_ceiling_fits_the_memory_limit_it_declares() -> None:
    """A full pod of calculations at the atom ceiling fits the memory limit it declares.

    An inequality over four declared numbers: the container's `limits.memory`,
    `calc_max_concurrent_requests`, the kit's session backlog budget, and one relaxation's peak.
    `xtb_max_atoms` sits a margin under the bound because every constant carries a few percent.
    Changing any of them past the bound fails here instead of in an OOMKill.
    """
    limit = _container_memory_limit_mib()
    sessions_mib = DEFAULT_MAX_SESSIONS * SESSION_COST_BYTES / 1024**2
    concurrent = settings.calc_max_concurrent_requests
    needed = SERVER_RESIDENT_MIB + sessions_mib + concurrent * _peak_mib_for(settings.xtb_max_atoms)
    assert needed <= limit, (
        f"{concurrent} concurrent relaxations at {settings.xtb_max_atoms} atoms need "
        f"{needed:.0f} MiB — the resident set ({SERVER_RESIDENT_MIB} MiB) and the session backlog "
        f"({sessions_mib:.0f} MiB) included — against the {limit} MiB this pod's Deployment "
        "declares. That is an OOMKill, and it takes the four-hour CREST search beside it too"
    )


def test_the_primitive_set_geometric_builds_is_still_what_that_bound_assumes() -> None:
    """The primitive set geomeTRIC builds is still what the memory bound assumes.

    The coefficient cannot be re-measured in-process (`getrusage` is a high-water mark;
    `tracemalloc` misses LAPACK workspace), so its basis is guarded instead: the primitive density,
    driven through the optimizer's own `_coordinate_system`.
    """
    structure = structure_from_smiles("C" * 39)
    numbers, positions = structure.arrays()
    molecule = xtb_opt._geometric_molecule(numbers, positions)
    coordinates: Any = xtb_opt._coordinate_system(molecule, ())
    density = len(coordinates.Prims.Internals) / len(numbers)
    assert density <= PRIMITIVES_PER_ATOM, (
        f"geomeTRIC builds {density:.2f} primitives per atom where the atom ceiling was derived "
        f"against {PRIMITIVES_PER_ATOM}. The peak goes as the square of this, so "
        f"`PEAK_MIB_PER_ATOM_SQUARED` needs re-measuring before `xtb_max_atoms` can be believed"
    )


def test_the_hessian_cap_is_the_tighter_bound_inside_the_general_one() -> None:
    """Two bounds, and the tighter one still bites first where it applies.

    A Hessian is 6N + 1 single points, so its cap is far below the general ceiling. The general
    bound does not make anything under it affordable; the wall clock prices the work.
    """
    assert settings.xtb_hessian_max_atoms < settings.xtb_max_atoms


def test_an_oversized_structure_cannot_be_smuggled_in_as_a_tool_argument() -> None:
    """An oversized structure cannot be smuggled in as a tool argument.

    `Structure.model_validate` runs on every incoming dict, so the ceiling holds at the JSON
    boundary.
    """
    payload = a_structure_of(settings.xtb_max_atoms).model_dump()
    payload["elements"] = [*payload["elements"], 2]
    payload["positions"] = [*payload["positions"], [1.0, 1.0, 1.0]]
    with pytest.raises(ValueError, match=r"exceeds this server's limit of"):
        Structure.model_validate(payload)


def test_the_optimizer_stops_when_the_inline_budget_is_spent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wall clock on the in-process optimisation, checked where the cost is: per gradient.

    A single optimizer cycle is unbounded in seconds. A microsecond budget proves the clock is
    consulted. A `ValueError`, so the message reaches the model verbatim.
    """
    monkeypatch.setattr(settings, "xtb_inline_timeout_seconds", 1e-6)
    water = structure_from_smiles("O")
    with pytest.raises(ValueError, match=r"exceeded this server's inline budget") as stopped:
        optimize_structure(OptSpec(engine="tblite"), water)
    # A `TimeBudgetError` opening with the marker, so a caller can tell this stop from a refusal of
    # the input; and the literal pinned here, because Chemclaw3 matches its own copy of it.
    assert isinstance(stopped.value, budget_module.TimeBudgetError)
    assert str(stopped.value).startswith(budget_module.TIME_BUDGET_MARKER)
    assert budget_module.TIME_BUDGET_MARKER == "[calc-time-budget]"


def test_a_relaxation_the_budget_stops_says_how_far_it_got(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal reports the gradients it completed and the gradient it was left at.

    That separates a run one cycle from converging from one nowhere near. The clock is replaced so
    the second gradient past the input is refused, and the figures are checked against the gradient
    actually evaluated last.
    """
    # A water with one bond stretched, so the optimizer certainly runs past its first gradient.
    strained = Structure(
        elements=[8, 1, 1],
        positions=[[0.0, 0.0, 0.0], [1.3, 0.0, 0.0], [-0.3, 0.9, 0.0]],
    )
    real_clock = time.monotonic
    checks = {"count": 0}

    def clock() -> float:
        # `Deadline.check` reads `elapsed` once to decide; the first decision passes and every
        # later one finds the budget spent.
        checks["count"] += 1
        return real_clock() + (0.0 if checks["count"] <= 1 else 1e9)

    evaluated: list[np.ndarray] = []

    def spy(calculator: Any, positions: np.ndarray) -> Any:
        answer = evaluate_point(calculator, positions)
        evaluated.append(np.asarray(answer[1], dtype=float))
        return answer

    monkeypatch.setattr(budget_module, "time", types.SimpleNamespace(monotonic=clock))
    monkeypatch.setattr(xtb_opt, "evaluate_point", spy)
    spec = OptSpec(engine="tblite")
    with pytest.raises(ValueError, match=r"exceeded this server's inline budget") as refused:
        optimize_structure(spec, strained)

    message = str(refused.value)
    assert len(evaluated) == 2, f"expected the input and one gradient past it, got {len(evaluated)}"
    assert "stopped after 1 gradient evaluation past the input geometry" in message, message
    reached = float(np.max(np.abs(evaluated[-1])))
    assert reached > spec.gradient_tolerance, "the relaxation had converged, so this proves nothing"
    assert f"max |gradient| {reached:.2e} Hartree/Angstrom" in message, message
    assert f"against the {spec.gradient_tolerance:.2e} it had to reach" in message, message


def test_the_finite_difference_hessian_stops_when_the_inline_budget_is_spent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same clock on the finite-difference Hessian, the more expensive loop.

    At the atom cap it would outlast the caller's timeout, so the budget must expire first.
    """
    monkeypatch.setattr(settings, "xtb_inline_timeout_seconds", 1e-6)
    water = structure_from_smiles("O")
    with pytest.raises(ValueError, match=r"exceeded this server's inline budget"):
        compute_hessian(HessianSpec(engine="tblite"), water)


#: What Chemclaw3 waits for each tier of this server (`core/config/calculators.py`), written as
#: literals because that repository is not installed here and the caller's own number is the point.
CHEMCLAW3_CALLER_BUDGETS = {
    "calc_server_timeout_seconds": 900.0,
    "calc_atomic_timeout_seconds": 3600.0,
    "calc_sampling_timeout_seconds": 14400.0,
}


def test_every_budget_here_is_strictly_tighter_than_the_bound_its_caller_waits() -> None:
    """Every budget here is strictly tighter than what its caller waits.

    The caller's clock starts first, so an equal budget means the caller always expires first and
    the worded refusal is unreachable, while the pod keeps computing. Checked as a strict inequality
    with a minimum margin: the numbers are a deployment's to change, the ordering is not.
    """
    margin = 120  # `config._CALLER_MARGIN_SECONDS`, and its derivation is in that comment.
    for name, budget in (
        ("calc_server_timeout_seconds", settings.xtb_inline_timeout_seconds),
        ("calc_atomic_timeout_seconds", float(settings.xtb_cli_timeout_seconds)),
        ("calc_sampling_timeout_seconds", float(settings.crest_timeout_seconds)),
    ):
        caller = CHEMCLAW3_CALLER_BUDGETS[name]
        assert budget <= caller - margin, (
            f"this server allows {budget:g} s where Chemclaw3's {name} waits {caller:g} s. The "
            f"caller's clock starts first, so anything above {caller - margin:g} s means its "
            "timeout wins and this server's refusal never reaches the chemist"
        )


def test_the_margin_covers_one_uninterruptible_single_point() -> None:
    """The margin covers one uninterruptible single point.

    `budget.Deadline` is checked between units of work, so a spent budget is noticed up to one
    single point late. Asserted against `xtb_max_atoms` as an inequality: the measured single-point
    time at a given size bounds every ceiling at or below it, so raising the ceiling past it needs a
    new measurement. The geomeTRIC coordinate build is smaller than a single point at every size
    measured.
    """
    worst_single_point_seconds = 81.0
    measured_at_atoms = 493
    assert settings.xtb_max_atoms <= measured_at_atoms, (
        f"the {worst_single_point_seconds:g} s figure is one single point at {measured_at_atoms} "
        "atoms; a ceiling above that needs a re-measurement, not a re-reading of this comment"
    )
    margin = CHEMCLAW3_CALLER_BUDGETS["calc_server_timeout_seconds"] - (
        settings.xtb_inline_timeout_seconds
    )
    assert margin >= worst_single_point_seconds


def test_the_budget_does_not_reach_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refusal is not a result, so the clock is a setting rather than a spec field.

    Keying on the budget would fork the cache every time a deployment gave itself more time.
    """
    water = structure_from_smiles("O")
    monkeypatch.setattr(settings, "xtb_inline_timeout_seconds", 900.0)
    before = PropertiesSpec().cache_key(water)
    monkeypatch.setattr(settings, "xtb_inline_timeout_seconds", 60.0)
    assert PropertiesSpec().cache_key(water).as_str() == before.as_str()


def test_a_single_point_under_the_ceiling_still_runs() -> None:
    """The bounds refuse the unaffordable and nothing else.

    A ceiling written backwards would refuse everything and still pass every refusal test.
    """
    result = compute_properties(PropertiesSpec(), structure_from_smiles("O"))
    assert result.total_energy_hartree < 0


def test_an_exceeded_inline_budget_says_which_loop_spent_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exceeded inline budget says which loop spent it.

    Hessian cost grows with the molecule and optimisation with the surface, so an operator needs to
    know which. `what` is a literal at the call site, bounded by this repository, so it is safe as a
    label. Both loops are driven, since two series prove the label separates.
    """
    from chemclaw_mcp_calc.engine.metrics import INLINE_BUDGET_EXCEEDED

    before = {
        what: INLINE_BUDGET_EXCEEDED.labels(what)._value.get()
        for what in ("geometry optimization", "Hessian")
    }
    monkeypatch.setattr(settings, "xtb_inline_timeout_seconds", 1e-6)
    water = structure_from_smiles("O")
    with pytest.raises(ValueError):
        optimize_structure(OptSpec(engine="tblite"), water)
    with pytest.raises(ValueError):
        compute_hessian(HessianSpec(engine="tblite"), water)

    for what, was in before.items():
        assert INLINE_BUDGET_EXCEEDED.labels(what)._value.get() == was + 1, (
            f"the inline-budget counter did not separate {what!r}; an operator cannot tell which "
            "budget is undersized"
        )


def test_the_body_cap_admits_far_more_atoms_than_the_ceiling_at_every_formatting() -> None:
    """The body cap admits far more atoms than the ceiling at every formatting.

    Bytes per atom depend on the caller's number formatting, so this asserts the range rather than a
    figure: every point in it is far above `xtb_max_atoms`, which is why the atom ceiling must
    exist.
    """
    import json

    from mcp_server_kit.app import DEFAULT_MAX_REQUEST_BYTES

    # Deterministic rather than sampled: what is being measured is the *length* of a serialized
    # coordinate, and a fixed cycle of magnitudes spans the same lengths a real payload does
    # without making the assertion depend on a seed.
    magnitudes = (-28.0, -3.5, -0.25, 0.0, 1.75, 9.5, 27.0)
    atoms = 10_000
    for decimals, elements in ((1, ("C",)), (3, ("C",)), (6, ("C", "N", "O", "Cl", "Fe"))):
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "relax_structure",
                "arguments": {
                    "structure": {
                        "elements": [elements[i % len(elements)] for i in range(atoms)],
                        "positions": [
                            [
                                round(magnitudes[(3 * i + axis) % len(magnitudes)] / 3, decimals)
                                for axis in range(3)
                            ]
                            for i in range(atoms)
                        ],
                        "charge": 0,
                        "multiplicity": 1,
                    }
                },
            },
        }
        per_atom = len(json.dumps(payload, separators=(",", ":")).encode()) / atoms
        fits = int(DEFAULT_MAX_REQUEST_BYTES // per_atom)
        assert fits > settings.xtb_max_atoms * 10, (
            f"a {decimals}-decimal payload costs {per_atom:.1f} bytes an atom, so the body cap "
            f"carries {fits:,} atoms against a ceiling of {settings.xtb_max_atoms} — if that stops "
            "being a wide margin, the transport bound has become the real ceiling and "
            "`Structure`'s own is no longer what protects the optimizer"
        )


@pytest.mark.parametrize("program", ["xtb", "crest"])
def test_a_subprocess_the_clock_killed_is_a_time_budget_stop_not_an_internal_fault(
    monkeypatch: pytest.MonkeyPatch, program: str
) -> None:
    """A CLI run killed by its timeout is a time-budget stop, not an internal fault.

    As an internal error Chemclaw3 would retry it against the same clock. Driven at the raise with
    the kill stubbed; `test_process_isolation.py` makes the kill real.
    """
    import subprocess

    import chemclaw_mcp_calc.engine.crest_cli as crest_cli
    import chemclaw_mcp_calc.engine.xtb_cli as xtb_cli

    def killed(argv: list[str], **_: Any) -> None:
        raise subprocess.TimeoutExpired(argv, 1.0)

    module = xtb_cli if program == "xtb" else crest_cli
    monkeypatch.setattr(module, "run_isolated", killed)
    structure = structure_from_smiles("CCO")
    with pytest.raises(budget_module.TimeBudgetError) as stopped:
        if program == "xtb":
            monkeypatch.setattr(xtb_cli, "require_binary_path", lambda: "/usr/bin/xtb")
            xtb_cli.run(structure, task="sp", method="GFN2-xTB")
        else:
            monkeypatch.setattr(crest_cli, "binary_path", lambda: "/usr/bin/crest")
            crest_cli.run(structure, search="conformers", method="GFN2-xTB")
    assert str(stopped.value).startswith("[calc-time-budget] ")
    assert f"{program} " in str(stopped.value) and "timed out after" in str(stopped.value)
