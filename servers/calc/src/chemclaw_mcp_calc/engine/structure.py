"""A concrete 3D structure — the value every xTB task consumes, and the `input_hash` of its key.

`Structure` makes the geometry an explicit, **content-addressed** value: `structure_id` is a stable
hash of the chemical content, so equal geometries collapse to one identity no matter how they were
produced, and `origin` records which calculation produced one (lineage).

Two consequences:

- the calculation key names the *geometry*, not the recipe that made it, so the embedding seed no
  longer has to appear in the key — its effect is already inside the coordinates;
- `multiplicity` generalizes a hard closed-shell rejection into a declared-and-validated electron
  count, which is what makes the Fukui ions (`xtb_props`) a legitimate open-shell calculation rather
  than a silent one.

**`structure_id` is ported unchanged and must stay that way.** It is the `input_hash` of every
`xtb.*` `CalculationKey` this server emits, and those keys address rows in Chemclaw3's cache. The
derivation is: round the positions to `settings.xtb_geometry_decimals` first, then `stable_hash`
over `{elements, positions, charge, multiplicity}` — deliberately *excluding* `smiles` and `origin`,
because two identical geometries are the same structure whether one was embedded from a SMILES and
the other optimized. Changing the rounding, the field set, or the key order re-addresses every
structure.

Coordinates are in **Angstrom** — the interchange unit of RDKit, XYZ files, and this whole layer;
`xtb_engine` is the single boundary that converts to atomic units.
"""

from __future__ import annotations

import numpy as np
from mcp_server_kit.limits import atom_count_error
from pydantic import BaseModel, Field, computed_field, model_validator
from rdkit import Chem

from chemclaw_mcp_calc.engine.chem import require_canonical_smiles
from chemclaw_mcp_calc.engine.config import settings
from chemclaw_mcp_calc.engine.ids import stable_hash
from chemclaw_mcp_calc.engine.xtb_engine import geometry, parse_molecule

