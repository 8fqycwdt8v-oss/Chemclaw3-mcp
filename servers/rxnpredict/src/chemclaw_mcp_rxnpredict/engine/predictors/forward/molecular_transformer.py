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
import re
from typing import Any

from ...preprocessing import canonical_smiles, truncate_echo
from ...schemas import ForwardPrediction
from .. import mark_unavailable, register_forward
from ..base import BaseForwardPredictor

logger = logging.getLogger(__name__)


# The tokenizer's alphabet, at module scope so the refusal below and the test that drives it read
# the same pattern. A second transcription of it is a second claim about what this model accepts.
#
# **`\\` and not `\\\\`, and the difference was half of E/Z stereochemistry.** Upstream
# MolecularTransformer writes this pattern in a *non-raw* string, where `\\\\` is the two
# characters that a regex reads as one literal backslash. Transcribed into a raw string here, the
# same four characters are two literal backslashes — a regex for **two** backslashes in a row,
# which no SMILES contains. So the alternation covered `/` and not `\`, and RDKit emits both:
# measured, `C/C=C/C` round-tripped and `C/C=C\C` did not, and the refusal below therefore turned
# away every cis-configured stereodefined alkene as "a structure this tokenizer cannot represent".
# Downstream that is not even an error a chemist sees — `tools._survivors` drops a raising
# predictor and refuses only when *every* one failed — so the answer was a consensus over fewer
# models, silently, for half of the stereochemistry this server exists to predict on.
TOKEN_PATTERN = re.compile(
    r"(\[[^\]]+]|Br?|Cl?|N|O|S|P|F|I|b|c|n|o|s|p|"
    r"\(|\)|\.|=|#|-|\+|\\|\/|:|~|@|\?|>|\*|\$|\%[0-9]{2}|[0-9])"
)


def _tokenize_smiles(smiles: str) -> str:
    """Atom-wise SMILES tokenization expected by MolecularTransformer.

    The round-trip check is the whole safety of this function and it is a **refusal**, not an
    `assert`. A pattern that does not cover some character silently *drops* it — `Se` outside
    brackets matches `S` and loses the `e` — so the model would be handed a different molecule from
    the one the chemist asked about and would answer confidently about it. An `assert` enforcing
    that on caller-derived data is removed by `python -O`, which is an interpreter flag no file in
    this repository sets and every deployment can: a control whose existence depends on how the
    process was started is not a control.

    `ValueError` because that is this repository's family for a deliberately worded, caller-safe
    message, and because it is what `tools._survivors` is already written against. **It is not
    because the model reads it**, which is what this paragraph claimed and what the one caller
    refutes: `predict_sync` runs inside `asyncio.gather(..., return_exceptions=True)`, and
    `_survivors` logs the `repr` and appends only `f"{name} ({type(result).__name__})"` to what the
    caller is told — deliberately, so a predictor's own text cannot carry a checkpoint path into a
    context window. So this message reaches an operator's log and never the model, and the echo is
    truncated to bound a **log line** rather than the context. Both are still worth having; the
    reason was wrong, not the change.
    """
    tokens = TOKEN_PATTERN.findall(smiles)
    if "".join(tokens) != smiles.replace(" ", ""):
        raise ValueError(
            "the Molecular Transformer tokenizer does not cover every character of this "
            "structure, so tokenising it would silently drop part of the molecule and the "
            f"prediction would be about a different one: {truncate_echo(smiles)!r}"
        )
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
