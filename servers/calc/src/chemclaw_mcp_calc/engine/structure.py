"""A concrete 3D structure — the value every xTB task consumes, and the `input_hash` of its key.

`structure_id` is a stable hash of the chemical content, so equal geometries share one identity
however produced (`origin` records lineage). The key therefore names the geometry, not the recipe,
and `multiplicity` is a declared, validated electron count.

`structure_id` is a wire contract with Chemclaw3's cache and must not change: round positions to
`settings.xtb_geometry_decimals`, then `stable_hash` over `{elements, positions, charge,
multiplicity}`, excluding `smiles` and `origin`. Changing the rounding, field set or key order
re-addresses every structure.

Coordinates are in **Angstrom**; `xtb_engine` is the single boundary that converts to atomic units.
"""

from __future__ import annotations

import numpy as np
from mcp_server_kit.limits import echo
from pydantic import BaseModel, Field, computed_field, model_validator
from rdkit import Chem

from chemclaw_mcp_calc.engine.chem import require_canonical_smiles
from chemclaw_mcp_calc.engine.config import settings
from chemclaw_mcp_calc.engine.ids import stable_hash
from chemclaw_mcp_calc.engine.xtb_engine import atom_ceiling_error, geometry, parse_molecule

__all__ = [
    "Structure",
    "atom_ceiling_error",
    "radical_multiplicity",
    "structure_from_mol",
    "structure_from_smiles",
]


class Structure(BaseModel):
    """One 3D molecular structure, addressed by the hash of its chemical content.

    `elements` and `positions` are parallel: atom `i` has atomic number `elements[i]` at
    `positions[i]` (Angstrom). Positions are normalized on construction (rounded to
    `settings.xtb_geometry_decimals`) so that float noise from a re-run cannot fork the identity
    while the stored coordinates still *are* the ones that were hashed.
    """

    elements: list[int] = Field(min_length=1)
    positions: list[list[float]] = Field(min_length=1)
    charge: int = 0
    # Spin multiplicity 2S+1: 1 = closed-shell singlet, 2 = doublet, 3 = triplet.
    multiplicity: int = Field(default=1, ge=1)
    # The canonical SMILES this structure represents, when it came from (or maps to) one. Carried
    # for reporting and for the atom-index mapping in `symbols`.
    smiles: str | None = None
    # `CalculationKey.as_str()` of the calculation that produced this geometry, for structures that
    # are a calculation's *output* rather than an embedding.
    origin: str | None = None

    @model_validator(mode="after")
    def _normalize_and_validate(self) -> Structure:
        """Round coordinates, then reject a structure that is not physically consistent.

        Catches mismatched array lengths, non-3D rows, and an electron count that cannot produce the
        declared multiplicity, rather than letting tblite converge something meaningless.

        Also enforces the atom ceiling here, once for every tool: a structure under the request body
        cap can carry tens of thousands of atoms, enough for the optimizer to exhaust the process.
        One ceiling (`xtb_max_atoms`) is derived from the most expensive path (geomeTRIC's
        coordinate build), which is conservative for the others; `xtb_hessian_max_atoms` is tighter.
        The refusal names both numbers.
        """
        if len(self.positions) != len(self.elements):
            raise ValueError(f"{len(self.positions)} positions for {len(self.elements)} elements")
        if any(len(row) != 3 for row in self.positions):
            raise ValueError("every position must have exactly three coordinates")
        if reason := atom_ceiling_error(len(self.elements), subject="a structure"):
            raise ValueError(reason)
        decimals = settings.xtb_geometry_decimals
        # `+ 0.0` normalizes the negative zero that rounding can produce, so two geometrically
        # identical structures cannot differ in their hash by a sign bit.
        self.positions = [[round(value, decimals) + 0.0 for value in row] for row in self.positions]
        unpaired = self.multiplicity - 1
        electrons = sum(self.elements) - self.charge
        if electrons < unpaired or (electrons - unpaired) % 2:
            # The accidental case (a radical SMILES or wrong charge) gets the specific message:
            # declare the multiplicity.
            if self.multiplicity == 1:
                raise ValueError(
                    f"open-shell species ({electrons} electrons at charge {self.charge}) "
                    "cannot be a closed-shell singlet: declare its multiplicity explicitly"
                )
            raise ValueError(
                f"{electrons} electrons at charge {self.charge} cannot form multiplicity "
                f"{self.multiplicity} ({unpaired} unpaired)"
            )
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def structure_id(self) -> str:
        """Content address: `st_` + a stable hash of the chemistry, not the provenance.

        A `computed_field` so the id is serialized and a caller never re-derives it. Output-only:
        the id is always recomputed from the coordinates that arrived, so an edited payload keys as
        what it is. Excludes `smiles` and `origin`. The payload dict is a wire contract with
        Chemclaw3 — every `xtb.*` `input_hash` derives from it — and `tests/test_key_contract.py`
        pins the result.
        """
        payload = {
            "elements": self.elements,
            "positions": self.positions,
            "charge": self.charge,
            "multiplicity": self.multiplicity,
        }
        return f"st_{stable_hash(payload)}"

    @property
    def uhf(self) -> int:
        """Number of unpaired electrons, the form tblite wants."""
        return self.multiplicity - 1

    @property
    def symbols(self) -> list[str]:
        """Element symbols, one per atom, for human-readable per-atom results."""
        table = Chem.GetPeriodicTable()
        return [table.GetElementSymbol(number) for number in self.elements]

    def arrays(self) -> tuple[np.ndarray, np.ndarray]:
        """Return (atomic numbers, positions in Angstrom) for the engine."""
        return np.array(self.elements), np.array(self.positions)


