"""The canonicalization contract with Chemclaw3, written as literal strings on both sides.

`engine/chem.py` is a **copy** of a definition Chemclaw3 owns. Copying a definition is normally how
two answers to one question appear, and the copy here is not free: Chemclaw3's `core/chem.py` keys
the calculation cache, the QM workflow-dedup id and the prediction ledger on `canonical_smiles`
(D-011, "compute once, never twice"), and 26 modules import it. If this copy drifts, a chemist
comparing this server's `resolve_compound` output with a Chemclaw3 note sees two spellings of one
molecule and has no way to tell which is right.

Neither repository can import the other — that is the point of the split — so the contract is
written as **data**: an input and the exact string it must canonicalize to. Every expected value
below was produced by running Chemclaw3's own function, not by reading the code and reasoning about
it:

    PYTHONPATH=/path/to/Chemclaw3/src /path/to/Chemclaw3/.venv/bin/python -c \\
        "from chemclaw.core.chem import require_canonical_smiles as f; print(f('CC(O)=CC(C)=O'))"

Paste the same table into Chemclaw3 and it must pass there unchanged. Whichever side moves first —
an RDKit upgrade here, a pipeline change there — turns a test red instead of quietly answering
differently, which is the only property that makes the duplication defensible.

**What the cases are chosen to pin.** Each row is a place where "the same molecule" is genuinely
ambiguous and a canonicalizer has to make a choice:

- **Tautomers stay apart.** The keto and enol forms of acetylacetone are two strings here, and they
  must be: this is the *structure* question, not the compound question. Chemclaw3 answers the
  second one with `standard_smiles`, whose tautomer canonicalization is deliberately **not** ported
  (nothing on this server asks it). A future edit that "helpfully" adds it collapses these two rows.
- **Charge is preserved.** Acetate is not acetic acid. A calculation submitted for the anion must
  not silently compute the conjugate acid, and the row pair is what says so.
- **Stereochemistry is preserved but re-anchored.** `N[C@@H](C)C(O)=O` and `C[C@H](N)C(=O)O` are
  one molecule written from two atoms; `@@` and `@` are not interchangeable, and the two alanines
  must stay two strings.
- **A salt keeps both fragments, in a fixed order.** Fragment *ordering* is exactly what a naive
  canonicalizer gets wrong, so the two spellings of sodium acetate are written both ways round.
- **A kekulized aromatic collapses onto the aromatic form.** This is the case everyone expects
  canonicalization to handle, and it belongs here so that a change which broke it would be caught
  by the same table as the ones nobody expects.
"""

from __future__ import annotations

import pytest
from chemclaw_mcp_chem.engine.chem import InvalidSmilesError, require_canonical_smiles

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

    Cheap to state and load-bearing: Chemclaw3 canonicalizes again on its side before keying
    anything, so a definition that was not idempotent would key one molecule two ways depending on
    how many times it had been round-tripped.
    """
    for _, canonical in CONTRACT:
        assert require_canonical_smiles(canonical) == canonical


@pytest.mark.parametrize("written", REFUSED)
def test_a_string_rdkit_would_truncate_is_refused(written: str) -> None:
    """The negative half of the contract, and the half RDKit itself does not enforce."""
    with pytest.raises(InvalidSmilesError):
        require_canonical_smiles(written)
