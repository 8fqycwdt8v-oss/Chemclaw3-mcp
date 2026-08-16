"""GFN2-xTB semiempirical single-point energies.

Fast, local, deterministic single-point energies via `tblite` (GFN2-xTB) on an RDKit-embedded 3D
geometry. No HPC, sub-second on ordinary molecules.

Thin by design: `structure` owns the geometry and its validation, `xtb_spec` owns the version and
the key, and `xtb_engine` owns the SCF. What is left here is the one thing specific to a single
point — its input and result shape.

**Ported without `run_cached_xtb`.** Chemclaw3's entry point looked the answer up in the calculation
store first; this server computes on request and returns, and the key it *would* have been stored
under travels back in `calc_key` so the caller can do the storing.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from chemclaw_mcp_calc.engine.key import Keyed
from chemclaw_mcp_calc.engine.structure import Structure, structure_from_smiles
from chemclaw_mcp_calc.engine.xtb_engine import gfn2_energy
from chemclaw_mcp_calc.engine.xtb_spec import XtbSpec

__all__ = ["XtbInput", "XtbResult", "run_xtb"]


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

    The version and the key come from the *resolved* spec, so an open-shell input that
    `for_structure` sent in-process is recorded as tblite's rather than as the configured backend's.
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

    Relaxation is **required for the energy to mean anything comparative**, and the margin is not
    subtle. Measured over five textbook isomer pairs, a raw ETKDG embedding gets the sign of the
    relative energy *wrong* in two of them — isobutane vs. n-butane and ethanol vs. dimethyl ether —
    because the residual strain in an unrelaxed geometry is larger than the energy difference being
    asked about. The same geometries relaxed with MMFF get all five orderings right. Since a
    single-point energy is only ever useful relatively, an unrelaxed geometry answers the question
    wrongly rather than approximately.
    """
    return structure_from_smiles(smiles, charge=charge, optimize=True)


def run_xtb(job: XtbInput) -> XtbResult:
    """Compute a GFN2-xTB single-point energy for one molecule.

    Raises `ValueError` on an unparseable SMILES, a declared charge that contradicts the SMILES
    formal charge, an open-shell electron count, or a geometry that fails to embed, rather than
    returning a meaningless energy: tblite silently converges a wrong-charge or odd-electron system
    to an energy that can be hundreds of kcal/mol off. Those checks live in `structure.Structure`,
    so every xTB task inherits them identically.
    """
    return _energy(XtbSpec(task="sp"), _sp_structure(job.smiles, job.charge))
