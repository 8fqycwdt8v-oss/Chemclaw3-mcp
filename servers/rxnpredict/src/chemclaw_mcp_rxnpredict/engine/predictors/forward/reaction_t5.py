"""ReactionT5 v2 forward predictor.

Sagawa et al. 2024 — HuggingFace `sagawa/ReactionT5v2-forward`.
Top-1 ≈ 97.5% on USPTO-MIT (the strongest published open transformer baseline).

The model takes a reaction SMILES with empty product half: `REACTANTS>AGENTS>`,
and emits the predicted product as the decoded sequence.
"""

from __future__ import annotations

import logging
import math
import re
from typing import Any

from ...preprocessing import canonical_smiles, parse_reaction
from ...schemas import ForwardPrediction
from .. import mark_unavailable, register_forward
from ..base import BaseForwardPredictor

logger = logging.getLogger(__name__)

_MODEL_ID = "sagawa/ReactionT5v2-forward"


class ReactionT5V2Forward(BaseForwardPredictor):
    name = "reaction_t5_v2"
    description = "ReactionT5 v2 — pretrained T5 for forward reaction prediction (Sagawa 2024)."
    citation = "Sagawa & Kojima, ReactionT5 v2 (2024), HuggingFace: sagawa/ReactionT5v2-forward"
    extras_install = "reaction_t5"

    def __init__(self) -> None:
        super().__init__()
        self._tokenizer: Any = None
        self._model: Any = None
        self._device: str = "cpu"

    def load(self) -> None:
        import torch
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

        from ...config import get_settings

        settings = get_settings()
        self._device = settings.resolve_device()
        logger.info("Loading %s on %s", _MODEL_ID, self._device)
        self._tokenizer = AutoTokenizer.from_pretrained(_MODEL_ID)
        self._model = AutoModelForSeq2SeqLM.from_pretrained(_MODEL_ID).to(self._device)
        self._model.eval()
        # Eager attribute so torch is referenced (silences lints in some envs)
        _ = torch.zeros(1, device=self._device)

    def predict_sync(self, reactants: str, top_k: int) -> list[ForwardPrediction]:
        import torch

        # ReactionT5 expects "REACTANT:<smiles>REAGENT:<smiles>" prompt form per the model card.
        rxts, agents, _ = parse_reaction(reactants)
        prompt = f"REACTANT:{rxts}REAGENT:{agents}"

        inputs = self._tokenizer(
            prompt,
            return_tensors="pt",
            truncation=True,
            max_length=400,
        ).to(self._device)

        with torch.no_grad():
            out = self._model.generate(
                **inputs,
                num_beams=max(top_k, 5),
                num_return_sequences=top_k,
                max_new_tokens=256,
                output_scores=True,
                return_dict_in_generate=True,
            )
        sequences = out.sequences
        # `sequences_scores` is log-prob per sequence on most HF generate paths
        raw_scores = getattr(out, "sequences_scores", None)

        preds: list[ForwardPrediction] = []
        for i, seq in enumerate(sequences):
            decoded = self._tokenizer.decode(seq, skip_special_tokens=True).strip()
            cleaned = _clean_product_string(decoded)
            if not cleaned:
                continue  # skip blank decodes — an empty SMILES is not a real product
            try:
                product = canonical_smiles(cleaned)
            except ValueError:
                continue

            if raw_scores is not None:
                # Sequence log-probs are negative; squash via exp into [0, 1]
                score = float(min(1.0, math.exp(float(raw_scores[i]))))
            else:
                score = max(0.01, 1.0 - 0.1 * i)

            preds.append(
                ForwardPrediction(
                    product_smiles=product,
                    score=score,
                    rank=i + 1,
                    source_model=self.name,
                )
            )
        return preds


_PRODUCT_RE = re.compile(r"PRODUCT[:\s]*", re.IGNORECASE)


def _clean_product_string(s: str) -> str:
    """ReactionT5 may emit `PRODUCT:<smi>` — strip that prefix if present."""
    return _PRODUCT_RE.sub("", s).strip()


try:
    import transformers  # noqa: F401

    register_forward(ReactionT5V2Forward())
except Exception as exc:
    mark_unavailable(
        ReactionT5V2Forward.name,
        "forward",
        f"missing optional deps (install `chemclaw-mcp-rxnpredict[reaction_t5]`): {exc!r}",
    )
