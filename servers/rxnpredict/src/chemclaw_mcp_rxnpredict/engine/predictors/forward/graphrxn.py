"""GraphRXN forward predictor (placeholder wrapper).

`jidushanbojue/GraphRXN` (Yan et al. 2023), a GNN on 2D reaction structures. Not on PyPI; it slots
into the ensemble once the repo and checkpoints are installed.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from ...preprocessing import canonical_smiles
from ...schemas import ForwardPrediction
from .. import mark_unavailable, register_forward
from ..base import BaseForwardPredictor

logger = logging.getLogger(__name__)


class GraphRxnForward(BaseForwardPredictor):
    name = "graphrxn"
    description = "GraphRXN — graph-based reaction encoder (Yan 2023)."
    citation = "Yan et al., J. Cheminform. 2023, 15:91 (jidushanbojue/GraphRXN)"
    extras_install = "graphrxn"

    def __init__(self) -> None:
        super().__init__()
        self._model: Any = None

    def load(self) -> None:
        from ...config import get_settings

        settings = get_settings()
        ckpt = os.environ.get(
            "GRAPHRXN_MODEL_PATH",
            str(settings.model_dir / "graphrxn" / "model.pt"),
        )
        if not os.path.exists(ckpt):
            raise FileNotFoundError(
                f"GraphRXN checkpoint not found at {ckpt}. "
                "Clone jidushanbojue/GraphRXN and place a trained model.pt at $GRAPHRXN_MODEL_PATH."
            )
        # The repo has no clean Python API (it is usually run as `predict.py`); this does the same
        # in-process.
        from graphrxn.model import GraphRXNPredictor  # type: ignore

        self._model = GraphRXNPredictor.load(ckpt)

    def predict_sync(self, reactants: str, top_k: int) -> list[ForwardPrediction]:
        results = self._model.predict(reactants, top_k=top_k)
        preds: list[ForwardPrediction] = []
        for i, item in enumerate(results[:top_k]):
            smi = item.get("smiles") if isinstance(item, dict) else item[0]
            score = item.get("score") if isinstance(item, dict) else item[1]
            # A malformed row is dropped so it cannot cost the other models' votes.
            if smi is None or score is None:
                continue
            try:
                product = canonical_smiles(str(smi))
            except ValueError:
                continue
            preds.append(
                ForwardPrediction(
                    product_smiles=product,
                    score=min(1.0, max(0.0, float(score))),
                    rank=i + 1,
                    source_model=self.name,
                )
            )
        return preds


try:
    import torch  # noqa: F401

    register_forward(GraphRxnForward())
# BLE001: guard around an optional predictor; `mark_unavailable` classifies `exc`.
except Exception as exc:  # noqa: BLE001
    mark_unavailable(
        GraphRxnForward.name,
        "forward",
        "missing optional deps (install `chemclaw-mcp-rxnpredict[graphrxn]` and the GraphRXN "
        f"repo): {exc!r}",
        exc=exc,
        optional=("torch",),
    )
