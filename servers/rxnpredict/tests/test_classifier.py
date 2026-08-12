"""Tests for the SMARTS-based reaction classifier."""

from __future__ import annotations

import pytest

pytest.importorskip("rdkit")

from chemclaw_mcp_rxnpredict.engine.meta.classifier import (
    CLASS_AMIDE_FORMATION,
    CLASS_ESTERIFICATION,
    CLASS_NITRATION,
    CLASS_OTHER,
    CLASS_REDUCTION,
    CLASS_SUZUKI,
    classify_reaction,
)


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
