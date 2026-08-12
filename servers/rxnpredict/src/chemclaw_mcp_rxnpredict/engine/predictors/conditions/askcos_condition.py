"""ASKCOS reaction condition recommender (Gao, Struble, Coley 2018).

The original neural-network condition recommender from MIT, trained on ~10M
Reaxys reactions. It lives inside the ASKCOS suite
(MIT ASKCOS, open-source release). 69.6% top-10 close-match on
catalyst/solvent/reagent.

The recommender ships as part of `askcos-core`; this wrapper imports the
specific module if it's on PYTHONPATH and otherwise marks itself unavailable.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from ...preprocessing import build_reaction_smiles
from ...schemas import ConditionsPrediction
from .. import mark_unavailable, register_conditions
from ..base import BaseConditionsPredictor

logger = logging.getLogger(__name__)


class ASKCOSConditionRecommender(BaseConditionsPredictor):
    name = "askcos_condition"
    description = "ASKCOS NN reaction condition recommender (Gao 2018, ~10M Reaxys)."
    citation = "Gao, Struble, Coley et al., ACS Cent. Sci. 2018, 4, 1465 (askcos.mit.edu)"
    extras_install = None

    def __init__(self) -> None:
        super().__init__()
        self._recommender: Any = None

    def load(self) -> None:
        # ASKCOS is a large suite; users install askcos-core separately.
        from askcos.synthetic.context.neuralnetwork import (  # type: ignore
            NeuralNetContextRecommender,
        )

        recommender = NeuralNetContextRecommender()
        weights = os.environ.get("ASKCOS_CONTEXT_MODEL_PATH")
        recommender.load_nn_model(model_path=weights) if weights else recommender.load_nn_model()
        self._recommender = recommender

    def predict_sync(self, reactants: str, product: str, top_k: int) -> list[ConditionsPrediction]:
        rxn = build_reaction_smiles(reactants=reactants, product=product)
        recs = self._recommender.get_n_conditions(rxn, n=top_k, return_separate=True)
        # `recs` is typically a list of tuples (T, solvent, reagent, catalyst, score)
        preds: list[ConditionsPrediction] = []
        for i, rec in enumerate(recs[:top_k]):
            if len(rec) >= 5:
                temperature, solvent, reagent, catalyst, score = rec[:5]
            else:
                temperature, solvent, reagent, catalyst = rec[:4]
                score = max(0.01, 1.0 - 0.1 * i)
            preds.append(
                ConditionsPrediction(
                    catalysts=_as_list(catalyst),
                    solvents=_as_list(solvent),
                    reagents=_as_list(reagent),
                    temperature_c=_as_float(temperature),
                    score=min(1.0, max(0.0, float(score))),
                    rank=i + 1,
                    source_model=self.name,
                )
            )
        return preds


def _as_list(v: Any) -> list[str]:
    if v is None or v == "":
        return []
    if isinstance(v, str):
        # ASKCOS sometimes returns dot-separated SMILES strings
        return [p for p in v.split(".") if p]
    return [str(x) for x in v]


def _as_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


try:
    import askcos  # type: ignore  # noqa: F401

    register_conditions(ASKCOSConditionRecommender())
except Exception as exc:
    mark_unavailable(
        ASKCOSConditionRecommender.name,
        "conditions",
        f"askcos suite not importable (install askcos-core from MIT's repo): {exc!r}",
    )
