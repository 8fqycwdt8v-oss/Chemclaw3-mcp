"""Canonical SMILES: "is this the same structure?", for this server's own input validation.

**Chemclaw3's `chemclaw/core/chem.py` is the authority for this definition, not this file.** This
is a copy, and it exists for one reason: a server in this repository must not import Chemclaw3 (it
is a separate process with a separate dependency closure, reachable only over MCP), while
`resolve_compound`, the charge table and the depiction all need to decide whether a string the
chemist typed is a molecule at all.

Copying a definition is normally how two answers to one question appear, so the split of duties has
to be stated rather than assumed:

- **Anything cache-keyed is canonicalized on the Chemclaw3 side.** D-011 ("compute once, never
  twice") keys the calculation cache, the QM workflow-dedup id and the prediction ledger on
  Chemclaw3's `require_canonical_smiles`, applied there, to the SMILES this server hands back. No
  key is ever derived here, so a divergence between the two copies cannot fragment a cache.
- **This copy governs exactly one thing: what this server accepts and what it echoes.** A structure
  returned by `resolve_compound` is a *value* travelling to the agent, which Chemclaw3 re-keys
  before it reaches anything persistent.

That bounds the blast radius; it does not make divergence acceptable, because a chemist comparing
this server's answer with a Chemclaw3 note must see one string. The contract test beside this
module is what makes divergence *detectable*: a table of inputs with their expected canonical output
written out as literal strings, derived by running Chemclaw3's own `require_canonical_smiles`. The
same table can be run in either repository without one importing the other, so whichever side moves
first — a pipeline change here, an RDKit upgrade there — turns a test red instead of quietly
answering differently.

**What was deliberately left behind.** Chemclaw3's module also carries the `standardize` pipeline
that answers the *other* question, "is this the same compound?" — salts stripped, charges
neutralized, one tautomer per set — plus `compound_id` built on it. None of chem's four tools ever
asked that question, so none of it is here: it keys the knowledge graph and the fingerprint index,
which are Chemclaw3's and stay Chemclaw3's.

**One spelling is added on top of the copy, not changed inside it**: `require_dative_free_smiles`,
what `resolve_compound` hands back. RDKit's sanitizer turns a metal-ligand bond written as a plain
single bond to an over-valent donor (`[NH2]` of a Buchwald palladacycle, `[NH3]` of cisplatin) into
a *dative* bond, and its writer then spells that bond `<-`/`->`. That notation is legal and
round-trips through RDKit, but it was measured going wrong in the agent's hands: handed
`[Pd]2(<-[NH2]...)` it re-typed `<-NH2`, which nothing parses, and then reasoned about whether the
compound id had changed. So the resolved structure writes each dative bond as the charge-separated
single bond it stands for (`[Pd-]...[NH2+]`), a spelling every SMILES reader accepts. It is the
*same structure*, measured against Chemclaw3 rather than argued: its `require_canonical_smiles`
returns the charge-separated string unchanged, and its std12 `compound_id` is identical for the raw,
the dative and the charge-separated spellings (see `tests/test_canonicalization_contract.py`).
`require_canonical_smiles` itself stays a byte-for-byte copy, because its contract table must pass
in both repositories.
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

    A `ValueError` on purpose, and that is load-bearing here rather than a stylistic echo of
    Chemclaw3's `ChemclawError`: `mcp_server_kit.connector_app` lets a `ValueError` reach the model
    verbatim and replaces every other exception with a generic notice. These messages are written
    for the chemist — they quote the string that was rejected — so they must be in the family that
    passes through.
    """


def require_molecule(smiles: str) -> Chem.Mol:
    """The parsed molecule, raising `InvalidSmilesError` unless RDKit reads `smiles` **whole**.

    The one definition of "RDKit accepts this string, all of it". Three inputs RDKit accepts and
    this rejects, each measured against a real build in Chemclaw3 before being written down:

    - **A string with embedded whitespace.** The parser treats any whitespace as the end of the
      structure and ignores the rest, so `"CCO junk"` is ethanol. That is the silent-truncation
      class: a malformed or concatenated string does not fail, it narrows to a *different, smaller
      molecule* than the caller submitted.
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

    The two checks `require_molecule` makes *before* RDKit sees the string, extracted because the
    depiction needs them on a **reaction** too and could not get them from `require_molecule`: a
    reaction branches on `">>"` before any molecule is parsed, so `"CCO>>CC=O CCCCCCBr"` was drawn
    as `CCO >> CC=O` — the bromide silently gone, the picture well-formed and plausible, and a
    drawing is the one form in which the model's choice is supposed to become checkable by a human.

    Args:
        smiles: The string as the caller typed it.
        what: What the string was meant to be, for the message — a refusal has to say which of the
            two it was reading, since the caller wrote `">>"` precisely to say.

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

    Spelling only: `"CCO"` and `"OCC"` collapse to one string, while an anion and its conjugate
    acid stay two — which is the property every caller here relies on. `resolve_compound` uses the
    strictness to tell "the chemist typed a structure" from "the chemist typed a name this table
    does not know", and getting that wrong in the lenient direction would resolve every unknown
    name to itself as a fabricated structure.
    """
    return str(Chem.MolToSmiles(require_molecule(smiles)))


def require_dative_free_smiles(smiles: str) -> str:
    """Canonical SMILES with each dative bond written as a charge-separated single bond.

    `require_canonical_smiles` with one difference, and only for a structure that has a dative bond:
    each `D->A` becomes `[D+]-[A-]` before the string is written, with the hydrogen count of both
    ends held where it was so no atom gains or loses one. Every other input gets exactly
    `require_canonical_smiles`'s string. The output is a fixed point — it parses with no rewritable
    dative bond left — so resubmitting what this returns returns it again.

    The charge-separated form rather than a plain single bond, because a single bond does not
    survive every donor: a pyridine nitrogen bonded to palladium is a four-valent aromatic `n`
    that RDKit refuses outright, while `[n+]` beside `[Pd-2]` parses.

    **A donor that cannot carry the charge keeps its arrow.** An alkene or arene written as a
    π-donor (`C=C->[Pt]`, `c1ccccc1->[Cr]`) would need a four-bonded carbocation or a
    non-kekulizable ring; there is no single-bond spelling of that bond that means the same thing,
    and writing a sigma metal-carbon bond instead would hand back a different compound. Each bond is
    rewritten on its own, so one such donor leaves the others in the molecule rewritten.

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

    Average rather than monoisotopic, because a charge table is what somebody weighs on a balance —
    `CalcExactMolWt` would answer a mass-spectrometry question instead, and differs by enough to
    matter on a chlorinated reagent.

    The `type: ignore` is `rdkit-stubs`' doing, not a claim about this call: `Descriptors.MolWt` is
    assigned as a lambda in `rdkit/Chem/Descriptors.py`, and the stub package omits every descriptor
    defined that way. `tests/test_tools.py` pins two molecular weights as literals so the ignored
    line is still covered by a number rather than by a promise.

    Raises:
        InvalidSmilesError: `smiles` does not parse.
    """
    return float(Descriptors.MolWt(require_molecule(smiles)))  # type: ignore[attr-defined]
