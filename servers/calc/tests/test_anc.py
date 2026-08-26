"""The ANC preconditioner: the physics of the model, and that it actually helps.

Ported from Chemclaw3's own `tests/test_anc.py` (deleted there at `ab3da835`, replaced nowhere —
this repo now owns `engine/anc.py` and had no test naming it, `model_hessian`, or the coordinate
basis at all; only integration-level coverage of `xtb_opt.optimize_structure` survived, which
catches a sign error or broken orthonormality only if it also breaks convergence).

The speed claim is measured here rather than asserted in a comment, because the first version of
this preconditioner made optimization *slower* and only a measurement said so — see the module
docstring of `engine/anc.py` for the full table this pins a slice of.
"""

from __future__ import annotations

import numpy as np
import pytest
from chemclaw_mcp_calc.engine import anc
from chemclaw_mcp_calc.engine.config import settings
from chemclaw_mcp_calc.engine.structure import structure_from_smiles
from chemclaw_mcp_calc.engine.xtb_opt import OptSpec, optimize_structure

# The floor `OptSpec.curvature_floor` defaults to. An argument to `basis` rather than a `settings`
# read inside it, because it steers the geometry and therefore belongs in the optimisation's key —
# so a test of the basis has to say which floor it is testing, exactly as the optimizer now does.
FLOOR = settings.xtb_anc_curvature_floor


def test_the_model_hessian_is_symmetric_and_has_no_net_force() -> None:
    """Two properties any Hessian must have, and the assembly is easy to get wrong.

    Translating the whole molecule costs nothing, so every 3x3 row-block must sum to
    zero — which is exactly what the +=/-= pattern of the pairwise assembly guarantees,
    and exactly what a sign error would break.
    """
    structure = structure_from_smiles("CCO", optimize=True)
    numbers, positions = structure.arrays()
    hessian = anc.model_hessian(numbers, positions)

    assert hessian.shape == (3 * len(numbers), 3 * len(numbers))
    assert np.allclose(hessian, hessian.T)
    blocks = hessian.reshape(len(numbers), 3, len(numbers), 3).sum(axis=2)
    assert np.allclose(blocks, 0.0, atol=1e-10)


def test_stiff_directions_come_out_stiffer_than_soft_ones() -> None:
    """The whole point: the model must separate a bond stretch from everything else.

    A preconditioner that ranked them equally would be a uniform rescaling and would do
    nothing at all.
    """
    structure = structure_from_smiles("CCO", optimize=True)
    numbers, positions = structure.arrays()
    eigenvalues = np.linalg.eigvalsh(anc.model_hessian(numbers, positions))
    assert eigenvalues.max() > 100 * max(float(np.median(eigenvalues)), 1e-6)


def test_the_basis_spans_only_the_free_coordinates() -> None:
    """A frozen atom is excluded by construction, not by a constraint to be respected.

    This is what lets a constrained optimization (`xtb_opt`'s `frozen_atoms`) hold its
    coordinate: a step in this basis simply cannot move a frozen atom, so there is
    nothing for the optimizer to violate.
    """
    structure = structure_from_smiles("CCO", optimize=True)
    numbers, positions = structure.arrays()
    free = np.repeat([False, False, True, True, True, True, True, True, True], 3)
    vectors, scale = anc.basis(numbers, positions, free, FLOOR)

    assert vectors.shape == (int(free.sum()), int(free.sum()))
    assert scale.shape == (int(free.sum()),)
    assert np.all(scale > 0)
    # Orthonormal: it is an eigenbasis, so a step decomposes without cross-talk.
    assert np.allclose(vectors.T @ vectors, np.eye(vectors.shape[1]), atol=1e-10)


def test_a_softer_direction_is_allowed_a_longer_step() -> None:
    """`scale` is the inverse square root of curvature — that is the preconditioning."""
    structure = structure_from_smiles("CCO", optimize=True)
    numbers, positions = structure.arrays()
    free = np.ones(3 * len(numbers), dtype=bool)
    hessian = anc.model_hessian(numbers, positions)
    eigenvalues = np.linalg.eigvalsh(hessian[np.ix_(free, free)])
    _, scale = anc.basis(numbers, positions, free, FLOOR)
    # Eigenvalues ascend, so the last is stiffest and must get the smallest scale.
    assert scale[-1] < scale[0]
    assert scale[-1] == pytest.approx(1.0 / np.sqrt(eigenvalues[-1]), rel=1e-6)


@pytest.mark.parametrize(
    ("smiles", "frozen", "cartesian_steps"),
    [
        ("COc1ccc2cc(ccc2c1)C(C)C(=O)O", (), 44),
        ("CC(C)Cc1ccc(cc1)C(C)C(=O)O", (), 71),
        ("CC(C)Cc1ccc(cc1)C(C)C(=O)O", (0, 1), 57),
    ],
    ids=["naproxen", "ibuprofen", "ibuprofen-with-frozen-atoms"],
)
def test_preconditioning_beats_plain_cartesian_optimization(
    smiles: str, frozen: tuple[int, ...], cartesian_steps: int
) -> None:
    """Measured against the step counts an *unpreconditioned* Cartesian optimizer needed.

    `cartesian_steps` is not reproducible in this repository — the unpreconditioned code path
    Chemclaw3 measured it against was never ported here, only the preconditioned one was — so these
    three literals are carried over unchanged from Chemclaw3's own recording, made before the
    preconditioner existed (see `engine/anc.py`'s module docstring, which quotes the same numbers).
    What this test actually exercises is exclusively this repository's code: that
    `optimize_structure` with the ANC preconditioner converges naproxen and ibuprofen in
    meaningfully fewer steps than that fixed, pre-preconditioner baseline.

    The frozen-atom case is not decoration: relaxed scans are one constrained optimization per
    point, and with the `xtb` binary unable to freeze atoms without a control file (`xtb_cli`
    refuses that input surface) they are the reason this preconditioner exists at all. Forcing
    `engine="tblite"` is what routes here regardless of whether `xtb` is on the image — this image
    ships without it (see `servers/calc/README.md`), so it is also what every deployed call takes.

    The margin is real but not enormous (~2x on this repo's measured step counts of 19/24/27), so
    the assertion is that it is *clearly* better rather than better by a hair — a change that
    regressed it to parity would fail, and one that quietly disabled it would fail hard.
    """
    structure = structure_from_smiles(smiles, multiplicity=None, optimize=True)
    result = optimize_structure(OptSpec(engine="tblite", frozen_atoms=frozen), structure)
    assert result.steps < 0.75 * cartesian_steps
    assert result.max_gradient is not None  # only GFN-FF may omit it
    assert result.max_gradient <= OptSpec().gradient_tolerance


def test_preconditioning_finds_the_same_minimum() -> None:
    """Speed is worthless if it changes the answer.

    The preconditioner is a change of variables, not of surface, so it must land on the
    same stationary point to well inside chemical significance.
    """
    structure = structure_from_smiles("CCO", optimize=True)
    result = optimize_structure(OptSpec(engine="tblite"), structure)
    reoptimized = optimize_structure(OptSpec(engine="tblite"), result.structure)
    assert reoptimized.energy_hartree == pytest.approx(result.energy_hartree, abs=1e-6)
    assert reoptimized.relaxation_kcal == pytest.approx(0.0, abs=1e-3)
