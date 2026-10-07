"""The canonicalization contract with Chemclaw3, written as literal strings on both sides.

`engine/chem.py` copies a definition Chemclaw3 owns, where it keys the calculation cache and the
prediction ledger. Neither repository can import the other, so the contract is data: an input
and the exact string Chemclaw3's own `require_canonical_smiles` produced for it. The table is the
same one `servers/chem/tests/test_canonicalization_contract.py` carries, so whichever copy moves
first turns a test red. `screened` is the string a caller keys a hazard result on, so a
divergence would make two systems disagree about which molecule was screened.
"""

from __future__ import annotations

import pytest
from chemclaw_mcp_safety.engine.chem import InvalidSmilesError, require_canonical_smiles

# (what a caller writes, what Chemclaw3's require_canonical_smiles returns for it).
CONTRACT: list[tuple[str, str]] = [
    # Tautomers: two structures, two strings. Never collapsed.
    ("CC(=O)CC(C)=O", "CC(=O)CC(C)=O"),
    ("CC(O)=CC(C)=O", "CC(=O)C=C(C)O"),
    ("Oc1ccncc1", "Oc1ccncc1"),
    ("O=c1cc[nH]cc1", "O=c1cc[nH]cc1"),
    # Charged species: the anion and its conjugate acid are different molecules — and on this
    # server, different screens. The anionic peroxide and the hydrazinium salt are the two rules
    # this table's charge rows are load-bearing for.
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
    # Salts: every fragment kept, in one fixed order however the input was written. Many rule
    # reference molecules here (sodium azide, peroxide, chloramine-T, hydrazinium salts) are salts.
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
    # Two reagents from the vendored table this server resolves ICH queries through.
    ("Cc1ccc(cc1)S(Cl)(=O)=O", "Cc1ccc(S(=O)(=O)Cl)cc1"),
    ("OC(=O)c1ccccc1", "O=C(O)c1ccccc1"),
]

# Strings RDKit accepts and this definition refuses. RDKit reads up to the first whitespace, so a
# lenient parse would screen a smaller molecule than the caller submitted and report it clean.
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
    """One row of the contract. A failure here means the copies now disagree."""
    assert require_canonical_smiles(written) == canonical


def test_canonicalization_is_idempotent() -> None:
    """Canonicalizing a canonical string returns it unchanged — the property every key relies on."""
    for _, canonical in CONTRACT:
        assert require_canonical_smiles(canonical) == canonical


@pytest.mark.parametrize("written", REFUSED)
def test_a_string_rdkit_would_truncate_is_refused(written: str) -> None:
    """The negative half of the contract, and the half RDKit itself does not enforce."""
    with pytest.raises(InvalidSmilesError):
        require_canonical_smiles(written)


def test_a_megamolecule_is_refused_not_crashed() -> None:
    """A 20k-atom SMILES is refused before canonicalisation, not a segfault that kills the pod.

    Every screen canonicalises through `require_molecule`, whose bound (via `mcp_server_kit.limits`)
    turns an uncatchable C-stack overflow in `MolToSmiles` into a refusal. This process surviving
    to assert is the proof.
    """
    with pytest.raises(InvalidSmilesError):
        require_canonical_smiles("C" * 20000)
    with pytest.raises(InvalidSmilesError):
        require_canonical_smiles("C" * 3000)
    assert require_canonical_smiles("CCO") == "CCO"
