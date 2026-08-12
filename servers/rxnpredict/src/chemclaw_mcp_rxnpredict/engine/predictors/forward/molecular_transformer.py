"""Molecular Transformer forward predictor (Schwaller 2019).

`pschwllr/MolecularTransformer` — OpenNMT-py seq2seq model, ~90% top-1 on
USPTO-MIT and notably uncertainty-calibrated. The model is invoked through
OpenNMT's `translate` pipeline.

Because OpenNMT-py pins legacy torch versions, this predictor is best deployed
as a subprocess worker on its own venv (see scripts/molecular_transformer_worker.py
for an isolated invocation pattern). When co-installed, the in-process loader
below works too.
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


def _tokenize_smiles(smiles: str) -> str:
    """Atom-wise SMILES tokenization expected by MolecularTransformer."""
    import re

    pattern = (
        r"(\[[^\]]+]|Br?|Cl?|N|O|S|P|F|I|b|c|n|o|s|p|"
        r"\(|\)|\.|=|#|-|\+|\\\\|\/|:|~|@|\?|>|\*|\$|\%[0-9]{2}|[0-9])"
    )
    tokens = re.findall(pattern, smiles)
    assert "".join(tokens) == smiles.replace(" ", ""), f"tokenizer drift: {smiles!r}"
    return " ".join(tokens)


class MolecularTransformerForward(BaseForwardPredictor):
    name = "molecular_transformer"
    description = "Molecular Transformer (Schwaller 2019), USPTO-MIT trained, OpenNMT-py."
    citation = "Schwaller et al., ACS Cent. Sci. 2019, 5, 1572 (pschwllr/MolecularTransformer)"
    extras_install = "molecular_transformer"

    def __init__(self) -> None:
        super().__init__()
        self._translator: Any = None
        self._model_path: str | None = None

    def load(self) -> None:
        from ...config import get_settings

        settings = get_settings()
        self._model_path = os.environ.get(
            "MOLECULAR_TRANSFORMER_MODEL_PATH",
            str(settings.model_dir / "molecular_transformer" / "MIT_mixed_augm_model_average.pt"),
        )
        if not os.path.exists(self._model_path):
            raise FileNotFoundError(
                f"Molecular Transformer checkpoint not found at {self._model_path}. "
                "See the server README for where this checkpoint comes from."
            )

        # OpenNMT-py translation pipeline
        from onmt.translate.translator import build_translator
        from onmt.utils.parse import ArgumentParser

        parser = ArgumentParser()
        from onmt.opts import translate_opts

        translate_opts(parser)
        opt = parser.parse_args(
            [
                "-model",
                self._model_path,
                "-src",
                "/dev/stdin",
                "-output",
                "/dev/null",
                "-batch_size",
                "1",
                "-replace_unk",
                "-max_length",
                "256",
                "-gpu",
                "0" if settings.resolve_device().startswith("cuda") else "-1",
            ]
        )
        ArgumentParser.validate_translate_opts(opt)
        self._translator = build_translator(opt, report_score=False)

    def predict_sync(self, reactants: str, top_k: int) -> list[ForwardPrediction]:
        tokenized = _tokenize_smiles(reactants)
        scores_lists, predictions_lists = self._translator.translate(
            src=[tokenized.encode("utf-8")],
            batch_size=1,
            n_best=top_k,
        )
        scores = scores_lists[0]
        predictions = predictions_lists[0]

        preds: list[ForwardPrediction] = []
        for i, (raw, log_score) in enumerate(zip(predictions, scores, strict=False)):
            untok = raw.replace(" ", "")
            try:
                product = canonical_smiles(untok)
            except ValueError:
                continue
            # OpenNMT returns total log-likelihood; softmax-normalise across the n-best list.
            score = float(min(1.0, math.exp(float(log_score) / max(1, len(untok)))))
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
    import onmt  # noqa: F401

    register_forward(MolecularTransformerForward())
except Exception as exc:
    mark_unavailable(
        MolecularTransformerForward.name,
        "forward",
        f"missing optional deps (install `chemclaw-mcp-rxnpredict[molecular_transformer]` "
        "and download MIT_mixed_augm_model_average.pt to "
        f"$MOLECULAR_TRANSFORMER_MODEL_PATH): {exc!r}",
    )
