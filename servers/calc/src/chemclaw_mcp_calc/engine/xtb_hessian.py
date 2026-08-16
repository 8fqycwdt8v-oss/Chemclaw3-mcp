"""The Hessian: the expensive half of every vibrational question, kept as its own spec.

A Hessian depends on exactly two things: the geometry, and the method that produced the second
derivatives. It does **not** depend on the temperature, the pressure, the rotational symmetry number
or the quasi-RRHO cutoff — those are arithmetic applied afterwards. `HessianSpec` is therefore
deliberately narrower than `ThermoSpec`, and that narrowness is the whole point: two thermochemistry
requests differing only in temperature project onto the *same* `HessianSpec`, so on the Chemclaw3
side the cheap answer misses and the expensive one hits.

**Ported without the artifact store.** In Chemclaw3 the 3N by 3N matrix is too large for a JSONB
result row, so it lives in a content-addressed blob store and the row holds the addresses — and a
cached row whose blob is gone counts as a miss. None of that machinery is here: this server computes
the matrix, hands it to `xtb_thermo`, and drops it. `HessianResult`, `_pack`/`_unpack`, `_load`,
`_persist` and `run_cached_hessian` are all gone with it; `compute_hessian` returns the `Hessian`
itself rather than `(hessian, files_to_store)`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
from pydantic import Field

from chemclaw_mcp_calc.engine import xtb_cli
from chemclaw_mcp_calc.engine.config import settings
from chemclaw_mcp_calc.engine.structure import Structure
from chemclaw_mcp_calc.engine.xtb_engine import AU_TO_DEBYE, evaluate_point, make_calculator
from chemclaw_mcp_calc.engine.xtb_spec import XtbSpec

__all__ = ["Hessian", "HessianSpec", "compute_hessian"]


class HessianSpec(XtbSpec):
    """Settings of one second-derivative calculation — everything that moves the matrix.

    Deliberately narrower than `ThermoSpec`: `temperature_k`, `pressure_pa`, `symmetry_number` and
    `rrho_cutoff_cm` are absent because a Hessian does not depend on them. That absence *is* the
    fix — `XtbSpec.cache_key` keys on `model_dump()`, so a field that is not here cannot force a
    recomputation.
    """

    task: Literal["hess"] = "hess"
    displacement_angstrom: float = Field(
        default_factory=lambda: settings.xtb_hessian_displacement, gt=0
    )


@dataclass(frozen=True)
class Hessian:
    """The second derivatives of one geometry, plus what the run collected alongside them.

    Not a pydantic model: it holds numpy arrays and never crosses the wire — `ThermochemistryResult`
    is what a caller receives.

    Exactly one of `ir_intensities` and `dipole_derivatives` is populated, and which one says which
    backend ran. The `xtb` binary computes intensities itself and reports one per Cartesian mode
    (translations and rotations included, which the caller reconciles); the in-process path returns
    the dipole derivatives it collected while displacing, from which intensities are derived once
    the normal modes are known.
    """

    matrix: np.ndarray
    electronic_energy_hartree: float
    ir_intensities: np.ndarray | None = None
    dipole_derivatives: np.ndarray | None = None


def _finite_difference(
    spec: HessianSpec, structure: Structure
) -> tuple[np.ndarray, np.ndarray, float]:
    """Central-difference Hessian and dipole derivatives at `structure`'s geometry.

    Returns `(hessian, dipole_derivatives, energy)` — the Hessian in Hartree/Angstrom^2, shape
    (3N, 3N), the dipole derivatives in Debye/Angstrom, shape (3N, 3), and the electronic energy at
    the undisplaced geometry.

    **The energy comes back from here because this function already holds the calculator.** The
    caller used to build a *second* calculator over the same system to get it — a second Hamiltonian
    assembly, measured at 2 per Hessian against 1 now.

    Cost is 6N + 1 single points: the gradient is analytic, so only *first* derivatives need
    differencing. The Hessian is symmetrized afterwards — central differences of an exact gradient
    give a nearly symmetric matrix, and forcing the symmetry removes the small asymmetry that would
    otherwise put a spurious imaginary component into the eigenvalues.
    """
    numbers, positions = structure.arrays()
    calc = make_calculator(
        spec.method,
        numbers,
        positions,
        charge=structure.charge,
        uhf=structure.uhf,
        solvent=spec.solvent,
    )
    size = positions.size
    hessian = np.zeros((size, size))
    dipole_derivatives = np.zeros((size, 3))
    step = spec.displacement_angstrom
    energy, _, _ = evaluate_point(calc, positions)
    for index in range(size):
        shifted = positions.copy().ravel()
        shifted[index] += step
        _, gradient_plus, dipole_plus = evaluate_point(calc, shifted.reshape(-1, 3))
        shifted[index] -= 2 * step
        _, gradient_minus, dipole_minus = evaluate_point(calc, shifted.reshape(-1, 3))
        hessian[index] = (gradient_plus.ravel() - gradient_minus.ravel()) / (2 * step)
        dipole_derivatives[index] = (dipole_plus - dipole_minus) * AU_TO_DEBYE / (2 * step)
    return 0.5 * (hessian + hessian.T), dipole_derivatives, energy


def compute_hessian(spec: HessianSpec, structure: Structure) -> Hessian:
    """The second derivatives at `structure`.

    The `xtb` binary computes both the Hessian and the IR intensities itself and is far faster at it
    — measured, a 76-atom Hessian in 26 s against 218 s of finite differences. What it does *not*
    get to supply is the thermochemistry over them: that stays in `xtb_thermo`, so the symmetry
    number remains an explicit input and the quasi-RRHO treatment is identical whichever backend
    ran, which is what keeps free energies from the two comparable.

    Raises `ValueError` above `settings.xtb_hessian_max_atoms`: the in-process cost is 6N single
    points, and blocking an agent turn for minutes is a worse failure than refusing. **The refusal
    is worded differently here than in Chemclaw3**, which named the durable QM job path as the
    alternative — this server has no durable path, and pointing at one that does not exist from here
    would send a chemist looking for a route nobody can take.
    """
    if len(structure.elements) > settings.xtb_hessian_max_atoms:
        raise ValueError(
            f"a Hessian on {len(structure.elements)} atoms exceeds this server's inline limit of "
            f"{settings.xtb_hessian_max_atoms}: the cost is 6N single points and a tool call runs "
            "inside a conversation turn. Submit it through Chemclaw3's durable QM job path instead"
        )
    if spec.for_structure(structure).engine == "xtb":
        outcome = xtb_cli.run(structure, task="hess", method=spec.method, solvent=spec.solvent)
        return Hessian(
            matrix=np.asarray(outcome.hessian),
            electronic_energy_hartree=outcome.energy_hartree,
            ir_intensities=np.asarray(outcome.ir_intensities),
        )

    matrix, dipole_derivatives, energy = _finite_difference(spec, structure)
    return Hessian(
        matrix=matrix,
        electronic_energy_hartree=energy,
        dipole_derivatives=dipole_derivatives,
    )
