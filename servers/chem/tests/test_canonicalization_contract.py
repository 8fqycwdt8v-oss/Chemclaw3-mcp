"""The canonicalization contract with Chemclaw3, written as literal strings on both sides.

`engine/chem.py` copies a definition Chemclaw3 owns and keys its cache and ledger on. Neither
repository imports the other, so the contract is data: inputs and the exact strings Chemclaw3's
own `chemclaw.core.chem.require_canonical_smiles` produced when run. The same table must pass in
Chemclaw3 unchanged, so whichever side moves first goes red.

Each row is a genuinely ambiguous case:

- **Tautomers stay apart.** Tautomer canonicalization is deliberately not ported.
- **Charge is preserved.** Acetate is not acetic acid.
- **Stereochemistry is preserved but re-anchored.** The two alanines stay two strings.
- **A salt keeps both fragments, in a fixed order**, written both ways round.
- **A kekulized aromatic collapses onto the aromatic form.**
"""

from __future__ import annotations

import pytest
from chemclaw_mcp_chem.engine.chem import (
    InvalidSmilesError,
    molecular_weight,
    require_canonical_smiles,
    require_dative_free_smiles,
)

# (what a caller writes, what Chemclaw3's require_canonical_smiles returns for it).
CONTRACT: list[tuple[str, str]] = [
    # Tautomers: two structures, two strings. Never collapsed.
    ("CC(=O)CC(C)=O", "CC(=O)CC(C)=O"),
    ("CC(O)=CC(C)=O", "CC(=O)C=C(C)O"),
    ("Oc1ccncc1", "Oc1ccncc1"),
    ("O=c1cc[nH]cc1", "O=c1cc[nH]cc1"),
    # Charged species: the anion and its conjugate acid are different calculations.
    ("CC(=O)[O-]", "CC(=O)[O-]"),
    ("CC(O)=O", "CC(=O)O"),
    ("C[N+](C)(C)C", "C[N+](C)(C)C"),
    ("C[N+](=O)[O-]", "C[N+](=O)[O-]"),
    # Stereocentres: preserved, and re-anchored to the canonical atom order.
    ("C[C@H](N)C(=O)O", "C[C@H](N)C(=O)O"),
    ("C[C@@H](N)C(=O)O", "C[C@@H](N)C(=O)O"),
    ("N[C@@H](C)C(O)=O", "C[C@H](N)C(=O)O"),
    ("C/C=C/C", "C/C=C/C"),
    ("C/C=C\\C", "C/C=C\\C"),
    # Salts: every fragment kept, in one fixed order whichever way the input was written.
    ("CC(=O)[O-].[Na+]", "CC(=O)[O-].[Na+]"),
    ("[Na+].CC(=O)[O-]", "CC(=O)[O-].[Na+]"),
    ("CCN.Cl", "CCN.Cl"),
    ("Cl.CCN", "CCN.Cl"),
    ("[K+].[K+].[O-]C([O-])=O", "O=C([O-])[O-].[K+].[K+]"),
    # Aromatics written kekulized collapse onto the aromatic form.
    ("C1=CC=CC=C1", "c1ccccc1"),
    ("c1ccccc1", "c1ccccc1"),
    ("C1=CC=NC=C1", "c1ccncc1"),
    ("C1=CC2=CC=CC=C2C=C1", "c1ccc2ccccc2c1"),
    # Two reagents from this server's own table, spelled the way a chemist types them.
    ("Cc1ccc(cc1)S(Cl)(=O)=O", "Cc1ccc(S(=O)(=O)Cl)cc1"),
    ("OC(=O)c1ccccc1", "O=C(O)c1ccccc1"),
]

# Strings RDKit accepts and this definition refuses. The strictness is half the contract: RDKit
# reads up to the first whitespace and calls "CCO junk" ethanol, so a lenient parse does not fail,
# it narrows to a *different, smaller* molecule than the caller submitted.
REFUSED: list[str] = [
    "CCO junk",
    "CCO\t1",
    "",
    "   ",
    "°C",
    "CC°",
    "not-a-molecule",
]


@pytest.mark.parametrize(("written", "canonical"), CONTRACT, ids=[case[0] for case in CONTRACT])
def test_the_canonical_form_matches_chemclaw3(written: str, canonical: str) -> None:
    """One row of the contract. A failure here means the two repositories now disagree."""
    assert require_canonical_smiles(written) == canonical


def test_canonicalization_is_idempotent() -> None:
    """Canonicalizing a canonical string returns it unchanged — the property every key relies on.

    Chemclaw3 canonicalizes again before keying, so a non-idempotent definition would key one
    molecule two ways.
    """
    for _, canonical in CONTRACT:
        assert require_canonical_smiles(canonical) == canonical


@pytest.mark.parametrize("written", REFUSED)
def test_a_string_rdkit_would_truncate_is_refused(written: str) -> None:
    """The negative half of the contract, and the half RDKit itself does not enforce."""
    with pytest.raises(InvalidSmilesError):
        require_canonical_smiles(written)


def test_a_megamolecule_is_refused_not_crashed() -> None:
    """A 20k-atom SMILES must be refused before canonicalisation, not segfault the process.

    `MolToSmiles` overflows the C stack on a large linear molecule; the bound in `require_molecule`
    turns that into a `ValueError`. This process surviving to assert is the proof. A real molecule
    still passes.
    """
    with pytest.raises(InvalidSmilesError):
        require_canonical_smiles("C" * 20000)
    # The atom bound bites below the character bound too: 3000 chars, 3000 atoms.
    with pytest.raises(InvalidSmilesError):
        require_canonical_smiles("C" * 3000)
    assert require_canonical_smiles("CCO") == "CCO"


