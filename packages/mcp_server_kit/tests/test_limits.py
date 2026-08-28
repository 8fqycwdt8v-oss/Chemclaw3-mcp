"""The structural-size bound: a large molecule is refused before it can crash the canonicaliser.

`MolToSmiles` on a large linear molecule overflows the C stack (an uncatchable SIGSEGV), so the
bound is what stands between one ~20 KB authenticated call and the pod. These tests pin the two
independent limits and that the refusal message never echoes the offending megastring.
"""

from __future__ import annotations

from mcp_server_kit.limits import (
    MAX_MOLECULE_ATOMS,
    MAX_SMILES_CHARS,
    atom_count_error,
    smiles_length_error,
)


def test_a_short_string_is_within_bounds() -> None:
    """A real reagent SMILES is far under the limit and returns no reason."""
    assert smiles_length_error("CCO") is None


def test_an_over_length_string_is_refused() -> None:
    """A string past `MAX_SMILES_CHARS` is refused before it is ever parsed."""
    reason = smiles_length_error("C" * (MAX_SMILES_CHARS + 1))
    assert reason is not None
    assert str(MAX_SMILES_CHARS) in reason


def test_the_refusal_never_echoes_the_megastring() -> None:
    """The message quotes lengths, not the input — echoing 500 KB would defeat its own purpose."""
    payload = "N" * 500_000
    reason = smiles_length_error(payload)
    assert reason is not None
    assert payload not in reason


def test_a_small_molecule_is_within_the_atom_bound() -> None:
    """A molecule under `MAX_MOLECULE_ATOMS` returns no reason."""
    assert atom_count_error(3) is None


def test_an_over_large_molecule_is_refused() -> None:
    """The atom bound is the one that actually stops the segfault (recursion scales with atoms)."""
    reason = atom_count_error(MAX_MOLECULE_ATOMS + 1)
    assert reason is not None
    assert str(MAX_MOLECULE_ATOMS) in reason


def test_the_subject_is_named_in_the_message() -> None:
    """A refusal names what was rejected so a caller can act on it."""
    reason = smiles_length_error("C" * (MAX_SMILES_CHARS + 1), subject="component 4 of 9")
    assert reason is not None and "component 4 of 9" in reason
