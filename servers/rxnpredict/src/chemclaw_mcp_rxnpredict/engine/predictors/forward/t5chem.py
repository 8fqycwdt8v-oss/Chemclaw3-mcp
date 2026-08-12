"""T5Chem forward predictor.

Lu & Zhang 2022 — `HelloJocelynLu/t5chem`. HuggingFace-T5 based multi-task model
for reaction prediction; the `product` task = forward reaction prediction.

Requires the `t5chem` Python package and a downloaded checkpoint (per the repo's
README: download a tar.gz from the project page and pass the directory via
T5CHEM_MODEL_PATH or the `model_path` setting).
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


class T5ChemForward(BaseForwardPredictor):
    name = "t5chem"
    description = "T5Chem multi-task transformer, forward prediction head (Lu 2022)."
    citation = "Lu & Zhang, J. Chem. Inf. Model. 2022, 62, 1376 (HelloJocelynLu/t5chem)"
    extras_install = "t5chem"

    def __init__(self) -> None:
        super().__init__()
        self._tokenizer: Any = None
        self._model: Any = None
        self._device: str = "cpu"

    def load(self) -> None:
        import torch

        from ...config import get_settings

        settings = get_settings()
        self._device = settings.resolve_device()
        model_path = os.environ.get(
            "T5CHEM_MODEL_PATH",
            str(settings.model_dir / "t5chem" / "USPTO_500_MT" / "product"),
        )
        # t5chem ships its own tokenizer + model wrapper
        from t5chem import SimpleTokenizer, T5ForProperty

        self._tokenizer = SimpleTokenizer(vocab_file=os.path.join(model_path, "vocab.pt"))
        from transformers import T5ForConditionalGeneration

        self._model = T5ForConditionalGeneration.from_pretrained(model_path).to(self._device)
        self._model.eval()
        # Silence unused-import warnings
        _ = (torch, T5ForProperty)

    def predict_sync(self, reactants: str, top_k: int) -> list[ForwardPrediction]:
        import torch

        # T5Chem prepends task tokens; for forward prediction the prefix is "Product:"
        prompt = f"Product:{reactants}"
        ids = torch.tensor([self._tokenizer.encode(prompt)], device=self._device)
        with torch.no_grad():
            out = self._model.generate(
                ids,
                num_beams=max(top_k, 5),
                num_return_sequences=top_k,
                max_length=300,
                output_scores=True,
                return_dict_in_generate=True,
            )
        raw_scores = getattr(out, "sequences_scores", None)
        preds: list[ForwardPrediction] = []
        for i, seq in enumerate(out.sequences):
            decoded = self._tokenizer.decode(seq.tolist()).strip()
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
    import t5chem  # noqa: F401

    register_forward(T5ChemForward())
except Exception as exc:
    mark_unavailable(
        T5ChemForward.name,
        "forward",
        f"missing optional deps (install `chemclaw-mcp-rxnpredict[t5chem]` and download weights "
        f"to $T5CHEM_MODEL_PATH): {exc!r}",
    )
