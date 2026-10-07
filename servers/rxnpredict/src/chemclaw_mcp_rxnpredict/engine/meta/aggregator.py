"""Meta-model aggregators for forward and conditions predictions.

Borda-style weighted rank voting with optional gating by reaction class:

  - Each candidate (canonical product SMILES or canonical condition tuple) sums, over every model
    that ranked it, `effective_prior(model, class) * model_score * 1/rank`.
  - `effective_prior` prefers per-class priors when a class is assigned, else global priors.
  - Candidates sort by total weight, ties broken by vote count.
  - `consensus_score` is the candidate's share of the attainable weight (every voter ranking it
    first at full confidence, `sum(effective_prior(m))`), so a unanimous vote and a lone
    low-confidence guess do not both score 1.0.

Needs no training data and degrades gracefully when models are missing.
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

# The unit a condition vote is cast on: catalysts, solvents, reagents and a bucketed temperature.
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

    # `class_priors()`, not the field: it lays the env override over the lazily read corpus.
    per_class = settings.class_priors()

    weights: dict[str, float] = defaultdict(float)
    voters: dict[str, set[str]] = defaultdict(set)
    # Summed over models that answered, so a crashed predictor costs its vote, not the scale.
    attainable = 0.0

    for model_name, preds in per_model.items():
        prior = effective_prior(
            model_name,
            reaction_class,
            settings.model_trust_priors,
            per_class,
        )
        attainable += prior
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

    attainable = attainable or 1.0
    aggregated: list[AggregatedForwardPrediction] = []
    for rank, (smiles, weight) in enumerate(sorted_candidates[:top_k], start=1):
        aggregated.append(
            AggregatedForwardPrediction(
                product_smiles=smiles,
                consensus_score=min(1.0, weight / attainable),
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

    Floor avoids round-half-to-even surprises and buckets negatives correctly (-15 → -20).
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

    The whole (catalysts, solvents, reagents, temperature bucket) tuple is the voting unit, so the
    same recipe is reinforced; 10 °C bins keep trivial temperature differences from splitting
    agreement.
    """
    reaction_class: str | None = None
    if reactants and settings.use_class_priors:
        reaction_class = classify_reaction(reactants, product=product)
        if reaction_class == CLASS_OTHER:
            reaction_class = None

    # `class_priors()`, not the field: it lays the env override over the lazily read corpus.
    per_class = settings.class_priors()

    weights: dict[ConditionKey, float] = defaultdict(float)
    voters: dict[ConditionKey, set[str]] = defaultdict(set)
    temps_for_key: dict[ConditionKey, list[float]] = defaultdict(list)
    # The `consensus_score` denominator is the attainable weight; see `aggregate_forward`.
    attainable = 0.0

    for model_name, preds in per_model.items():
        prior = effective_prior(
            model_name,
            reaction_class,
            settings.model_trust_priors,
            per_class,
        )
        attainable += prior
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

    attainable = attainable or 1.0
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
                consensus_score=min(1.0, weight / attainable),
                rank=rank,
                vote_count=len(voters[key]),
                contributing_models=sorted(voters[key]),
            )
        )
    return aggregated
