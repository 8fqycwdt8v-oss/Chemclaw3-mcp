"""Lightweight SMARTS-based reaction classifier.

Used by the meta-model to look up per-class trust priors (Mixture-of-Experts
gating). Returns a single canonical class label for a (reactants, optional
product) pair, or `None` when no rule matches.

This is intentionally simple. For richer classification, swap in Rxn-INSIGHT's
classifier when available (it's already a registered conditions predictor and
exposes a `Reaction.get_class()` method on the wrapped object).

The labels here mirror the buckets we publish per-class priors for in
`Settings.model_trust_priors_by_class`. Add new entries as new classes are
calibrated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# Reaction class labels. Stable strings — referenced by the trust-prior map.
CLASS_AMIDE_FORMATION = "amide_formation"
CLASS_ESTERIFICATION = "esterification"
CLASS_SUZUKI = "suzuki_coupling"
CLASS_REDUCTION = "carbonyl_reduction"
CLASS_OXIDATION = "alcohol_oxidation"
CLASS_NUCLEOPHILIC_SUBSTITUTION = "nucleophilic_substitution"
CLASS_NITRATION = "aromatic_nitration"
CLASS_HALOGENATION = "aromatic_halogenation"
CLASS_HYDROLYSIS = "hydrolysis"
CLASS_OTHER = "other"


@dataclass(frozen=True)
class _Rule:
    """A reaction-class rule.

    All `reactant_smarts` patterns must match at least one reactant molecule.
    When `product_smarts` is non-empty AND a product is supplied, each pattern
    must additionally match the product. Each entry in the tuples is a single
    SMARTS string (no rule-level OR — express OR by adding another _Rule).
    """

    label: str
    reactant_smarts: tuple[str, ...]
    product_smarts: tuple[str, ...] = ()


# Every label a rule can produce, plus `other`. Exported because the vendored trust-priors file
# names classes, and a typo there would silently give that class no weighting rather than failing —
# `tests/test_dataset.py` checks the priors against this set.
ALL_CLASSES: frozenset[str] = frozenset(
    {
        CLASS_AMIDE_FORMATION,
        CLASS_ESTERIFICATION,
        CLASS_SUZUKI,
        CLASS_REDUCTION,
        CLASS_OXIDATION,
        CLASS_NUCLEOPHILIC_SUBSTITUTION,
        CLASS_NITRATION,
        CLASS_HALOGENATION,
        CLASS_HYDROLYSIS,
        CLASS_OTHER,
    }
)


_RULES: tuple[_Rule, ...] = (
    # Amide formation: acid chloride + amine -> amide
    _Rule(
        label=CLASS_AMIDE_FORMATION,
        reactant_smarts=("[CX3](=O)[Cl,Br,F,I]", "[NX3;H2,H1;!$(NC=O)]"),
        product_smarts=("[NX3][CX3]=O",),
    ),
    # Amide formation: carboxylic acid + amine -> amide
    _Rule(
        label=CLASS_AMIDE_FORMATION,
        reactant_smarts=("[CX3](=O)[OX2H]", "[NX3;H2,H1;!$(NC=O)]"),
        product_smarts=("[NX3][CX3]=O",),
    ),
    # Esterification: carboxylic acid + alcohol -> ester
    _Rule(
        label=CLASS_ESTERIFICATION,
        reactant_smarts=("[CX3](=O)[OX2H]", "[OX2H][CX4]"),
        product_smarts=("[CX3](=O)[OX2][CX4]",),
    ),
    # Suzuki coupling: aryl halide + boronic acid -> biaryl
    _Rule(
        label=CLASS_SUZUKI,
        reactant_smarts=("[c,C][Br,I,Cl]", "[B]([OH])([OH])[c,C]"),
        product_smarts=("[c,C]-[c,C]",),
    ),
    # Carbonyl reduction (NaBH4 / LiAlH4) -> alcohol
    _Rule(
        label=CLASS_REDUCTION,
        reactant_smarts=("[CX3]=[OX1]", "[BH4-,AlH4-]"),
        product_smarts=("[CX4][OX2H]",),
    ),
    # Alcohol oxidation with metal oxidants
    _Rule(
        label=CLASS_OXIDATION,
        reactant_smarts=("[CX4][OX2H]", "[Cr,Mn]"),
        product_smarts=("[CX3]=[OX1]",),
    ),
    # Aromatic nitration
    _Rule(
        label=CLASS_NITRATION,
        reactant_smarts=("c1ccccc1", "O=[N+]([O-])O"),
        product_smarts=("c[N+](=O)[O-]",),
    ),
    # Aromatic halogenation with X2
    _Rule(
        label=CLASS_HALOGENATION,
        reactant_smarts=("c1ccccc1", "[Cl,Br][Cl,Br]"),
        product_smarts=("c[Cl,Br]",),
    ),
    # Hydrolysis of an ester / amide / nitrile
    _Rule(
        label=CLASS_HYDROLYSIS,
        reactant_smarts=(
            "[$([CX3](=O)[OX2][CX4]),$([CX3](=O)[NX3]),$([CX2]#[NX1])]",
            "[OX2H2]",
        ),
    ),
    # Generic nucleophilic substitution on an alkyl halide. This rule has no
    # product gate and matches broadly (any alkyl halide + anion), so it is
    # placed LAST — it only fires as a fallback when no more-specific named
    # reaction above matched, avoiding false positives on spectator halides.
    _Rule(
        label=CLASS_NUCLEOPHILIC_SUBSTITUTION,
        reactant_smarts=("[CX4][Cl,Br,I]", "[N-,O-,S-]"),
    ),
)


def classify_reaction(reactants: str, product: str | None = None) -> str:
    """Return the best-matching reaction class label, or CLASS_OTHER."""
    try:
        from rdkit import Chem  # noqa: F401
    except ImportError:
        return CLASS_OTHER

    reactant_mols = _smiles_to_mols(reactants)
    product_mols = _smiles_to_mols(product) if product else []

    for rule in _RULES:
        if _rule_matches(rule, reactant_mols, product_mols):
            return rule.label

    return CLASS_OTHER


def _smiles_to_mols(s: str | None) -> list[Any]:
    if not s:
        return []
    from rdkit import Chem

    out = []
    # Strip any reaction-SMILES separators
    if ">" in s:
        s = s.split(">")[0]
    for part in s.split("."):
        part = part.strip()
        if not part:
            continue
        mol = Chem.MolFromSmiles(part)
        if mol is not None:
            out.append(mol)
    return out


def _rule_matches(rule: _Rule, reactant_mols: list[Any], product_mols: list[Any]) -> bool:
    for smarts in rule.reactant_smarts:
        if not _any_mol_matches(reactant_mols, smarts):
            return False
    if rule.product_smarts and product_mols:
        for smarts in rule.product_smarts:
            if not _any_mol_matches(product_mols, smarts):
                return False
    return True


def _any_mol_matches(mols: list[Any], smarts: str) -> bool:
    from rdkit import Chem

    patt = Chem.MolFromSmarts(smarts)
    if patt is None:
        logger.warning("invalid SMARTS in classifier: %r", smarts)
        return False
    return any(m.HasSubstructMatch(patt) for m in mols)
