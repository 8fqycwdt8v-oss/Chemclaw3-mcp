"""The torsion-handle contract with Chemclaw3, asserted from this side.

Neither repository imports the other, so the definition is written twice and the same literals
are asserted in Chemclaw3's `tests/test_torsion_handle.py`. A drifted handle would be accepted
for the wrong bond on one side, reporting the rotational profile of a different bond.
"""

from __future__ import annotations

import pytest
from chemclaw_mcp_chem.engine.torsions import enumerate_torsion_candidates, torsion_handle
from rdkit import Chem

# `(SMILES, bond, handle)` — the identical table Chemclaw3 asserts, under rdkit 2026.09.1.
CONTRACT = [
    ("CC(=O)Nc1ccccc1", (1, 3), "tor_a09b24d328061b96"),
    ("O=C(C)Nc1ccccc1", (1, 3), "tor_a09b24d328061b96"),
    ("c1ccc(NC(C)=O)cc1", (4, 5), "tor_a09b24d328061b96"),
    ("CCCC", (1, 2), "tor_504719db4695b70e"),
    ("c1ccc(-c2ccccc2)cc1", (3, 4), "tor_54e766936ffdb5ab"),
    ("Cc1ccc(C)cc1", (0, 1), "tor_8a49a44e671d07de"),
    ("Cc1ccc(C)cc1", (4, 5), "tor_8a49a44e671d07de"),
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
