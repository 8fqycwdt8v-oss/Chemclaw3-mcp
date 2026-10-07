"""CREST ensemble and complex searches, as primitives — the search, and nothing built on it.

Populations, entropy and interaction energies are arithmetic over these results and three
`relax_structure` calls, done in Chemclaw3, so every part is cached separately. A CREST search is
one stateful metadynamics run and is exposed whole: one tool, one key. Without the binary these
primitives refuse by name.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
from pydantic import Field

from chemclaw_mcp_calc.engine import crest_cli
from chemclaw_mcp_calc.engine.chem import require_canonical_smiles
from chemclaw_mcp_calc.engine.config import settings
from chemclaw_mcp_calc.engine.crest_cli import CrestEffort, CrestSearch, EnsembleMember
from chemclaw_mcp_calc.engine.structure import Structure
from chemclaw_mcp_calc.engine.xtb_spec import CrestSpec, backend_version

__all__ = [
    "ComplexSpec",
    "EnsembleSearch",
    "EnsembleSpec",
    "combine_structures",
    "ordered_pair",
    "require_crest",
    "search_ensemble",
]

# Searches over one molecule; `complex` is a search over a pair and has its own spec.
EnsembleSearch = Literal["conformers", "tautomers", "protomers", "deprotomers"]


class EnsembleSpec(CrestSpec):
    """Settings of one ensemble search over a single molecule.

    Every field enters the key through `model_dump()` — including `effort`, because a quick pass and
    an extensive one are different calculations that must not share an entry, and `temperature_k`,
    because it is passed to `crest --temp` and changes what is sampled.

    **`max_members` is not here**, and its absence is the point. In Chemclaw3 it is a field that
    `unkeyed_fields` then has to exclude, because it truncates a finished ensemble rather than
    searching. Truncation is a presentation choice made by whoever reads the result, and the reader
    is on the other side of this seam — so the field does not exist here at all, and there is
    nothing to remember to exclude.
    """

    task: Literal["conformers"] = "conformers"
    search: EnsembleSearch = "conformers"
    effort: CrestEffort = Field(default_factory=lambda: settings.crest_effort)
    temperature_k: float = Field(default_factory=lambda: settings.xtb_thermo_temperature_k, gt=0)


class ComplexSpec(CrestSpec):
    """Settings of one non-covalent complex search over an already-combined pair.

    `CrestSpec` because the search is crest's. `engine` is put *back* into the version string —
    unlike a plain ensemble search — and the reason is specific rather than defensive: on the
    Chemclaw3 side the numbers an interaction energy reports all come from the three
    `relax_structure` calls around this search, which run on `engine`. A composite keyed without it
    would let a tblite interaction energy be served to a deployment that has the xtb binary.

    Here the composite is Chemclaw3's, so this spec keys only the search — but the search's own
    result feeds those optimizations, and a caller composing them must be able to tell one
    deployment's chain from another's. Keeping `engine` in this version is what makes the whole
    chain's provenance readable from any one of its rows.
    """

    task: Literal["complex"] = "complex"
    effort: CrestEffort = Field(default_factory=lambda: settings.crest_effort)

    def calc_version(self) -> str:
        """crest's build *and* the backend the surrounding optimizations run on."""
        return f"{super().calc_version()}+{self.engine}+{backend_version(self.engine)}"


def require_crest() -> None:
    """Refuse before anything else happens when the binary is not installed.

    Called by both the compute path and the identity derivation: `calc_version()` would otherwise
    derive a `crest-absent` key addressing a row nothing can write.

    Raises:
        ValueError: naming the binary and what is unavailable without it.
    """
    if crest_cli.is_available():
        return
    raise ValueError(
        f"the {settings.crest_binary!r} binary is not installed on this server, so conformer, "
        "tautomer, protomer and non-covalent-complex sampling are unavailable. The shipped image "
        "carries it, so this deployment has replaced or trimmed that image; restoring the binary "
        "on PATH is what turns these back on"
    )


def search_ensemble(spec: EnsembleSpec | ComplexSpec, structure: Structure) -> list[EnsembleMember]:
    """Run one CREST search on `structure` and return its members, lowest energy first.

    Raises:
        ValueError: crest is not installed, or the method is not one it accepts.
        CliError: the search timed out, exited non-zero, or wrote no ensemble.
    """
    require_crest()
    search: CrestSearch = "complex"
    temperature: float | None = None
    if isinstance(spec, EnsembleSpec):
        search = spec.search
        temperature = spec.temperature_k
    return crest_cli.run(
        structure,
        search=search,
        method=spec.method,
        effort=spec.effort,
        solvent=spec.solvent,
        temperature_k=temperature,
    )


def _radius(positions: np.ndarray) -> float:
    """Distance from a centred molecule's centroid to its furthest atom."""
    return float(np.linalg.norm(positions, axis=1).max())


def combine_structures(first: Structure, second: Structure, separation: float) -> Structure:
    """Place `second` beside `first` and return the pair as one structure. Pure geometry, no SCF.

    Each monomer is centred and the second offset along x by the sum of radii plus a gap — only a
    non-overlapping start. A primitive of its own because it produces the subject a complex search
    is keyed on. Not symmetric in its arguments; use `ordered_pair` to make A+B and B+A one
    calculation.
    """
    if first.uhf or second.uhf:
        raise ValueError(
            "combining two structures where either is open-shell is refused: the pair's spin state "
            f"is a chemical decision, not arithmetic. Two doublets (here: multiplicities "
            f"{first.multiplicity} and {second.multiplicity}) are a singlet or a triplet, and this "
            "function has no basis to pick one — every energy computed on the pair, and the "
            "interaction energy built from them, would be the chosen surface's. Relax and search "
            "the monomers separately, or open a spec that states the multiplicity and keys on it"
        )
    left = np.array(first.positions)
    right = np.array(second.positions)
    left = left - left.mean(axis=0)
    right = right - right.mean(axis=0)
    offset = _radius(left) + _radius(right) + separation
    right = right + np.array([offset, 0.0, 0.0])
    return Structure(
        elements=[*first.elements, *second.elements],
        positions=[*left.tolist(), *right.tolist()],
        charge=first.charge + second.charge,
        # Two closed shells make a closed shell; open-shell monomers are refused above, since
        # `Structure` would accept any declared multiplicity consistent with the electron count.
        multiplicity=first.multiplicity + second.multiplicity - 1,
        smiles=f"{first.smiles}.{second.smiles}",
    )


def ordered_pair(smiles_a: str, smiles_b: str) -> tuple[str, str]:
    """The pair in a canonical order, so A-with-B and B-with-A are one calculation.

    Swapping the arguments to `combine_structures` gives a different start, hence a different key.
    """
    first, second = require_canonical_smiles(smiles_a), require_canonical_smiles(smiles_b)
    return (first, second) if first <= second else (second, first)
