"""The cost bounds on tautomer and degradant enumeration, driven on the molecules that found them.

Before these bounds, polyglycine at 1,985 heavy atoms — inside `MAX_MOLECULE_ATOMS` — cost 11.6 s in
`enumerate_tautomer_set`, the same again inside `describe_molecule`, and 22.5 s in
`enumerate_degradant_candidates`, each holding a worker thread well into the 30 s request budget
and burning on after the caller had gone. The output caps (`MAX_TAUTOMERS`, `MAX_DEGRADANTS`) were
consulted only after all of that work, so they bounded the answer and not the cost.
"""

from __future__ import annotations

import time

import pytest
from chemclaw_mcp_chem.engine import species
from chemclaw_mcp_chem.engine.species import (
    TautomerCostRefused,
    describe_molecule,
    enumerate_degradant_candidates,
    enumerate_tautomer_set,
)

#: 1,985 heavy atoms: the molecule the review drove, just inside the parse bound.
POLYGLYCINE = "N" + "CC(=O)N" * 495 + "CC(=O)O"

#: A refusal costs a parse and a count, never the enumeration; generous against a slow runner.
PROMPT_SECONDS = 2.0


def test_a_tautomer_enumeration_past_the_bound_is_refused_before_it_runs() -> None:
    """Named refusal, naming the knob, in the time a parse takes rather than 11.6 s."""
    started = time.perf_counter()
    with pytest.raises(TautomerCostRefused, match="CHEMCLAW_CHEM_MAX_TAUTOMER_HEAVY_ATOMS"):
        enumerate_tautomer_set(POLYGLYCINE)
    assert time.perf_counter() - started < PROMPT_SECONDS


def test_describe_molecule_answers_a_large_molecule_without_counting_its_tautomers() -> None:
    """Null with `computed` false — not `saturated`, which would claim it is tautomeric."""
    started = time.perf_counter()
    topology = describe_molecule(POLYGLYCINE)
    assert time.perf_counter() - started < PROMPT_SECONDS
    assert topology.tautomer_count is None
    assert topology.tautomer_count_computed is False
    assert topology.tautomer_count_saturated is False
    assert topology.heavy_atom_count == 1985, "every other field must still be answered"


def test_a_molecule_inside_the_bound_is_still_counted() -> None:
    """The bound refuses the cost, not the question: acetylacetone and a 100-mer still count."""
    small = describe_molecule("CC(=O)CC(C)=O")
    assert small.tautomer_count_computed is True
    assert small.tautomer_count is not None and small.tautomer_count > 1

    peptide = "N" + "CC(=O)N" * 99 + "CC(=O)O"
    assert describe_molecule(peptide).heavy_atom_count <= species.MAX_TAUTOMER_HEAVY_ATOMS
    counted = describe_molecule(peptide)
    assert counted.tautomer_count_computed is True
    assert counted.tautomer_count_saturated is True, "a 100-mer has more forms than the cap"


def test_a_degradant_enumeration_past_the_bound_is_refused_before_any_product_is_built() -> None:
    """496 amide matches on 1,985 atoms was 22.5 s of product building before the count cap."""
    started = time.perf_counter()
    with pytest.raises(ValueError, match="CHEMCLAW_CHEM_MAX_DEGRADANT_MATCH_ATOM_PRODUCT"):
        enumerate_degradant_candidates(POLYGLYCINE)
    assert time.perf_counter() - started < PROMPT_SECONDS


def test_a_drug_sized_parent_is_still_enumerated() -> None:
    """Paracetamol: one amide, one phenol — the ordinary case the bound must not touch."""
    assert enumerate_degradant_candidates("CC(=O)Nc1ccc(O)cc1").count > 0
