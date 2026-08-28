"""Tests for SMILES canonicalisation and reaction parsing."""

from __future__ import annotations

import pytest

rdkit = pytest.importorskip("rdkit")

from chemclaw_mcp_rxnpredict.engine.preprocessing import (  # noqa: E402
    build_reaction_smiles,
    canonical_multi_smiles,
    canonical_reaction_input,
    canonical_smiles,
    parse_reaction,
)


def test_canonical_smiles_idempotent():
    assert canonical_smiles("CCO") == canonical_smiles("OCC")


def test_canonical_smiles_invalid():
    with pytest.raises(ValueError):
        canonical_smiles("not_a_smiles_string!!!")


def test_canonical_multi_smiles_sorts():
    out = canonical_multi_smiles("CCO.CC")
    # Order is sorted alphabetically of canonical forms
    parts = out.split(".")
    assert parts == sorted(parts)


def test_parse_reaction_full():
    r, a, p = parse_reaction("CC(=O)Cl.Nc1ccccc1>>CC(=O)Nc1ccccc1")
    assert r == "CC(=O)Cl.Nc1ccccc1"
    assert a == ""
    assert p == "CC(=O)Nc1ccccc1"


def test_parse_reaction_reactants_only():
    r, a, p = parse_reaction("CC(=O)Cl.Nc1ccccc1")
    assert r == "CC(=O)Cl.Nc1ccccc1"
    assert a == p == ""


def test_canonical_reaction_input_preserves_segments():
    # Reactants-only and reactants-with-agents must canonicalise differently.
    bare = canonical_reaction_input("CC(=O)Cl.Nc1ccccc1")
    with_agent = canonical_reaction_input("CC(=O)Cl.Nc1ccccc1>CCN(CC)CC>")
    assert bare != with_agent
    # Agent segment is preserved (two '>' present).
    assert with_agent.count(">") == 2


def test_canonical_reaction_input_canonicalises_each_segment():
    out = canonical_reaction_input("OCC>OCC>")
    # both the reactant and agent segments canonicalise ethanol identically
    left, agent, right = out.split(">")
    assert left == canonical_smiles("CCO")
    assert agent == canonical_smiles("CCO")
    assert right == ""


def test_build_reaction_smiles_canonicalises_both_sides():
    out = build_reaction_smiles("OCC.CC(=O)Cl", "CC(=O)OCC")
    left, agents, right = out.split(">")
    # Reactants and product must each be canonical
    for side in (left, right):
        for smi in side.split("."):
            assert canonical_smiles(smi) == smi
    assert agents == ""


def test_canonical_smiles_refuses_a_megamolecule_not_crash():
    """A 20k-atom SMILES is refused before `MolToSmiles`, which would segfault the process.

    `MolToSmiles` overflows the C stack (uncatchable SIGSEGV) on a large linear molecule; the bound
    in `canonical_smiles` (via `mcp_server_kit.limits`) turns it into a `ValueError`. This process
    surviving to assert is the regression proof. The atom bound also bites under the char bound.
    """
    with pytest.raises(ValueError):
        canonical_smiles("C" * 20000)
    with pytest.raises(ValueError):
        canonical_smiles("C" * 3000)
    assert canonical_smiles("CCO") == "CCO"


def test_an_invalid_megastring_is_not_echoed_in_full():
    """A 500 KB invalid SMILES must not be echoed whole into the refusal (log/context flood)."""
    payload = "not_a_smiles!" * 50_000
    with pytest.raises(ValueError) as raised:
        canonical_smiles(payload)
    assert payload not in str(raised.value)
