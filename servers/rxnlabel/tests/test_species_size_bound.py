"""A megamolecule is dropped, not canonicalised.

`MolToSmiles` overflows the C stack (an uncatchable SIGSEGV) on a large linear molecule, so the
size bound from `mcp_server_kit.limits` makes it another "could not be read" and a corpus scan
keeps going. This process surviving to assert is the proof.
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
