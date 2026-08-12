"""Parrot reaction condition predictor.

Wang et al. 2023 — `wangxr0526/Parrot`. Transformer trained on Pistachio and
USPTO condition datasets; predicts catalysts, solvents, reagents, and
temperatures. +13.44% top-3 over the Coley 2018 baseline.

The upstream repo is a research codebase, not a pip package — users clone it
and install via the bundled `setup.py`/conda env. The wrapper below uses the
public `parrot.inference` API once it's importable; until then the predictor
is marked unavailable.
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


class ParrotConditions(BaseConditionsPredictor):
    name = "parrot"
    description = "Parrot transformer condition predictor (Wang 2023)."
    citation = "Wang et al., Research 2023, 6:0231 (wangxr0526/Parrot)"
    extras_install = "parrot"

    def __init__(self) -> None:
        super().__init__()
        self._predictor: Any = None

    def load(self) -> None:
        from ...config import get_settings

        settings = get_settings()
        ckpt_dir = os.environ.get(
            "PARROT_MODEL_PATH",
            str(settings.model_dir / "parrot" / "uspto_condition"),
        )
        if not os.path.exists(ckpt_dir):
            raise FileNotFoundError(
                f"Parrot model dir not found at {ckpt_dir}. Download from the project release page."
            )
        from parrot.inference import ParrotPredictor  # type: ignore

        self._predictor = ParrotPredictor(ckpt_dir, device=settings.resolve_device())

    def predict_sync(self, reactants: str, product: str, top_k: int) -> list[ConditionsPrediction]:
        rxn = build_reaction_smiles(reactants=reactants, product=product)
        raw = self._predictor.predict(rxn, top_n=top_k)
        # Parrot returns a list of dicts: {"catalyst": [...], "solvent": [...],
        # "reagent": [...], "temperature": float, "score": float}
        preds: list[ConditionsPrediction] = []
        for i, item in enumerate(raw[:top_k]):
            preds.append(
                ConditionsPrediction(
                    catalysts=_as_list(item.get("catalyst") or item.get("catalysts")),
                    solvents=_as_list(item.get("solvent") or item.get("solvents")),
                    reagents=_as_list(item.get("reagent") or item.get("reagents")),
                    temperature_c=_as_float(item.get("temperature")),
                    score=min(1.0, max(0.0, float(item.get("score", 0.5)))),
                    rank=i + 1,
                    source_model=self.name,
                )
            )
        return preds


def _as_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    return [str(x) for x in v]


def _as_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


try:
    import torch  # noqa: F401

    register_conditions(ParrotConditions())
except Exception as exc:
    mark_unavailable(
        ParrotConditions.name,
        "conditions",
        f"missing optional deps (install `chemclaw-mcp-rxnpredict[parrot]` and clone "
        f"wangxr0526/Parrot, with checkpoint at $PARROT_MODEL_PATH): {exc!r}",
    )
