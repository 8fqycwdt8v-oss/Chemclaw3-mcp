"""Reading a species string, whole or not at all.

RDKit stops at whitespace and skips non-ASCII bytes at a string's edges, so `"CCO (2 vol)"` parses
as ethanol and `"°C"` as methane. This server reads ELN and patent free text, so it must refuse such
strings. The rules mirror `chem`'s `require_molecule`, transcribed because servers do not import
each other.

Unlike `chem`, a bad string returns `None` rather than raising, so one OCR artefact does not lose a
whole reaction. Every caller must keep "could not be read" distinct from "read, and carries
nothing", or an unreadable species is counted as a negative.
"""

from __future__ import annotations

from mcp_server_kit.limits import atom_count_error, smiles_length_error
from rdkit import Chem

__all__ = ["read_molecule"]


def read_molecule(smiles: str) -> Chem.Mol | None:
    """The parsed molecule, or `None` unless RDKit reads `smiles` **whole**.

    Surrounding whitespace is stripped; internal whitespace and any non-ASCII character are refused
    (checked on the string, since the parsed molecule cannot show what RDKit skipped).
    """
    stripped = smiles.strip()
    if not stripped or any(character.isspace() for character in stripped):
        return None
    if not stripped.isascii():
        return None
    # The size bound must precede canonicalisation: `MolToSmiles` on a huge linear molecule
    # overflows the C stack (SIGSEGV) and would take the server down. See `mcp_server_kit.limits`.
    if smiles_length_error(stripped) is not None:
        return None
    mol = Chem.MolFromSmiles(stripped)
    if mol is None or mol.GetNumAtoms() == 0:
        return None
    if atom_count_error(mol.GetNumAtoms()) is not None:
        return None
    return mol
