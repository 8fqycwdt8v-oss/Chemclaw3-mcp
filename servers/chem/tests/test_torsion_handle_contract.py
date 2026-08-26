"""The torsion-handle contract with Chemclaw3, asserted from this side.

Chemclaw3 checks the handles this server mints, and neither repository may import the other — so
the definition is written twice and this table is what makes a divergence detectable. The same
literals are asserted in Chemclaw3's `tests/test_torsion_handle.py`, so whichever side moves first
turns a test red instead of the two quietly answering differently. Exactly the arrangement
`test_canonicalization_contract.py` already has for `require_canonical_smiles`, applied to the one
other value that crosses between the repositories.

**Why this matters more than a canonical SMILES does.** A canonical form that drifts fragments a
cache: work is repeated, which is waste. A torsion handle that drifts is *accepted for the wrong
bond* on one side, which is a rotational profile of a different bond reported as an answer.
"""

from __future__ import annotations

import pytest
from chemclaw_mcp_chem.engine.torsions import enumerate_torsion_candidates, torsion_handle
from rdkit import Chem

# `(SMILES, bond, handle)` — the identical table Chemclaw3 asserts, under rdkit 2026.03.5.
CONTRACT = [
    ("CC(=O)Nc1ccccc1", (1, 3), "tor_d139107cd84f9333"),
    ("O=C(C)Nc1ccccc1", (1, 3), "tor_d139107cd84f9333"),
    ("c1ccc(NC(C)=O)cc1", (4, 5), "tor_d139107cd84f9333"),
    ("CCCC", (1, 2), "tor_6b25409b2bd410a6"),
    ("c1ccc(-c2ccccc2)cc1", (3, 4), "tor_17935ce6ec9a1219"),
    ("Cc1ccc(C)cc1", (0, 1), "tor_7b6b88fe5991e188"),
    ("Cc1ccc(C)cc1", (4, 5), "tor_7b6b88fe5991e188"),
]


@pytest.mark.parametrize(("smiles", "bond", "handle"), CONTRACT)
def test_the_handle_is_the_one_both_repositories_agree_on(
    smiles: str, bond: tuple[int, int], handle: str
) -> None:
    """Literals, not a recomputation: a test that derives the expected value proves nothing."""
    assert torsion_handle(Chem.MolFromSmiles(smiles), bond) == handle


@pytest.mark.parametrize(("smiles", "bond", "handle"), CONTRACT)
def test_the_enumeration_reports_the_same_handle_it_would_mint(
    smiles: str, bond: tuple[int, int], handle: str
) -> None:
    """The tool's own output is what Chemclaw3 receives, so that is what has to match the table.

    A handle that is right in the helper and wrong in the listing would be a contract that holds
    everywhere except where it is used.
    """
    listed = {torsion.torsion_id for torsion in enumerate_torsion_candidates(smiles)}
    assert handle in listed
