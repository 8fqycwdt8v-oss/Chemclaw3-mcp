"""GFN2-xTB semiempirical single-point energies via `tblite` on an RDKit-embedded geometry.

`structure` owns the geometry, `xtb_spec` the version and key, `xtb_engine` the SCF; this module
owns only the single point's input and result shape. Nothing is cached here: the key travels back
in `calc_key` and the caller stores the result.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from chemclaw_mcp_calc.engine.key import Keyed
from chemclaw_mcp_calc.engine.structure import Structure, structure_from_smiles
from chemclaw_mcp_calc.engine.xtb_engine import gfn2_energy
from chemclaw_mcp_calc.engine.xtb_spec import XtbSpec

__all__ = ["XtbInput", "XtbResult", "run_xtb", "sp_inputs"]


class XtbInput(BaseModel):
    """A single-point xTB request: a molecule and its charge.

    `charge` is redundant with the SMILES — it is validated against the formal charge the structure
    already carries, so it cannot disagree. It is kept anyway, deliberately: the LLM tool signature
    stays loud, and a model that passes a charge contradicting the structure gets an error instead
    of having its argument silently ignored.
    """

    smiles: str = Field(min_length=1)
    charge: int = 0


class XtbResult(Keyed):
    """The parsed result of a GFN2-xTB single point, with the version that produced it."""

    smiles: str
    method: str
    charge: int
    total_energy_hartree: float


def _energy(spec: XtbSpec, structure: Structure) -> XtbResult:
    """Compute one single-point energy for an already-validated structure.

    Version and key come from the *resolved* spec, so an input routed in-process is recorded as
    tblite's.
    """
    resolved = spec.for_structure(structure)
    numbers, positions = structure.arrays()
    return XtbResult(
        calc_version=resolved.calc_version(),
        calc_key=resolved.cache_key(structure).as_str(),
        smiles=structure.smiles or "",
        method=resolved.method,
        charge=structure.charge,
        total_energy_hartree=gfn2_energy(
            resolved.method,
            numbers,
            positions,
            charge=structure.charge,
            solvent=resolved.solvent,
        ),
    )


def _sp_structure(smiles: str, charge: int) -> Structure:
    """Embed the geometry a single point runs on: MMFF-relaxed where parametrized.

    Required: an unrelaxed ETKDG geometry's residual strain can exceed the energy difference being
    compared and flip the sign of a relative energy.
    """
    return structure_from_smiles(smiles, charge=charge, optimize=True)


def sp_inputs(job: XtbInput) -> tuple[XtbSpec, Structure]:
    """The settings and the geometry one single point runs on — the pair its *identity* is made of.

    Shared by `run_xtb` and `identity.calculation_identity` so the key cannot drift between them
    (`tests/test_calculation_key.py`).
    """
    return XtbSpec(task="sp"), _sp_structure(job.smiles, job.charge)


def run_xtb(job: XtbInput) -> XtbResult:
    """Compute a GFN2-xTB single-point energy for one molecule.

    Raises `ValueError` on an unparseable SMILES, a contradicting charge, an inconsistent electron
    count, or a failed embedding (checked in `structure.Structure`), since tblite would otherwise
    converge a wrong system to a badly wrong energy.
    """
    return _energy(*sp_inputs(job))
