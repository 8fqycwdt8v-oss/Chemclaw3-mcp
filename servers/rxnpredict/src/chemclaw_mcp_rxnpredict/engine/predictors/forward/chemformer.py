"""Chemformer / MolBART forward predictor.

`MolecularAI/MolBART` — BART-style transformer pretrained on PubChem SMILES,
fine-tunable for forward reaction prediction. Like GraphRXN/MEGAN the
upstream repo is not a clean Python package; users must install it manually
and provide a fine-tuned checkpoint.
"""

from __future__ import annotations

import logging
import math
import os
from typing import Any

from ...preprocessing import canonical_smiles
from ...schemas import ForwardPrediction
from .. import mark_unavailable, register_forward
from ..base import BaseForwardPredictor

logger = logging.getLogger(__name__)


class ChemformerForward(BaseForwardPredictor):
    name = "chemformer"
    description = "Chemformer / MolBART forward prediction (Irwin 2022)."
    citation = "Irwin et al., Mach. Learn. Sci. Technol. 2022, 3 (MolecularAI/MolBART)"
    extras_install = None  # uses standard transformers from `reaction_t5` extra

    def __init__(self) -> None:
        super().__init__()
        self._model: Any = None
        self._tokenizer: Any = None
        self._device = "cpu"

    def load(self) -> None:
        import torch

        from ...config import get_settings

        settings = get_settings()
        self._device = settings.resolve_device()
        ckpt_dir = os.environ.get(
            "CHEMFORMER_MODEL_PATH",
            str(settings.model_dir / "chemformer" / "forward"),
        )
        if not os.path.exists(ckpt_dir):
            raise FileNotFoundError(
                f"Chemformer checkpoint dir not found at {ckpt_dir}. "
                "Fine-tune MolecularAI/MolBART on USPTO-MIT or download a community checkpoint."
            )
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

        self._tokenizer = AutoTokenizer.from_pretrained(ckpt_dir)
        self._model = AutoModelForSeq2SeqLM.from_pretrained(ckpt_dir).to(self._device)
        self._model.eval()
        _ = torch.zeros(1, device=self._device)

    def predict_sync(self, reactants: str, top_k: int) -> list[ForwardPrediction]:
        import torch

        inputs = self._tokenizer(reactants, return_tensors="pt", truncation=True).to(self._device)
        with torch.no_grad():
            out = self._model.generate(
                **inputs,
                num_beams=max(top_k, 5),
                num_return_sequences=top_k,
                max_new_tokens=256,
                output_scores=True,
                return_dict_in_generate=True,
            )
        raw_scores = getattr(out, "sequences_scores", None)
        preds: list[ForwardPrediction] = []
        for i, seq in enumerate(out.sequences):
            decoded = self._tokenizer.decode(seq, skip_special_tokens=True).strip()
            try:
                product = canonical_smiles(decoded)
            except ValueError:
                continue
            score = (
                float(min(1.0, math.exp(float(raw_scores[i]))))
                if raw_scores is not None
                else max(0.01, 1.0 - 0.1 * i)
            )
            preds.append(
                ForwardPrediction(
                    product_smiles=product,
                    score=score,
                    rank=i + 1,
                    source_model=self.name,
                )
            )
        return preds


try:
    import transformers  # noqa: F401

    register_forward(ChemformerForward())
except Exception as exc:
    mark_unavailable(
        ChemformerForward.name,
        "forward",
        f"missing optional deps (install `chemclaw-mcp-rxnpredict[reaction_t5]` and provide "
        f"a Chemformer checkpoint at $CHEMFORMER_MODEL_PATH): {exc!r}",
    )
