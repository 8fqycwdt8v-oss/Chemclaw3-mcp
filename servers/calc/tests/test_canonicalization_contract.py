"""The canonicalization contract with Chemclaw3, written as literal strings on both sides.

`engine/chem.py` is a **copy** of a definition Chemclaw3 owns, and it is the third copy in this
repository — `servers/chem/` and `servers/safety/` carry the first two. Copying a definition is
normally how two answers to one question appear, and neither repository may import the other, so the
contract is written as **data**: an input and the exact string it must canonicalize to. Every
expected value below was produced by running Chemclaw3's own function, not by reading the code and
reasoning about it:

    PYTHONPATH=/path/to/Chemclaw3/src /path/to/Chemclaw3/.venv/bin/python -c \\
        "from chemclaw.core.chem import require_canonical_smiles as f; print(f('CC(O)=CC(C)=O'))"

The table is deliberately the *same table* the other two servers carry, so whichever copy moves
first turns a test red instead of quietly answering differently.

**Why the stakes are higher on this server than on either of the others.** Both of their contract
files say "nothing here derives a cache key". This one does. `structure_from_smiles` canonicalizes
*before* embedding — atom order steers the seeded ETKDG geometry — and that geometry's hash is the
`input_hash` of every `xtb.*` key this server emits; `pka`, `solubility` and `descriptors` hash the
canonical string directly. A divergence here would not merely make two systems echo two spellings of
one molecule: it would produce a `CalculationKey` addressing a row that does not exist, and a
prediction Chemclaw3's calibration ledger never reconciles — which reports as `UNCALIBRATED` rather
than as an error. See `tests/test_key_contract.py` for the other half of that contract.
"""

from __future__ import annotations

import pytest
from chemclaw_mcp_calc.engine.chem import InvalidSmilesError, require_canonical_smiles

# (what a caller writes, what Chemclaw3's require_canonical_smiles returns for it).
CONTRACT: list[tuple[str, str]] = [
    # Tautomers: two structures, two strings. Never collapsed.
    ("CC(=O)CC(C)=O", "CC(=O)CC(C)=O"),
    ("CC(O)=CC(C)=O", "CC(=O)C=C(C)O"),
    ("Oc1ccncc1", "Oc1ccncc1"),
    ("O=c1cc[nH]cc1", "O=c1cc[nH]cc1"),
    # Charged species: the anion and its conjugate acid are different molecules — and on this
    # server, two different calculations at two different electron counts. `Structure` validates a
    # declared charge against the SMILES for exactly this reason, so collapsing them here would make
    # a submitted acetate silently compute acetic acid.
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
    # Salts: every fragment kept, in one fixed order whichever way the input was written. Order
    # independence is what this server needs from the row — a salt written two ways must reach one
    # key — and keeping the counter-ion is what `solubility`'s applicability-domain check then reads
    # to refuse it, rather than silently predicting the free base.
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
    # Two ordinary bench reagents, kept from the shared table so the three copies of this file
    # compare the same strings.
    ("Cc1ccc(cc1)S(Cl)(=O)=O", "Cc1ccc(S(=O)(=O)Cl)cc1"),
    ("OC(=O)c1ccccc1", "O=C(O)c1ccccc1"),
]

# Strings RDKit accepts and this definition refuses. The strictness is half the contract: RDKit
# reads up to the first whitespace and calls "CCO junk" ethanol, so a lenient parse does not fail,
# it narrows to a *different, smaller* molecule than the caller submitted — which on a calculator is
# a real, converged energy for a molecule nobody asked about, stored under a key naming the one they
# did.
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
