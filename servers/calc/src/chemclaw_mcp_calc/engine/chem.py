"""Canonical SMILES and the strict parse: "is this the same structure?", for this server's keys.

**Chemclaw3's `chemclaw/core/chem.py` is the authority for this definition, not this file.** This
is a copy — the *third* in this repository, after `servers/chem/` and `servers/safety/` — and it
exists for the rule this repository is arranged around: a server is a dependency closure, one
server never imports another, and none may import Chemclaw3.

**The bound the other two copies carry does not apply here, and that is the difference worth
naming.** Both of them say "nothing here derives a cache key". This server does: `structure.py`
canonicalizes before embedding, and the resulting geometry's `structure_id` is the `input_hash` of
an `xtb.*` key; `pka`, `solubility` and `descriptors` hash the canonical SMILES directly. So on
this server, a divergence in this function would not merely make two systems echo two spellings —
it would produce a `CalculationKey` addressing a row that does not exist, and Chemclaw3 would
record a prediction the ledger never reconciles.

`tests/test_canonicalization_contract.py` makes that detectable: the same table
`servers/chem/` and `servers/safety/` carry, as literal strings derived by running Chemclaw3's own
function, so whichever copy moves first turns a test red instead of quietly answering differently.

**What was deliberately left behind**, so nobody restores it believing it was an oversight:
Chemclaw3's module also carries the `standardize` pipeline that answers the *other* question, "is
this the same compound?" (salts stripped, charges neutralized, one tautomer per set) plus
`compound_id` built on it. Nothing here asks that question — and it must not: an anion is a
different calculation from its conjugate acid, and `Structure` validates a declared charge against
its SMILES, so standardizing a submitted acetate into acetic acid would compute a different
molecule under the caller's key.
"""

from __future__ import annotations

from collections.abc import Sequence

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

    A `ValueError` on purpose, and load-bearing rather than stylistic:
    `mcp_server_kit.connector_app` lets a `ValueError` reach the model verbatim and replaces every
    other exception with a generic notice. These messages quote the string that was rejected, and a
    refusal has to be actionable — a chemist told "internal error" would re-ask the same malformed
    question.
    """


def require_molecule(smiles: str) -> Chem.Mol:
    """The parsed molecule, raising `InvalidSmilesError` unless RDKit reads `smiles` **whole**.

    The one definition of "RDKit accepts this string, all of it". Three inputs RDKit accepts and
    this rejects, each measured against a real build in Chemclaw3 before being written down:

    - **A string with embedded whitespace.** The parser treats any whitespace as the end of the
      structure and ignores the rest, so `"CCO junk"` is ethanol. That is the silent-truncation
      class, and on a calculator it is worse than a parse error: the energy returned is a real,
      converged energy — of a *different, smaller molecule* than the caller submitted, stored under
      a key naming the string they typed.
    - **The empty string**, which parses to a molecule with no atoms — a structure for nothing.
    - **A string carrying a non-ASCII character at either end.** SMILES is printable ASCII, and
      RDKit skips a run of non-ASCII bytes at the *edges* while failing on one between two atoms:
      `"°C"` is methane, `"CC°"` is ethane, `"C°C"` is a parse error. Prose is what produces this —
      a note reading `` `80 °C` `` offers `°C` as a candidate structure, and a bare parse calls it
      methane. Tested on the string rather than on the parsed molecule, because once RDKit has
      skipped the character nothing about the molecule says it was ever there.

    Surrounding whitespace is stripped rather than refused: a leading newline is a copy-paste
    artifact, not a second molecule. The message quotes the caller's own string, not the stripped
    one, so what is echoed back is what was typed.

    Raises:
        InvalidSmilesError: `smiles` is empty, holds whitespace or non-ASCII, or does not parse.
    """
    stripped = smiles.strip()
    if not stripped or any(ch.isspace() for ch in stripped):
        raise InvalidSmilesError(f"invalid SMILES (empty or contains whitespace): {smiles!r}")
    if not stripped.isascii():
        raise InvalidSmilesError(f"invalid SMILES (non-ASCII characters): {smiles!r}")
    mol = Chem.MolFromSmiles(stripped)
    if mol is None or mol.GetNumAtoms() == 0:
        raise InvalidSmilesError(f"invalid SMILES: {smiles!r}")
    return mol


def require_canonical_smiles(smiles: str) -> str:
    """RDKit canonical SMILES, raising `InvalidSmilesError` if `smiles` does not parse.

    Spelling only: `"CCO"` and `"OCC"` collapse to one string, while an anion and its conjugate acid
    stay two. That distinction is the reason this and not `standardize` keys a calculation —
    computing the conjugate acid of a submitted anion would answer a question nobody asked.

    Canonicalizing *before* embedding is what makes two spellings of one molecule produce the same
    3D geometry, and therefore the same `structure_id` and the same key.
    """
    return str(Chem.MolToSmiles(require_molecule(smiles)))


def atomic_numbers(symbols: Sequence[str]) -> list[int]:
    """Atomic numbers for element symbols, rejecting one the periodic table does not know.

    The inverse of `Structure.symbols`, and it exists because a CREST ensemble file is the one
    input to this server whose *element list* is not already known: a protonation search adds or
    removes an atom and presorts the rest, so the elements have to be read from the file rather
    than inherited from the structure that was sent in.

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

    A CREST protonation, deprotonation or tautomer search returns structures whose constitution is
    not the input's — that is the whole point of running it — so the SMILES the caller sent in is
    the wrong label for every member. Without perception the ensemble is a list of anonymous
    geometries, and the question a chemist actually asked ("which site comes off first?") is
    unanswerable from the result. Measured on phenol's deprotomer ensemble: `[O-]c1ccccc1`, in 4 ms.

    **Best-effort by construction, and never a guess.** Bond orders are inferred from interatomic
    distances plus the *known* charge, and that inference fails on exactly the structures where it
    would be most misleading — a transition-metal complex, a fragment mid-dissociation, a geometry
    whose bonding is genuinely ambiguous. On any failure this answers `None` and the member travels
    without a label, because a wrong constitution reported confidently is worse than no label: it
    would name the wrong protonation site in a pKa.

    The atom-count ceiling is a real bound rather than caution: bond-order assignment is
    combinatorial over the conjugated system, and an unbounded call inside an ensemble loop is a
    hang rather than a slow answer.

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