def structure_from_mol(
    mol: Chem.Mol,
    *,
    charge: int,
    multiplicity: int = 1,
    smiles: str | None = None,
    optimize: bool = False,
) -> Structure:
    """Embed a deterministic geometry for `mol` and wrap it as a `Structure`.

    Expects explicit hydrogens so the electron count is complete; the seed comes from config so the
    structure id is reproducible.
    """
    numbers, positions = geometry(mol, settings.xtb_embed_seed, optimize=optimize)
    return Structure(
        elements=[int(number) for number in numbers],
        positions=[[float(value) for value in row] for row in positions],
        charge=charge,
        multiplicity=multiplicity,
        smiles=smiles,
    )


def radical_multiplicity(mol: Chem.Mol) -> int:
    """The spin multiplicity a SMILES' explicit radical electrons imply (2S+1, all unpaired).

    A closed-shell formula whose ground state is a triplet still needs `multiplicity` stated.
    """
    return 1 + sum(int(atom.GetNumRadicalElectrons()) for atom in mol.GetAtoms())


def structure_from_smiles(
    smiles: str,
    *,
    charge: int | None = None,
    multiplicity: int | None = 1,
    optimize: bool = False,
) -> Structure:
    """Build a `Structure` from a SMILES, canonicalizing first.

    Atom order steers the seeded embedding, so canonicalizing first gives two spellings one geometry
    and one key.

    Args:
        smiles: The molecule as a SMILES string.
        charge: Net charge. `None` takes the SMILES' formal charge; a contradicting value is
        rejected. multiplicity: Spin multiplicity 2S+1, validated against the electron count. `None`
        derives it from explicit radical electrons; the default 1 means closed-shell-or-error.
        optimize: Pre-optimize with MMFF where the force field has parameters.

    Returns:
        The embedded structure, carrying the canonical SMILES.
    """
    canonical = require_canonical_smiles(smiles)
    mol = parse_molecule(canonical)
    # Refused before embedding: `Structure`'s own ceiling runs only after ETKDG and MMFF have done
    # super-linear work, and these paths hold no admission slot. Counts the `AddHs` molecule, the
    # same number the validator counts.
    if reason := atom_ceiling_error(mol.GetNumAtoms(), subject=f"the molecule {echo(smiles)!r}"):
        raise ValueError(reason)
    formal_charge = Chem.GetFormalCharge(mol)
    if charge is None:
        charge = formal_charge
    elif charge != formal_charge:
        raise ValueError(
            f"declared charge {charge} does not match the formal charge "
            f"{formal_charge} of {echo(smiles)!r}"
        )
    return structure_from_mol(
        mol,
        charge=charge,
        multiplicity=radical_multiplicity(mol) if multiplicity is None else multiplicity,
        smiles=canonical,
        optimize=optimize,
    )
