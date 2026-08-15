"""Canonical SMILES and the strict parse: "is this the same structure?", for this server's input.

**Chemclaw3's `chemclaw/core/chem.py` is the authority for this definition, not this file.** This is
a copy — and it is the *second* copy in this repository,
`servers/chem/src/chemclaw_mcp_chem/engine/chem.py` being the first. Both exist for the same reason
and it is the rule this repository is arranged around: a server is a dependency closure, one server
never imports another, and neither may import Chemclaw3. Three screens here refuse a SMILES RDKit
would silently truncate, and the ICH lookup resolves a structure to a name, so all three need this
definition inside their own process.

The duplication is bounded the same way `chem`'s is, and the bound is what makes it defensible:

- **Nothing here derives a cache key.** D-011 ("compute once, never twice") keys the calculation
  cache, the QM workflow-dedup id and the prediction ledger on Chemclaw3's own
  `require_canonical_smiles`, applied *there*. This copy governs what this server accepts and what
  it echoes back in `screened` — values travelling to the agent, which Chemclaw3 re-keys before
  anything persistent sees them.
- **`tests/test_canonicalization_contract.py` makes a divergence detectable**, as literal strings
  derived by running Chemclaw3's own function. The same table passes in all three places without any
  of them importing the others, so whichever side moves first turns a test red instead of quietly
  answering differently.

**What was deliberately left behind**, so nobody restores it believing it was an oversight:
Chemclaw3's module also carries the `standardize` pipeline that answers the *other* question, "is
this the same compound?" (salts stripped, charges neutralized, one tautomer per set) plus
`compound_id` built on it, and `chem`'s copy additionally carries `molecular_weight` for its charge
table. None of the three tools here asks either question, so neither is present.
"""

from __future__ import annotations

from rdkit import Chem

__all__ = ["InvalidSmilesError", "require_canonical_smiles", "require_molecule"]


class InvalidSmilesError(ValueError):
    """A SMILES string RDKit cannot parse, or will only parse by silently truncating it.

    A `ValueError` on purpose, and load-bearing rather than stylistic:
    `mcp_server_kit.connector_app` lets a `ValueError` reach the model verbatim and replaces every
    other exception with a generic notice. These messages quote the string that was rejected, and a
    screen's refusal has to be actionable — a chemist told "internal error" would re-ask the same
    malformed question.
    """


def require_molecule(smiles: str) -> Chem.Mol:
    """The parsed molecule, raising `InvalidSmilesError` unless RDKit reads `smiles` **whole**.

    The one definition of "RDKit accepts this string, all of it". Three inputs RDKit accepts and
    this rejects, each measured against a real build in Chemclaw3 before being written down:

    - **A string with embedded whitespace.** The parser treats any whitespace as the end of the
      structure and ignores the rest, so `"CCO junk"` is ethanol. That is the silent-truncation
      class, and it is the worst thing that can happen to a hazard screen: measured on the
      Chemclaw3 build, `"CCO CN=[N+]=[N-]"` — an azide sitting in the ignored tail — screened with
      zero flags and the verdict "No rule in the hazard table matched".
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
    stay two. The ICH lookup relies on the strictness to tell "the chemist typed a structure" from
    "the chemist typed a name these tables do not know"; a lenient canonicalizer returns its input
    unparsed, which would make every unknown query resolve to itself as a fabricated structure.
    """
    return str(Chem.MolToSmiles(require_molecule(smiles)))