__all__ = [
    "Structure",
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

        Three ways a structure can be wrong are caught here rather than by tblite converging
        something meaningless: mismatched array lengths, a coordinate row that is not 3D, and an
        electron count that cannot produce the declared multiplicity.

        **And one way it can be right and still unaffordable.** The atom ceiling lives here rather
        than on each tool for the same reason the electron-count check does: four primitives take a
        structure and the next one will, so a per-tool check is one somebody forgets — and only
        `compute_hessian` had one. A structure under the 1 MB body cap can carry ~42,000 atoms, at
        which the optimizer's dense model Hessian asks for 127 GB and takes the whole process with
        it, every other connected turn included. The refusal names both numbers because the caller's
        only options are a smaller system or a deployment configured for a larger one.
        """
        if len(self.positions) != len(self.elements):
            raise ValueError(f"{len(self.positions)} positions for {len(self.elements)} elements")
        if any(len(row) != 3 for row in self.positions):
            raise ValueError("every position must have exactly three coordinates")
        if len(self.elements) > settings.xtb_max_atoms:
            raise ValueError(
                f"a structure of {len(self.elements)} atoms exceeds this server's limit of "
                f"{settings.xtb_max_atoms}: every calculation here is at least one SCF over the "
                "whole system and runs inside a conversation turn, so a system this size is "
                "refused rather than started and abandoned"
            )
        decimals = settings.xtb_geometry_decimals
        # `+ 0.0` normalizes the negative zero that rounding can produce, so two geometrically
        # identical structures cannot differ in their hash by a sign bit.
        self.positions = [[round(value, decimals) + 0.0 for value in row] for row in self.positions]
        unpaired = self.multiplicity - 1
        electrons = sum(self.elements) - self.charge
        if electrons < unpaired or (electrons - unpaired) % 2:
            # The default (closed-shell) case gets the specific message, because it is the one a
            # caller hits by accident — from a radical SMILES or a wrong charge — and the fix is to
            # declare the multiplicity, not to fix the atoms.
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

        **A `computed_field` rather than a plain property, and that is load-bearing rather than
        stylistic.** A plain property is not serialized, so a `Structure` crossing the wire would
        arrive without its content address — and a caller would then have to re-derive it, which is
        the one thing this whole seam exists to prevent: the derivation depends on the installed
        RDKit's embedding and on `xtb_geometry_decimals`, so a client-side rebuild is the silent
        divergence again. `tests/test_server.py` caught exactly this by asserting it on the payload
        rather than on the object.

        Output-only, so a caller may send the field back unchanged and it is ignored on the way in:
        the id is always recomputed from the coordinates that actually arrived, and a payload edited
        in transit therefore keys as what it *is* rather than as what it claims.

        Deliberately excludes `smiles` and `origin`: two identical geometries are the same structure
        whether one was embedded from a SMILES and the other optimized, and that is exactly the
        identity that lets a downstream task address the same entry regardless of which route
        produced its input.

        **The payload dict below is a wire contract with Chemclaw3**, not an implementation detail:
        it is what `stable_hash` sees, and therefore what every `xtb.*` `input_hash` is derived
        from. `tests/test_key_contract.py` pins the result.
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

    Expects explicit hydrogens (`xtb_engine.parse_molecule` output) so the electron count validated
    by `Structure` is complete. The embedding seed comes from config, so the geometry — and
    therefore the structure id — is reproducible.
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
    """The spin multiplicity a SMILES' explicit radical electrons imply.

    A SMILES *can* state its open shell: `[CH3]` carries one radical electron, `[O][O]` two. Where
    it does, the ground-state multiplicity follows (2S+1 with every radical electron unpaired), and
    there is nothing to guess. Silent on the cases a SMILES genuinely does not encode: a
    closed-shell formula whose ground state is a triplet still needs `multiplicity` stated
    explicitly.
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

    Atom order steers the seeded embedding, so canonicalizing *before* embedding is what makes two
    spellings of one molecule produce the same geometry — and thus the same structure id and the
    same key.

    Args:
        smiles: The molecule as a SMILES string.
        charge: Net charge. `None` takes the SMILES' own formal charge; an explicit value that
            contradicts it is rejected rather than computed at the wrong electron count.
        multiplicity: Spin multiplicity 2S+1; validated against the electron count. `None` derives
            it from the SMILES' explicit radical electrons; the default of 1 keeps every caller
            closed-shell-or-error.
        optimize: Pre-optimize with MMFF where the force field has parameters.

    Returns:
        The embedded structure, carrying the canonical SMILES.
    """
    canonical = require_canonical_smiles(smiles)
    mol = parse_molecule(canonical)
    # **Refused here, before the embedding, because `Structure`'s own ceiling is checked after it.**
    # `_normalize_and_validate` rejects a molecule over `xtb_max_atoms`, and it runs on the finished
    # `Structure` — i.e. after ETKDG and MMFF have already done the work. The kit's SMILES guard
    # bounds the *crash* at 2,000 atoms and not the cost, so the interval between the two ceilings
    # was paid in full and then thrown away. Measured, one process each:
    #
    #     "C"*120 -> 362 atoms,  13.49 s, accepted
    #     "C"*200 -> 602 atoms,  66.68 s, refused after the work
    #     "C"*300 -> 902 atoms, 206.23 s, refused after the work
    #
    # That is ~n^2.8, so a 2 kB SMILES — inside the body cap and inside the kit's guard — is hours
    # of one core. `embed_structure` and `calculation_key` are `read_only`, so they carry no
    # `engine.admission` slot and four such calls own a four-core pod; `MODULES.md` calls them
    # "helpers that compute nothing" and `tools.py` calls them "cheap — RDKit only, no SCF", which
    # is true about the SCF and not about the cost. Asking the same question of the parsed molecule
    # first makes the refusal 4 ms.
    oversized = atom_count_error(
        mol.GetNumAtoms(), subject="this molecule", max_atoms=settings.xtb_max_atoms
    )
    if oversized:
        raise ValueError(oversized)
    formal_charge = Chem.GetFormalCharge(mol)
    if charge is None:
        charge = formal_charge
    elif charge != formal_charge:
        raise ValueError(
            f"declared charge {charge} does not match the formal charge "
            f"{formal_charge} of {smiles!r}"
        )
    return structure_from_mol(
        mol,
        charge=charge,
        multiplicity=radical_multiplicity(mol) if multiplicity is None else multiplicity,
        smiles=canonical,
        optimize=optimize,
    )
