"""A megamolecule is dropped, not canonicalised — this server eats a multi-million-row corpus.

`read_molecule` is lenient: an unusable species returns `None` so a corpus scan keeps the other
forty-nine. But leniency must not reach `MolToSmiles`, which overflows the C stack (an uncatchable
SIGSEGV) on a large linear molecule — a single such row would take the whole server down mid-scan.
The bound (via `mcp_server_kit.limits`) makes the oversize case just another "could not be read".
This process surviving to assert is the regression proof.
"""

from __future__ import annotations

from chemclaw_mcp_rxnlabel.engine import species
from chemclaw_mcp_rxnlabel.engine.chem import read_molecule


def test_a_megamolecule_reads_as_none_not_a_crash() -> None:
    """20k atoms (over the char bound) and 3000 atoms (over the atom bound) both read as None."""
    assert read_molecule("C" * 20000) is None
    assert read_molecule("C" * 3000) is None
    assert species.canonical_smiles("C" * 20000) is None
    assert species.canonical_smiles("C" * 3000) is None


def test_a_real_species_still_reads() -> None:
    """The bound must not touch an ordinary reagent."""
    assert species.canonical_smiles("CCO") == "CCO"
    assert read_molecule("c1ccccc1") is not None
