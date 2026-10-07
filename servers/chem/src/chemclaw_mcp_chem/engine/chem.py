"""Canonical SMILES: "is this the same structure?", for this server's own input validation.

A copy of Chemclaw3's `chemclaw/core/chem.py`, which is the authority; a server here cannot
import Chemclaw3. This copy governs only what the server accepts and echoes — every cache key is
derived on the Chemclaw3 side. `tests/test_canonicalization_contract.py` holds a literal table of
expected canonical outputs that runs in both repositories, so divergence turns a test red.
`require_canonical_smiles` stays a byte-for-byte copy.

`require_dative_free_smiles` is added on top: it spells each dative bond as a charge-separated
single bond (`[Pd-]...[NH2+]`), the same structure in a form every SMILES reader accepts.
"""

from __future__ import annotations

from mcp_server_kit.limits import atom_count_error, echo, smiles_length_error
from rdkit import Chem
from rdkit.Chem import Descriptors

__all__ = [
    "InvalidSmilesError",
    "molecular_weight",
    "require_canonical_smiles",
    "require_dative_free_smiles",
    "require_molecule",
    "require_whole_string",
]


class InvalidSmilesError(ValueError):
    """A SMILES string RDKit cannot parse, or will only parse by silently truncating it.

    A `ValueError` so `connector_app` passes the message, which quotes the rejected string, to the
    model verbatim.
    """


def require_molecule(smiles: str) -> Chem.Mol:
    """The parsed molecule, raising `InvalidSmilesError` unless RDKit reads `smiles` **whole**.

    Rejects three inputs RDKit accepts: embedded whitespace (RDKit stops at it, so `"CCO junk"` is
    ethanol), the empty string (a molecule with no atoms), and non-ASCII at either end (RDKit skips
    it, so `"°C"` is methane). Checked on the string, since the molecule carries no trace of what
    was skipped. Surrounding whitespace is stripped; the message quotes the caller's original
    string.

    Raises:
        InvalidSmilesError: `smiles` is empty, holds whitespace or non-ASCII, or does not parse.
    """
    if reason := smiles_length_error(smiles, subject="this SMILES"):
        raise InvalidSmilesError(reason)
    stripped = require_whole_string(smiles)
    mol = Chem.MolFromSmiles(stripped)
    if mol is None or mol.GetNumAtoms() == 0:
        raise InvalidSmilesError(f"invalid SMILES: {echo(smiles)!r}")
    if reason := atom_count_error(mol.GetNumAtoms(), subject="this SMILES"):
        raise InvalidSmilesError(reason)
    return mol


def require_whole_string(smiles: str, what: str = "SMILES") -> str:
    """The stripped string, raising unless every character of it is part of one structure.

    `require_molecule`'s pre-parse checks, separate so a reaction (split on `">>"` before any parse)
    gets them too and a trailing component cannot vanish from a drawing.

    Args:
        smiles: The string as the caller typed it.
        what: What the string was meant to be, for the message.

    Raises:
        InvalidSmilesError: `smiles` is empty, holds whitespace, or is not printable ASCII.
    """
    stripped = smiles.strip()
    echoed = echo(smiles)
    if not stripped or any(ch.isspace() for ch in stripped):
        raise InvalidSmilesError(f"invalid {what} (empty or contains whitespace): {echoed!r}")
    if not stripped.isascii():
        raise InvalidSmilesError(f"invalid {what} (non-ASCII characters): {echoed!r}")
    return stripped


def require_canonical_smiles(smiles: str) -> str:
    """RDKit canonical SMILES, raising `InvalidSmilesError` if `smiles` does not parse.

    Spelling only: `"CCO"` and `"OCC"` collapse, an anion and its conjugate acid stay two. The
    strictness is what lets `resolve_compound` tell a structure from an unknown name.
    """
    return str(Chem.MolToSmiles(require_molecule(smiles)))


def require_dative_free_smiles(smiles: str) -> str:
    """Canonical SMILES with each dative bond written as a charge-separated single bond.

    Each `D->A` becomes `[D+]-[A-]` with both ends' hydrogen counts held; any other input gets
    exactly `require_canonical_smiles`'s string. The output is a fixed point. Charge separation
    rather than a plain single bond because a four-valent aromatic `n` will not parse. A donor that
    cannot carry the charge (an alkene or arene π-donor) keeps its arrow; each bond is rewritten
    independently.

    Raises:
        InvalidSmilesError: `smiles` does not parse (see `require_molecule`).
    """
    mol = require_molecule(smiles)
    for bond in list(mol.GetBonds()):
        if bond.GetBondType() != Chem.BondType.DATIVE:
            continue
        candidate = Chem.RWMol(mol)
        edited = candidate.GetBondWithIdx(bond.GetIdx())
        for atom, shift in ((edited.GetBeginAtom(), 1), (edited.GetEndAtom(), -1)):
            atom.SetNumExplicitHs(atom.GetTotalNumHs())
            atom.SetNoImplicit(True)
            atom.SetFormalCharge(atom.GetFormalCharge() + shift)
        edited.SetBondType(Chem.BondType.SINGLE)
        try:
            Chem.SanitizeMol(candidate)
        except Chem.MolSanitizeException:
            continue  # the donor cannot hold the charge; this bond keeps its arrow (see above)
        mol = candidate.GetMol()
    return str(Chem.MolToSmiles(mol))


def molecular_weight(smiles: str) -> float:
    """Average molecular weight in g/mol, for the charge-table arithmetic.

    Average rather than monoisotopic: a charge table is what somebody weighs. The `type: ignore` is
    because `rdkit-stubs` omits descriptors defined as lambdas.

    Raises:
        InvalidSmilesError: `smiles` does not parse.
    """
    return float(Descriptors.MolWt(require_molecule(smiles)))  # type: ignore[attr-defined]
