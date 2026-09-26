"""The cost bounds on the species enumerations, driven on the molecules that found them.

Before these bounds, polyglycine at 1,985 heavy atoms — inside `MAX_MOLECULE_ATOMS` — cost 11.6 s in
`enumerate_tautomer_set`, the same again inside `describe_molecule`, and 22.5 s in
`enumerate_degradant_candidates`, each holding a worker thread well into the 30 s request budget
and burning on after the caller had gone. The output caps (`MAX_TAUTOMERS`, `MAX_DEGRADANTS`) were
consulted only after all of that work, so they bounded the answer and not the cost.

`enumerate_stereoisomer_set` had the same shape one step removed: `maxIsomers` stopped it building
more than 65 isomers, but each is canonicalised over the whole graph, so a 1,991-atom polyol built
its 65 and was refused after 10.2 s. `MAX_STEREO_ISOMER_ATOM_PRODUCT` prices `isomers x atoms`.
"""

from __future__ import annotations

import time

import pytest
from chemclaw_mcp_chem.engine import species
from chemclaw_mcp_chem.engine.chem import require_molecule
from chemclaw_mcp_chem.engine.species import (
    TautomerCostRefused,
    describe_molecule,
    enumerate_degradant_candidates,
    enumerate_stereoisomer_set,
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


#: 1,991 heavy atoms, 994 open centres: the polyol the band's ADR measured at 10.2 s, then refused.
POLYOL = "C" + "C(O)" * 995

#: Four open centres — 16 isomers, 2^4 — at the head of a chain padded to the size a test needs.
_FOUR_CENTRES = "CC(O)C(N)C(Cl)C(F)"


def _four_centres_on(heavy_atoms: int) -> str:
    """Four open centres on a molecule of exactly `heavy_atoms` heavy atoms."""
    return _FOUR_CENTRES + "C" * (heavy_atoms - 9)


def _price(smiles: str) -> int:
    """`isomers the enumerator would build x heavy atoms`, as the bound reads it."""
    mol = require_molecule(smiles)
    built = min(1 << species._open_stereo_elements(mol), species.MAX_STEREOISOMERS + 1)
    return built * int(mol.GetNumHeavyAtoms())


def test_a_stereo_enumeration_at_the_bound_is_answered() -> None:
    """Exactly `MAX_STEREO_ISOMER_ATOM_PRODUCT` is inside it: the set comes back whole."""
    at = _four_centres_on(species.MAX_STEREO_ISOMER_ATOM_PRODUCT // 16)
    assert species._open_stereo_elements(require_molecule(at)) == 4, "the fixture moved"
    assert _price(at) == species.MAX_STEREO_ISOMER_ATOM_PRODUCT, "the fixture is not at the bound"
    assert enumerate_stereoisomer_set(at).count == 16


def test_a_stereo_enumeration_just_over_the_bound_is_refused_before_it_runs() -> None:
    """One heavy atom more — sixteen units over — is refused, naming both numbers and the knob."""
    over = _four_centres_on(species.MAX_STEREO_ISOMER_ATOM_PRODUCT // 16 + 1)
    assert _price(over) == species.MAX_STEREO_ISOMER_ATOM_PRODUCT + 16
    with pytest.raises(ValueError, match="CHEMCLAW_CHEM_MAX_STEREO_ISOMER_ATOM_PRODUCT") as refused:
        enumerate_stereoisomer_set(over)
    message = str(refused.value)
    assert "4 open stereo elements" in message
    assert "build 16 isomers" in message
    assert f"{species.MAX_STEREO_ISOMER_ATOM_PRODUCT:,}" in message


def test_a_stereo_enumeration_far_over_the_bound_is_refused_in_the_time_a_parse_takes() -> None:
    """The 1,991-atom polyol: 21.6x over, refused promptly rather than after building 65 isomers.

    The quote of the caller's 3,976-character SMILES goes through `echo`, so the refusal is the size
    of a sentence rather than the size of the polymer.
    """
    started = time.perf_counter()
    with pytest.raises(ValueError, match="CHEMCLAW_CHEM_MAX_STEREO_ISOMER_ATOM_PRODUCT") as refused:
        enumerate_stereoisomer_set(POLYOL)
    assert time.perf_counter() - started < PROMPT_SECONDS
    message = str(refused.value)
    assert f"{65 * 1991 / species.MAX_STEREO_ISOMER_ATOM_PRODUCT:.1f}x" in message
    assert len(message) < len(POLYOL) / 4, "the refusal quoted the whole polymer back"


def test_a_large_molecule_with_nothing_open_is_still_enumerated() -> None:
    """The bound prices the isomers, not the size: polyglycine has no open centre and answers."""
    assert enumerate_stereoisomer_set(POLYGLYCINE).count == 1
    assert enumerate_stereoisomer_set("CC(O)CC").count == 2, "2-butanol, the smallest question"
