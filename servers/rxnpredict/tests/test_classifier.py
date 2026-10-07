"""Tests for the SMARTS-based reaction classifier."""

from __future__ import annotations

import logging

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


def test_amide_formation_from_acid_chloride() -> None:
    klass = classify_reaction(
        reactants="CC(=O)Cl.Nc1ccccc1",
        product="CC(=O)Nc1ccccc1",
    )
    assert klass == CLASS_AMIDE_FORMATION


def test_esterification() -> None:
    klass = classify_reaction(
        reactants="CC(=O)O.CCO",
        product="CCOC(C)=O",
    )
    assert klass == CLASS_ESTERIFICATION


def test_suzuki_coupling() -> None:
    klass = classify_reaction(
        reactants="Brc1ccccc1.OB(O)c1ccccc1",
        product="c1ccc(-c2ccccc2)cc1",
    )
    assert klass == CLASS_SUZUKI


def test_carbonyl_reduction() -> None:
    klass = classify_reaction(
        reactants="CC(C)=O.[Na+].[BH4-]",
        product="CC(C)O",
    )
    assert klass == CLASS_REDUCTION


def test_nitration() -> None:
    klass = classify_reaction(
        reactants="c1ccccc1.O=[N+]([O-])O",
        product="O=[N+]([O-])c1ccccc1",
    )
    assert klass == CLASS_NITRATION


def test_unknown_returns_other() -> None:
    # Random nonsense reactants -> no rule fires
    klass = classify_reaction(
        reactants="C(F)(F)F.C#N",
        product=None,
    )
    assert klass == CLASS_OTHER


def test_specific_class_wins_over_generic_substitution() -> None:
    """A specific named reaction wins over generic nucleophilic substitution.

    The broad rule is evaluated last; azidation of an alkyl bromide has no more specific rule, so it
    still classifies as nucleophilic_substitution.
    """
    from chemclaw_mcp_rxnpredict.engine.meta.classifier import CLASS_NUCLEOPHILIC_SUBSTITUTION

    klass = classify_reaction(
        reactants="CCBr.[N-]=[N+]=[N-]",
        product="CCN=[N+]=[N-]",
    )
    assert klass == CLASS_NUCLEOPHILIC_SUBSTITUTION


def test_amide_not_shadowed_by_generic_substitution() -> None:
    """An acid-chloride aminolysis (which also contains a halide + N) must
    classify as amide_formation, not the generic substitution fallback."""
    klass = classify_reaction(
        reactants="CC(=O)Cl.Nc1ccccc1",
        product="CC(=O)Nc1ccccc1",
    )
    assert klass == CLASS_AMIDE_FORMATION


def test_classifier_ignores_invalid_smiles() -> None:
    # Should not raise even if RDKit refuses one of the inputs
    klass = classify_reaction(
        reactants="CC(=O)Cl.Nc1ccccc1.not_a_smiles",
        product="CC(=O)Nc1ccccc1",
    )
    assert klass == CLASS_AMIDE_FORMATION


def test_a_pattern_is_compiled_once_per_process_rather_than_once_per_call() -> None:
    """Each SMARTS pattern is compiled once per process, not once per call.

    `classify_reaction` runs per reaction on the aggregator's hot path. Asserted by counting parses,
    not timing. The reaction matches nothing, so every rule and pattern is reached; one matching the
    first rule would leave the table uncompiled and pass vacuously.
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


def test_an_unparseable_pattern_is_warned_about_once_and_answers_no_match(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unparseable pattern answers no match and is warned about once, on compile.

    A rule that cannot be compiled must never silently match everything.
    """
    classifier_module._compiled.cache_clear()
    assert classifier_module._compiled("this is not a SMARTS(((") is None
    ethanol = [Chem.MolFromSmiles("CCO")]
    with caplog.at_level(logging.WARNING, logger=classifier_module.__name__):
        for _ in range(5):
            assert not classifier_module._any_mol_matches(ethanol, "((((")
    warned = [
        record
        for record in caplog.records
        if record.name == classifier_module.__name__
        and record.getMessage() == "invalid SMARTS in classifier: '(((('"
    ]
    assert len(warned) == 1, f"{len(warned)} warnings over five calls for one bad pattern"
