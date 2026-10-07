"""Lightweight SMARTS-based reaction classifier.

Gives the meta-model one coarse class label per (reactants, optional product) for per-class trust
priors, or `CLASS_OTHER` when no rule matches. Labels mirror the classes priors are published for;
add one when a new class is calibrated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import cache
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

    Every `reactant_smarts` pattern must match some reactant; when `product_smarts` is non-empty and
    a product is supplied, each must also match the product. Express OR as another rule.
    """

    label: str
    reactant_smarts: tuple[str, ...]
    product_smarts: tuple[str, ...] = ()


# Every label a rule can produce, plus `other`. `tests/test_dataset.py` checks the vendored priors
# against it, since a misspelt class would silently get no weighting.
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
    # Generic SN on an alkyl halide: no product gate and broad, so it is last, a fallback that does
    # not claim spectator halides.
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


@cache
def _compiled(smarts: str) -> Any | None:
    """One SMARTS, compiled once per process. `None` for a pattern RDKit will not parse.

    `classify_reaction` runs per reaction during aggregation, so patterns are not re-parsed per
    call. Cached lazily on the string so an early match does not pay for the rest of the table; the
    key set is the literals in `_RULES`. A bad pattern warns once rather than per call.
    """
    from rdkit import Chem

    pattern = Chem.MolFromSmarts(smarts)
    if pattern is None:
        logger.warning("invalid SMARTS in classifier: %r", smarts)
    return pattern


def _any_mol_matches(mols: list[Any], smarts: str) -> bool:
    pattern = _compiled(smarts)
    if pattern is None:
        return False
    return any(m.HasSubstructMatch(pattern) for m in mols)
