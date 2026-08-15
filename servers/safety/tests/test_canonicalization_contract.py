"""The canonicalization contract with Chemclaw3, written as literal strings on both sides.

`engine/chem.py` is a **copy** of a definition Chemclaw3 owns, and it is the second copy in this
repository — `servers/chem/` carries the first. Copying a definition is normally how two answers to
one question appear, and the copy is not free: on the Chemclaw3 side `require_canonical_smiles` keys
the calculation cache, the QM workflow-dedup id and the prediction ledger (D-011, "compute once,
never twice"), and 26 modules import it. If this copy drifts, a chemist comparing this server's
`screened` list with a Chemclaw3 note sees two spellings of one molecule and has no way to tell
which is right.

Neither repository can import the other — that is the point of the split — so the contract is
written as **data**: an input and the exact string it must canonicalize to. Every expected value
below was produced by running Chemclaw3's own function, not by reading the code and reasoning about
it:

    PYTHONPATH=/path/to/Chemclaw3/src /path/to/Chemclaw3/.venv/bin/python -c \\
        "from chemclaw.core.chem import require_canonical_smiles as f; print(f('CC(O)=CC(C)=O'))"

The table is deliberately the *same table* `servers/chem/tests/test_canonicalization_contract.py`
carries, and that is the property worth having: three copies of one definition, one table, so
whichever copy moves first turns a test red instead of quietly answering differently.

**Why this server needs the contract even though it stores nothing.** `screened` is this server's
whole answer to "which molecules is this result about" — a clean hazard screen is otherwise a
disclaimer with no subject — and it is the string a caller keys a result on. A divergence would not
corrupt a cache here; it would make two systems disagree about which molecule was screened, on a
result whose entire discipline is that it must never be read as being about the wrong one.
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
    # Salts: every fragment kept, in one fixed order whichever way the input was written. This is
    # the case this server leans on hardest — sodium azide, sodium peroxide, chloramine-T and the
    # hydrazinium salts are all multi-fragment, and every one of them is a rule's reference
    # molecule.
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

# Strings RDKit accepts and this definition refuses. The strictness is half the contract: RDKit
# reads up to the first whitespace and calls "CCO junk" ethanol, so a lenient parse does not fail,
# it narrows to a *different, smaller* molecule than the caller submitted — which on a hazard screen
# is a clean result about a molecule nobody asked about.
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
