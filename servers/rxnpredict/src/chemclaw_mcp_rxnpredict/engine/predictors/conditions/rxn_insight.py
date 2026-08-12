"""Rxn-INSIGHT condition predictor.

Rodobbe et al. 2024 (J. Cheminform.) — `mrodobbe/Rxn-INSIGHT`.
Rule + similarity based. Sub-second on a laptop, no GPU. Returns suggested
catalysts, solvents, reagents and (optionally) temperature for a reaction.
"""

from __future__ import annotations

import logging
from typing import Any

from ...preprocessing import build_reaction_smiles
from ...schemas import ConditionsPrediction
from .. import mark_unavailable, register_conditions
from ..base import BaseConditionsPredictor

logger = logging.getLogger(__name__)


class RxnInsightConditions(BaseConditionsPredictor):
    name = "rxn_insight"
    description = (
        "Rxn-INSIGHT — rule + similarity based condition suggestion using "
        "bond-electron matrices (Rodobbe 2024)."
    )
    citation = "Rodobbe et al., J. Cheminform. 2024, 16:22 (mrodobbe/Rxn-INSIGHT)"
    extras_install = "rxn_insight"

    def __init__(self) -> None:
        super().__init__()
        self._insight_module: Any = None

    def load(self) -> None:
        # Import here so server startup doesn't require the dep.
        import rxn_insight

        self._insight_module = rxn_insight

    def predict_sync(self, reactants: str, product: str, top_k: int) -> list[ConditionsPrediction]:
        rxn_smiles = build_reaction_smiles(reactants=reactants, product=product)

        # Rxn-INSIGHT exposes its main entrypoint as `Reaction` and a
        # `suggest_conditions()` helper, but the API surface has shifted between
        # versions. Try the high-level helper first, then fall back to the class.
        suggestions = _invoke_rxn_insight(self._insight_module, rxn_smiles, top_k)

        preds: list[ConditionsPrediction] = []
        for i, sug in enumerate(suggestions[:top_k]):
            preds.append(
                ConditionsPrediction(
                    catalysts=_as_list(sug.get("catalyst") or sug.get("catalysts")),
                    solvents=_as_list(sug.get("solvent") or sug.get("solvents")),
                    reagents=_as_list(sug.get("reagent") or sug.get("reagents")),
                    temperature_c=_as_float(sug.get("temperature") or sug.get("temperature_c")),
                    score=max(0.01, 1.0 - 0.1 * i),
                    rank=i + 1,
                    source_model=self.name,
                )
            )
        return preds


def _invoke_rxn_insight(module: Any, rxn_smiles: str, top_k: int) -> list[dict[str, Any]]:
    """Adapter for Rxn-INSIGHT's evolving public API."""
    # Public helper (newer versions)
    if hasattr(module, "suggest_conditions"):
        suggested: list[dict[str, Any]] = module.suggest_conditions(rxn_smiles, top_n=top_k)
        return suggested

    # Reaction class fallback
    Reaction = getattr(module, "Reaction", None)
    if Reaction is None:
        raise RuntimeError("rxn_insight has neither `suggest_conditions` nor `Reaction`")
    rxn = Reaction(rxn_smiles)
    for attr in ("get_conditions", "suggest_conditions", "predict_conditions"):
        if hasattr(rxn, attr):
            result = getattr(rxn, attr)(top_n=top_k)
            return list(result) if not isinstance(result, list) else result
    raise RuntimeError("rxn_insight.Reaction exposes no condition-suggestion method")


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


try:
    import rxn_insight  # noqa: F401

    register_conditions(RxnInsightConditions())
except Exception as exc:
    mark_unavailable(
        RxnInsightConditions.name,
        "conditions",
        f"missing optional deps (install `chemclaw-mcp-rxnpredict[rxn_insight]`): {exc!r}",
    )
