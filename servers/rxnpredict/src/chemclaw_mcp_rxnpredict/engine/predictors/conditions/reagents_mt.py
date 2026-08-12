"""Reagents-via-Molecular-Transformer condition predictor.

Andronov et al. — `Academich/reagents`. Fine-tuned Molecular Transformer that
emits reagents/conditions given reactants + product. Same OpenNMT-py stack
as the forward Molecular Transformer; share the dependency.
"""

from __future__ import annotations

import logging
import math
import os
from typing import Any

from ...schemas import ConditionsPrediction
from .. import mark_unavailable, register_conditions
from ..base import BaseConditionsPredictor
from ..forward.molecular_transformer import _tokenize_smiles

logger = logging.getLogger(__name__)


class ReagentsMTConditions(BaseConditionsPredictor):
    name = "reagents_mt"
    description = "Reagent prediction via Molecular Transformer (Andronov, Academich/reagents)."
    citation = "Andronov et al., reagents repo (Academich/reagents)"
    extras_install = "reagents_mt"

    def __init__(self) -> None:
        super().__init__()
        self._translator: Any = None

    def load(self) -> None:
        from ...config import get_settings

        settings = get_settings()
        model_path = os.environ.get(
            "REAGENTS_MT_MODEL_PATH",
            str(settings.model_dir / "reagents_mt" / "model.pt"),
        )
        if not os.path.exists(model_path):
            raise FileNotFoundError(
                f"Reagents-MT checkpoint not found at {model_path}. "
                "Download from the Academich/reagents repo release page."
            )
        from onmt.opts import translate_opts
        from onmt.translate.translator import build_translator
        from onmt.utils.parse import ArgumentParser

        parser = ArgumentParser()
        translate_opts(parser)
        opt = parser.parse_args(
            [
                "-model",
                model_path,
                "-src",
                "/dev/stdin",
                "-output",
                "/dev/null",
                "-batch_size",
                "1",
                "-max_length",
                "256",
                "-gpu",
                "0" if settings.resolve_device().startswith("cuda") else "-1",
            ]
        )
        ArgumentParser.validate_translate_opts(opt)
        self._translator = build_translator(opt, report_score=False)

    def predict_sync(self, reactants: str, product: str, top_k: int) -> list[ConditionsPrediction]:
        # The reagents model is trained on "REACTANTS>>PRODUCTS" -> "REAGENTS" format
        src = f"{_tokenize_smiles(reactants)} >> {_tokenize_smiles(product)}"
        scores_lists, predictions_lists = self._translator.translate(
            src=[src.encode("utf-8")], batch_size=1, n_best=top_k
        )
        preds: list[ConditionsPrediction] = []
        for i, (raw, log_score) in enumerate(
            zip(predictions_lists[0], scores_lists[0], strict=False)
        ):
            tokens = raw.replace(" ", "").split(".")
            reagents = [t for t in tokens if t]
            score = (
                float(min(1.0, math.exp(float(log_score) / max(1, len(raw.split())))))
                if log_score is not None
                else max(0.01, 1.0 - 0.1 * i)
            )
            preds.append(
                ConditionsPrediction(
                    catalysts=[],
                    solvents=[],
                    reagents=reagents,
                    temperature_c=None,
                    score=score,
                    rank=i + 1,
                    source_model=self.name,
                )
            )
        return preds


try:
    import onmt  # noqa: F401

    register_conditions(ReagentsMTConditions())
except Exception as exc:
    mark_unavailable(
        ReagentsMTConditions.name,
        "conditions",
        f"missing optional deps (install `chemclaw-mcp-rxnpredict[reagents_mt]` and download "
        f"checkpoint to $REAGENTS_MT_MODEL_PATH): {exc!r}",
    )
