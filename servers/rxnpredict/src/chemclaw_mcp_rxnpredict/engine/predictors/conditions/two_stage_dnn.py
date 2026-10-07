"""Two-stage DNN condition predictor (Chen & Li, J. Cheminform. 2024, 16:11).

Stage 1 is a multi-label classifier suggesting reagents and solvents; stage 2 ranks candidate
combinations. Assumes a `two_stage_dnn` package on PYTHONPATH exposing
`TwoStageConditionPredictor.predict(rxn_smiles, top_n)`.
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


class TwoStageDNNConditions(BaseConditionsPredictor):
    name = "two_stage_dnn"
    description = "Two-stage DNN feasible reaction condition predictor (Chen & Li 2024)."
    citation = "Chen & Li, J. Cheminform. 2024, 16:11"
    extras_install = "two_stage_dnn"

    def __init__(self) -> None:
        super().__init__()
        self._predictor: Any = None

    def load(self) -> None:
        from ...config import get_settings

        settings = get_settings()
        ckpt_dir = os.environ.get(
            "TWO_STAGE_DNN_MODEL_PATH",
            str(settings.model_dir / "two_stage_dnn"),
        )
        if not os.path.exists(ckpt_dir):
            raise FileNotFoundError(
                f"two_stage_dnn checkpoints not found at {ckpt_dir}. "
                "See Chen & Li (2024) supplementary materials for weights."
            )
        from two_stage_dnn.inference import TwoStageConditionPredictor

        self._predictor = TwoStageConditionPredictor.load(
            ckpt_dir, device=settings.resolve_device()
        )

    def predict_sync(self, reactants: str, product: str, top_k: int) -> list[ConditionsPrediction]:
        rxn = build_reaction_smiles(reactants=reactants, product=product)
        ranked = self._predictor.predict(rxn, top_n=top_k)
        preds: list[ConditionsPrediction] = []
        for i, item in enumerate(ranked[:top_k]):
            preds.append(
                ConditionsPrediction(
                    catalysts=list(item.get("catalysts", [])),
                    solvents=list(item.get("solvents", [])),
                    reagents=list(item.get("reagents", [])),
                    temperature_c=item.get("temperature"),
                    score=min(1.0, max(0.0, float(item.get("score", 0.5)))),
                    rank=i + 1,
                    source_model=self.name,
                )
            )
        return preds


try:
    import torch  # noqa: F401

    register_conditions(TwoStageDNNConditions())
# BLE001: guard around an optional predictor; `mark_unavailable` classifies `exc`.
except Exception as exc:  # noqa: BLE001
    mark_unavailable(
        TwoStageDNNConditions.name,
        "conditions",
        f"missing optional deps (install `chemclaw-mcp-rxnpredict[two_stage_dnn]` and place "
        f"checkpoints at $TWO_STAGE_DNN_MODEL_PATH): {exc!r}",
        exc=exc,
        optional=("torch",),
    )
