"""CREST ensemble and complex searches, as primitives — the search, and nothing built on it.

Chemclaw3's `conformers.py` and `complexes.py` each wrap one CREST call in arithmetic: Boltzmann
populations and a conformational entropy for the first, a three-optimization interaction energy for
the second. **Only the CREST call is here.** The arithmetic is pure Python over energies and
degeneracies, the interaction energy is a subtraction over three `relax_structure` results, and both
stayed in Chemclaw3 with the durable jobs that report them. What crosses the wire is what only the
binary can produce.

That split is what makes the pieces cacheable. Chemclaw3's `xtb.complex` row today is one entry for
"embed A, relax A, embed B, relax B, combine, search, relax the best mode" — so changing the
separation, or asking about A with a different partner, recomputes every optimization. Composed from
primitives, each monomer relaxation is its own row and is shared with every other question about
that molecule.

**A CREST search itself cannot be decomposed and is exposed whole.** It is a metadynamics
trajectory: the sampling is a single stateful run, its intermediate structures are not answers, and
there is no point at which half of it is a result. One tool, one key, minutes to hours.

**The binary is absent here and absent in Chemclaw3** (`which crest` is empty on both sides today),
so these primitives refuse identically wherever they run. That is parity rather than a regression —
nothing that worked before stops working — and it becomes live when an operator adds the binary.
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

# The four searches over one molecule. `complex` is excluded deliberately: it is a search over a
# *pair* and carries its own spec, because the three optimizations around it run on `engine` and
# therefore belong in its version string.
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

    Called by the compute path **and** by the identity derivation, which is the part worth stating:
    `CrestSpec.calc_version()` answers `crest-absent` rather than raising, so a key *is* derivable
    with no binary — and it would name a program that cannot run, addressing a row nothing will ever
    write. A probe that cannot be acted on is worse than a refusal, so both refuse together.

    Raises:
        ValueError: naming the binary and what is unavailable without it.
    """
    if crest_cli.is_available():
        return
    raise ValueError(
        f"the {settings.crest_binary!r} binary is not installed on this server, so conformer, "
        "tautomer, protomer and non-covalent-complex sampling are unavailable. It is absent from "
        "Chemclaw3's own environment too, so nothing that previously worked has stopped working; "
        "adding it to this server's image is what turns these on"
    )


def search_ensemble(spec: EnsembleSpec | ComplexSpec, structure: Structure) -> list[EnsembleMember]:
    """Run one CREST search on `structure` and return its members, lowest energy first.

    The whole primitive: no populations, no entropy, no interaction energy. Those are arithmetic
    over what this returns, and they belong with the orchestration that asked for them.

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

    Each monomer is centred and then offset along x by the sum of their radii plus a gap, so the two
    start apart regardless of their shapes. This is only a starting point: the wall potential holds
    the pair together and the search finds the binding modes, so the arrangement here decides
    nothing except that the pair does not begin overlapping.

    Exposed as its own primitive rather than folded into the search, because it is the step that
    produces the *subject* a complex search is keyed on. A caller that cannot build the combined
    structure cannot derive the key, and would be back to guessing it.

    **Not symmetric in its arguments** — it holds the first monomer at the origin and offsets the
    second — so a caller wanting A-with-B and B-with-A to be one calculation orders the pair first
    with `ordered_pair`.
    """
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
        # Two closed shells make a closed shell; an open-shell monomer is rejected by `Structure`
        # itself rather than silently mis-assigned here.
        multiplicity=first.multiplicity + second.multiplicity - 1,
        smiles=f"{first.smiles}.{second.smiles}",
    )


def ordered_pair(smiles_a: str, smiles_b: str) -> tuple[str, str]:
    """The pair in a canonical order, so A-with-B and B-with-A are one calculation.

    The interaction of two molecules is one physical quantity, but `combine_structures` is not
    symmetric in its arguments: swapping them negates the intermolecular vector while leaving each
    monomer's own orientation alone. That is a *different* starting arrangement, and it would key to
    a different entry — paying twice, at minutes per search, for the same answer.
    """
    first, second = require_canonical_smiles(smiles_a), require_canonical_smiles(smiles_b)
    return (first, second) if first <= second else (second, first)
