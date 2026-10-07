"""Canonical SMILES and the strict parse: "is this the same structure?", for this server's input.

A copy of Chemclaw3's `chemclaw/core/chem.py`, which is the authority (as is `servers/chem`'s copy):
servers never import each other or Chemclaw3. The copy derives no cache key — it only governs what
this server accepts and echoes — and `tests/test_canonicalization_contract.py` pins literal results
from Chemclaw3's function so a divergence turns a test red. Chemclaw3's `standardize`/`compound_id`
("same compound?") are deliberately absent; no tool here asks that question.
"""

from __future__ import annotations

from mcp_server_kit.limits import atom_count_error, echo, smiles_length_error
from rdkit import Chem

__all__ = ["InvalidSmilesError", "require_canonical_smiles", "require_molecule"]


class InvalidSmilesError(ValueError):
    """A SMILES string RDKit cannot parse, or will only parse by silently truncating it.

    A `ValueError` so `connector_app` passes the message, which quotes the rejected string, to the
    model verbatim.
    """


def require_molecule(smiles: str) -> Chem.Mol:
    """The parsed molecule, raising `InvalidSmilesError` unless RDKit reads `smiles` **whole**.

    Rejects three inputs RDKit accepts:

    - **Embedded whitespace**: RDKit ignores everything after it, so `"CCO CN=[N+]=[N-]"` would
      screen as ethanol with the azide unseen.
    - **The empty string**, which parses to a molecule with no atoms.
    - **Non-ASCII characters**: RDKit skips them at the edges (`"°C"` parses as methane); checked on
      the string, since the parsed molecule cannot show it.

    Surrounding whitespace is stripped; the message quotes the caller's original string.

    Raises:
        InvalidSmilesError: `smiles` is empty, holds whitespace or non-ASCII, or does not parse.
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

    Spelling only: `"CCO"` and `"OCC"` collapse, an anion and its conjugate acid do not. The ICH
    lookup relies on the strictness to tell a structure from an unknown name.
    """
    return str(Chem.MolToSmiles(require_molecule(smiles)))
