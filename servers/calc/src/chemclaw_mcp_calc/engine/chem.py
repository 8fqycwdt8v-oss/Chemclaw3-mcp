"""Canonical SMILES and the strict parse: "is this the same structure?", for this server's keys.

A copy of Chemclaw3's `chemclaw/core/chem.py` (the authority), because servers never import each
other or Chemclaw3. Here it feeds cache keys — the canonical SMILES becomes a structure's
`input_hash` — so a divergence would address rows that do not exist;
`tests/test_canonicalization_contract.py` pins it against Chemclaw3's own outputs.

`standardize` ("is this the same compound?") is deliberately absent: an anion and its conjugate
acid are different calculations.
"""

from __future__ import annotations

from collections.abc import Sequence

from mcp_server_kit.limits import atom_count_error, echo, smiles_length_error
from rdkit import Chem
from rdkit.Chem import rdDetermineBonds

from chemclaw_mcp_calc.engine.config import settings

__all__ = [
    "InvalidSmilesError",
    "atomic_numbers",
    "perceive_smiles",
    "require_canonical_smiles",
    "require_molecule",
]


class InvalidSmilesError(ValueError):
    """A SMILES string RDKit cannot parse, or will only parse by silently truncating it.

    A `ValueError` so `connector_app` passes the actionable message to the model verbatim.
    """


def require_molecule(smiles: str) -> Chem.Mol:
    """The parsed molecule, raising `InvalidSmilesError` unless RDKit reads `smiles` **whole**.

    The size bounds from `mcp_server_kit.limits` come first: canonicalising a long linear molecule
    overflows the C stack and kills the pod. Then refused, though RDKit would accept them:

    - embedded whitespace (RDKit stops at it, so `"CCO junk"` would compute ethanol under the
      caller's key);
    - the empty string (a molecule with no atoms);
    - non-ASCII at either end (RDKit skips it, so `"°C"` would be methane).

    Surrounding whitespace is stripped. Messages quote the caller's string through `limits.echo`.

    Raises:
        InvalidSmilesError: `smiles` is too long or too large, empty, holds whitespace or
            non-ASCII, or does not parse.
    """
    if reason := smiles_length_error(smiles, subject="this SMILES"):
        raise InvalidSmilesError(reason)
    stripped = smiles.strip()
    echoed = echo(smiles)
    if not stripped or any(ch.isspace() for ch in stripped):
        raise InvalidSmilesError(f"invalid SMILES (empty or contains whitespace): {echoed!r}")
    if not stripped.isascii():
        raise InvalidSmilesError(f"invalid SMILES (non-ASCII characters): {echoed!r}")
    mol = Chem.MolFromSmiles(stripped)
    if mol is None or mol.GetNumAtoms() == 0:
        raise InvalidSmilesError(f"invalid SMILES: {echoed!r}")
    if reason := atom_count_error(mol.GetNumAtoms(), subject="this SMILES"):
        raise InvalidSmilesError(reason)
    return mol


def require_canonical_smiles(smiles: str) -> str:
    """RDKit canonical SMILES, raising `InvalidSmilesError` if `smiles` does not parse.

    Spelling only: `"CCO"` and `"OCC"` collapse, an anion and its conjugate acid stay two.
    Canonicalising before embedding gives two spellings the same geometry and the same key.
    """
    return str(Chem.MolToSmiles(require_molecule(smiles)))


def atomic_numbers(symbols: Sequence[str]) -> list[int]:
    """Atomic numbers for element symbols, rejecting one the periodic table does not know.

    Needed for CREST ensemble files, whose element list may differ from the input (a protonation
    search adds or removes an atom).

    Raises:
        ValueError: naming the symbol RDKit's periodic table refuses.
    """
    table = Chem.GetPeriodicTable()
    numbers: list[int] = []
    for symbol in symbols:
        try:
            numbers.append(int(table.GetAtomicNumber(symbol)))
        except RuntimeError as error:  # RDKit raises this for an unknown symbol
            raise ValueError(f"{symbol!r} is not an element symbol") from error
    return numbers


def perceive_smiles(
    elements: Sequence[int], positions: Sequence[Sequence[float]], charge: int
) -> str | None:
    """Best-effort SMILES for a bare geometry: *which* molecule is this one?

    A protonation, deprotonation or tautomer search changes constitution, so ensemble members need
    their own label. Bond orders are inferred from distances and the *known* charge; on any failure
    (or above the atom ceiling, since assignment is combinatorial) this returns `None` rather than a
    possibly wrong constitution.

    Args:
        elements: Atomic numbers, parallel to `positions`.
        positions: Cartesian coordinates in Angstrom.
        charge: The species' net charge — an input, not something perception may decide.

    Returns:
        The canonical SMILES, or `None` when the geometry cannot be read as one molecule.
    """
    if len(elements) > settings.crest_perceive_max_atoms:
        return None
    table = Chem.GetPeriodicTable()
    lines = [str(len(elements)), ""]
    lines += [
        f"{table.GetElementSymbol(number)} {x:.10f} {y:.10f} {z:.10f}"
        for number, (x, y, z) in zip(elements, positions, strict=True)
    ]
    try:
        mol = Chem.MolFromXYZBlock("\n".join(lines) + "\n")
        if mol is None:
            return None
        rdDetermineBonds.DetermineBonds(mol, charge=charge)
        Chem.SanitizeMol(mol)
        return str(Chem.MolToSmiles(Chem.RemoveHs(mol)))
    except (ValueError, RuntimeError, Chem.AtomValenceException, Chem.KekulizeException):
        return None
