"""Tests for the SMARTS-based reaction classifier."""

from __future__ import annotations

import pytest

pytest.importorskip("rdkit")

from unittest.mock import patch

from chemclaw_mcp_rxnpredict.engine.meta import classifier as classifier_module
from chemclaw_mcp_rxnpredict.engine.meta.classifier import (
    CLASS_AMIDE_FORMATION,
    CLASS_ESTERIFICATION,
    CLASS_NITRATION,
    CLASS_OTHER,
    CLASS_REDUCTION,
    CLASS_SUZUKI,
    classify_reaction,
)
from rdkit import Chem


def test_amide_formation_from_acid_chloride():
    klass = classify_reaction(
        reactants="CC(=O)Cl.Nc1ccccc1",
        product="CC(=O)Nc1ccccc1",
    )
    assert klass == CLASS_AMIDE_FORMATION


def test_esterification():
    klass = classify_reaction(
        reactants="CC(=O)O.CCO",
        product="CCOC(C)=O",
    )
    assert klass == CLASS_ESTERIFICATION


def test_suzuki_coupling():
    klass = classify_reaction(
        reactants="Brc1ccccc1.OB(O)c1ccccc1",
        product="c1ccc(-c2ccccc2)cc1",
    )
    assert klass == CLASS_SUZUKI


def test_carbonyl_reduction():
    klass = classify_reaction(
        reactants="CC(C)=O.[Na+].[BH4-]",
        product="CC(C)O",
    )
    assert klass == CLASS_REDUCTION


def test_nitration():
    klass = classify_reaction(
        reactants="c1ccccc1.O=[N+]([O-])O",
        product="O=[N+]([O-])c1ccccc1",
    )
    assert klass == CLASS_NITRATION


def test_unknown_returns_other():
    # Random nonsense reactants -> no rule fires
    klass = classify_reaction(
        reactants="C(F)(F)F.C#N",
        product=None,
    )
    assert klass == CLASS_OTHER


def test_specific_class_wins_over_generic_substitution():
    """A reaction that is both an alkyl-halide+nucleophile (generic SN) AND a
    more specific named reaction should classify as the specific one, because
    the broad nucleophilic_substitution rule is evaluated last.

    Azidation of an alkyl bromide is a genuine SN reaction with no more-specific
    rule, so it must still classify as nucleophilic_substitution."""
    from chemclaw_mcp_rxnpredict.engine.meta.classifier import CLASS_NUCLEOPHILIC_SUBSTITUTION

    klass = classify_reaction(
        reactants="CCBr.[N-]=[N+]=[N-]",
        product="CCN=[N+]=[N-]",
    )
    assert klass == CLASS_NUCLEOPHILIC_SUBSTITUTION


def test_amide_not_shadowed_by_generic_substitution():
    """An acid-chloride aminolysis (which also contains a halide + N) must
    classify as amide_formation, not the generic substitution fallback."""
    klass = classify_reaction(
        reactants="CC(=O)Cl.Nc1ccccc1",
        product="CC(=O)Nc1ccccc1",
    )
    assert klass == CLASS_AMIDE_FORMATION


def test_classifier_ignores_invalid_smiles():
    # Should not raise even if RDKit refuses one of the inputs
    klass = classify_reaction(
        reactants="CC(=O)Cl.Nc1ccccc1.not_a_smiles",
        product="CC(=O)Nc1ccccc1",
    )
    assert klass == CLASS_AMIDE_FORMATION


def test_a_pattern_is_compiled_once_per_process_rather_than_once_per_call() -> None:
    """`Chem.MolFromSmarts` on the hot path is a parse paid per reaction, not per rule table.

    `classify_reaction` is called per reaction by the aggregator's trust-prior gating, and it used
    to compile every pattern of every rule tried, on every invocation. `servers/safety`'s
    `screen.py::_load_rules` has been `lru_cache`d over its whole rule table since it was written,
    for exactly this reason.

    Asserted by counting the *parses*, not by timing: a timing assertion on a 180 µs difference is a
    flake, and the property that matters is "compiled once", which is exact. The reaction chosen
    matches nothing, so every rule is tried and every pattern in the table is reached — a reaction
    that matched the first rule would leave most of the table uncompiled and pass vacuously.
    """
    parsed: list[str] = []
    real = Chem.MolFromSmarts

    def counting(smarts: str) -> object:
        parsed.append(smarts)
        return real(smarts)

    classifier_module._compiled.cache_clear()
    with patch.object(Chem, "MolFromSmarts", counting):
        for _ in range(5):
            assert classify_reaction("CCCCCCCC.CCCCCC", "CCCCCCCCCCCCCC") == CLASS_OTHER
    assert parsed, "no pattern was compiled at all — the rule table was not reached"
    assert len(parsed) == len(set(parsed)), (
        f"{len(parsed)} compilations for {len(set(parsed))} distinct patterns over five calls; "
        "the cache is not holding"
    )


def test_an_unparseable_pattern_is_warned_about_once_and_answers_no_match() -> None:
    """The miss path survived the cache, and stopped being a log line per call.

    `_compiled` returns `None` for a pattern RDKit rejects and `_any_mol_matches` reads that as no
    match, which is the behaviour a malformed constant must have: a rule that cannot be compiled
    must not silently match everything. What changed is that the warning is now issued on the
    compile rather than on the call.
    """
    classifier_module._compiled.cache_clear()
    assert classifier_module._compiled("this is not a SMARTS(((") is None
    assert not classifier_module._any_mol_matches([Chem.MolFromSmiles("CCO")], "((((")