# (what a caller writes, what `resolve_compound` returns, Chemclaw3's std12 `compound_id` of both).
#
# Not part of the shared table: `resolve_compound` returns the dative-free spelling, while
# Chemclaw3's canonicalizer writes the dative arrow. Measured with Chemclaw3's own functions:
#
#     from chemclaw.core.chem import require_canonical_smiles as rcs, compound_id
#     rcs(returned) == returned                      # True for every row
#     compound_id(written) == compound_id(returned)  # True for every row; the id is column three
#
# So the compound key does not move when the agent passes the answer on.
DATIVE: list[tuple[str, str, str]] = [
    (
        "CC(P(C(C)(C)C)C(C)(C)C)C1=C(C([Fe]C2C=CC=C2)C=C1)[P]([Pd]3(OS(C)(=O)=O)C4=CC=CC=C4"
        "C5=C([NH2]3)C=CC=C5)(C6CCCCC6)C7CCCCC7",
        "CC(C1=C([P](C2CCCCC2)(C2CCCCC2)[Pd-]2([O]S(C)(=O)=O)[NH2+]c3ccccc3-c3cccc[c]32)"
        "[CH]([Fe][CH]2C=CC=C2)C=C1)P(C(C)(C)C)C(C)(C)C",
        "compound-b4ed44b921ee",
    ),
    (
        "CC(C1=C([P](C2CCCCC2)(C2CCCCC2)[Pd]2(<-[NH2]c3ccccc3-c3cccc[c]32)[O]S(C)(=O)=O)"
        "[CH]([Fe][CH]2C=CC=C2)C=C1)P(C(C)(C)C)C(C)(C)C",
        "CC(C1=C([P](C2CCCCC2)(C2CCCCC2)[Pd-]2([O]S(C)(=O)=O)[NH2+]c3ccccc3-c3cccc[c]32)"
        "[CH]([Fe][CH]2C=CC=C2)C=C1)P(C(C)(C)C)C(C)(C)C",
        "compound-b4ed44b921ee",
    ),
    # An ammine written without an arrow and with one: RDKit perceives both as dative.
    ("[NH3][Pt]([NH3])(Cl)Cl", "[NH3+][Pt-2]([NH3+])([Cl])[Cl]", "compound-c7e0fbfd706b"),
    ("N->[Pt](<-N)(Cl)Cl", "[NH3+][Pt-2]([NH3+])([Cl])[Cl]", "compound-c7e0fbfd706b"),
    # An aromatic donor: the case a plain single bond cannot spell, since a four-valent `n` fails.
    (
        "Cl[Pd](Cl)(<-n1ccccc1)<-n1ccccc1",
        "[Cl][Pd-2]([Cl])([n+]1ccccc1)[n+]1ccccc1",
        "compound-6ea96b20af98",
    ),
    (
        "c1ccc(cc1)P(->[Pd](<-P(c1ccccc1)(c1ccccc1)c1ccccc1)(Cl)Cl)(c1ccccc1)c1ccccc1",
        "[Cl][Pd-2]([Cl])([P+](c1ccccc1)(c1ccccc1)c1ccccc1)[P+](c1ccccc1)(c1ccccc1)c1ccccc1",
        "compound-933600761ccd",
    ),
    # A pi donor keeps its arrow (no single-bond spelling means the same bond); the amine beside
    # it is still rewritten, because each bond is decided on its own.
    ("C=C->[Pt]<-N", "C=[CH2]->[Pt-][NH3+]", "compound-82a2cdf20772"),
]


@pytest.mark.parametrize(
    ("written", "returned"), [row[:2] for row in DATIVE], ids=[row[1][:40] for row in DATIVE]
)
def test_a_dative_bond_is_returned_charge_separated(written: str, returned: str) -> None:
    """One row of the dative table: the spelling the agent receives for a metal complex."""
    assert require_dative_free_smiles(written) == returned


@pytest.mark.parametrize(
    "returned", [row[1] for row in DATIVE], ids=[row[1][:40] for row in DATIVE]
)
def test_the_returned_spelling_is_a_fixed_point(returned: str) -> None:
    """Resubmitting the answer returns the answer, which is what makes it safe to pass on."""
    assert require_dative_free_smiles(returned) == returned


@pytest.mark.parametrize(("written", "returned"), [row[:2] for row in DATIVE])
def test_the_rewrite_moves_no_atom_and_no_hydrogen(written: str, returned: str) -> None:
    """A charge moved along a bond is a spelling; a hydrogen gained or lost would be a new compound.

    The molecular weight is the cheap witness for both, and it is what a charge table weighs.
    """
    assert molecular_weight(returned) == pytest.approx(molecular_weight(written))


@pytest.mark.parametrize(("written", "canonical"), CONTRACT, ids=[case[0] for case in CONTRACT])
def test_without_a_dative_bond_the_two_spellings_are_one(written: str, canonical: str) -> None:
    """The rewrite touches dative bonds and nothing else, so the contract table holds for it too."""
    assert require_dative_free_smiles(written) == canonical


@pytest.mark.parametrize("written", REFUSED)
def test_the_dative_free_spelling_refuses_what_the_canonical_one_refuses(written: str) -> None:
    """One parse gate for both spellings, so neither can accept what the other refuses."""
    with pytest.raises(InvalidSmilesError):
        require_dative_free_smiles(written)
