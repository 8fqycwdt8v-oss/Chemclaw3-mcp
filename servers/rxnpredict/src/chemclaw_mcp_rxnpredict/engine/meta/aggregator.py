"""Meta-model aggregators for forward and conditions predictions.

Strategy: Borda-style weighted rank voting with optional Mixture-of-Experts
gating by reaction class.

  - For each candidate (canonical product SMILES or canonical condition tuple),
    sum a contribution from every model that ranked it:
        weight = effective_prior(model, class) * model_score * 1/rank
  - `effective_prior` looks up per-class trust priors first (when a class can
    be assigned), falling back to global priors.
  - Sort candidates by total weight, descending.
  - Ties broken by higher vote count.
  - Returned consensus_score is renormalised so the top candidate scores ~1.0.

This requires no training data and degrades gracefully when models are missing.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from collections.abc import Iterable

from ..config import Settings
from ..preprocessing import canonical_smiles
from ..schemas import (
    AggregatedConditionsPrediction,
    AggregatedForwardPrediction,
    ConditionsPrediction,
    ForwardPrediction,
)
from .classifier import CLASS_OTHER, classify_reaction
from .trust_priors import effective_prior

# The unit a condition vote is cast on: catalysts, solvents, reagents and a bucketed temperature,
# flattened to something hashable. Named because it appears in three parallel dicts below, and an
# anonymous `tuple` there says nothing about what the aggregator is counting.
ConditionKey = tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], int | None]

logger = logging.getLogger(__name__)


def _normalise_product(smiles: str) -> str:
    try:
        return canonical_smiles(smiles)
    except ValueError:
        return smiles  # leave malformed strings as-is; they'll get bottom rank by score


def aggregate_forward(
    per_model: dict[str, list[ForwardPrediction]],
    settings: Settings,
    top_k: int,
    *,
    reactants: str | None = None,
) -> list[AggregatedForwardPrediction]:
    """Borda-weighted voting across forward predictors.

    `per_model[model_name]` is that model's top-K predictions (rank 1 = best).
    `reactants` is optional; when provided, the reaction is classified and
    per-class trust priors are used (MoE gating).
    """
    reaction_class: str | None = None
    if reactants and settings.use_class_priors:
        reaction_class = classify_reaction(reactants)
        if reaction_class == CLASS_OTHER:
            reaction_class = None

    per_class = settings.model_trust_priors_by_class

    weights: dict[str, float] = defaultdict(float)
    voters: dict[str, set[str]] = defaultdict(set)

    for model_name, preds in per_model.items():
        prior = effective_prior(
            model_name,
            reaction_class,
            settings.model_trust_priors,
            per_class,
        )
        for p in preds:
            canon = _normalise_product(p.product_smiles)
            contribution = prior * p.score / p.rank
            weights[canon] += contribution
            voters[canon].add(model_name)

    if not weights:
        return []

    sorted_candidates = sorted(
        weights.items(),
        key=lambda kv: (-kv[1], -len(voters[kv[0]])),
    )

    max_weight = sorted_candidates[0][1] or 1.0
    aggregated: list[AggregatedForwardPrediction] = []
    for rank, (smiles, weight) in enumerate(sorted_candidates[:top_k], start=1):
        aggregated.append(
            AggregatedForwardPrediction(
                product_smiles=smiles,
                consensus_score=min(1.0, weight / max_weight),
                rank=rank,
                vote_count=len(voters[smiles]),
                contributing_models=sorted(voters[smiles]),
            )
        )
    return aggregated


def _canon_set(items: Iterable[str]) -> tuple[str, ...]:
    """Canonicalise & sort a set of SMILES strings into a hashable tuple."""
    out: list[str] = []
    for x in items:
        x = x.strip()
        if not x:
            continue
        try:
            out.append(canonical_smiles(x))
        except ValueError:
            out.append(x)
    return tuple(sorted(set(out)))


def _temperature_bucket(t: float | None) -> int | None:
    """Bucket temperature into 10 °C bins using floor division.

    floor() avoids round-half-to-even surprises near bin boundaries (where
    e.g. 25 and 28 would otherwise land in different bins). Negative
    temperatures stay correctly bucketed (-15 → -20, not -10).
    """
    if t is None:
        return None
    return math.floor(t / 10.0) * 10


def aggregate_conditions(
    per_model: dict[str, list[ConditionsPrediction]],
    settings: Settings,
    top_k: int,
    *,
    reactants: str | None = None,
    product: str | None = None,
) -> list[AggregatedConditionsPrediction]:
    """Borda-weighted voting across condition predictors.

    Conditions are higher-dimensional than products. We treat the whole
    (catalysts, solvents, reagents, temperature_bucket) tuple as the voting unit
    so that "the same recipe" gets reinforced, and bucket temperature into 10 °C
    bins to avoid trivial mismatches drowning out agreement.
    """
    reaction_class: str | None = None
    if reactants and settings.use_class_priors:
        reaction_class = classify_reaction(reactants, product=product)
        if reaction_class == CLASS_OTHER:
            reaction_class = None

    per_class = settings.model_trust_priors_by_class

    weights: dict[ConditionKey, float] = defaultdict(float)
    voters: dict[ConditionKey, set[str]] = defaultdict(set)
    temps_for_key: dict[ConditionKey, list[float]] = defaultdict(list)

    for model_name, preds in per_model.items():
        prior = effective_prior(
            model_name,
            reaction_class,
            settings.model_trust_priors,
            per_class,
        )
        for p in preds:
            cats = _canon_set(p.catalysts)
            sols = _canon_set(p.solvents)
            rgs = _canon_set(p.reagents)
            tbucket = _temperature_bucket(p.temperature_c)
            key = (cats, sols, rgs, tbucket)

            contribution = prior * p.score / p.rank
            weights[key] += contribution
            voters[key].add(model_name)
            if p.temperature_c is not None:
                temps_for_key[key].append(p.temperature_c)

    if not weights:
        return []

    sorted_candidates = sorted(
        weights.items(),
        key=lambda kv: (-kv[1], -len(voters[kv[0]])),
    )

    max_weight = sorted_candidates[0][1] or 1.0
    aggregated: list[AggregatedConditionsPrediction] = []
    for rank, (key, weight) in enumerate(sorted_candidates[:top_k], start=1):
        cats, sols, rgs, _tbucket = key
        temps = temps_for_key[key]
        mean_temp = sum(temps) / len(temps) if temps else None
        aggregated.append(
            AggregatedConditionsPrediction(
                catalysts=list(cats),
                solvents=list(sols),
                reagents=list(rgs),
                temperature_c=mean_temp,
                consensus_score=min(1.0, weight / max_weight),
                rank=rank,
                vote_count=len(voters[key]),
                contributing_models=sorted(voters[key]),
            )
        )
    return aggregated
