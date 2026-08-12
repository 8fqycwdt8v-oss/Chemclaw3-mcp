"""Predictor registry.

Each predictor module registers itself by importing this module's
`register_forward` / `register_conditions` decorators. Modules that fail to
import (missing optional deps) are caught and logged; the corresponding
predictor is marked unavailable rather than crashing server startup.
"""

from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import BaseConditionsPredictor, BaseForwardPredictor

logger = logging.getLogger(__name__)

_FORWARD: dict[str, BaseForwardPredictor] = {}
_CONDITIONS: dict[str, BaseConditionsPredictor] = {}
_UNAVAILABLE: dict[str, tuple[str, str]] = {}  # name -> (kind, reason)


def register_forward(predictor: BaseForwardPredictor) -> None:
    if predictor.name in _FORWARD:
        raise ValueError(f"Duplicate forward predictor: {predictor.name}")
    _FORWARD[predictor.name] = predictor
    logger.info("Registered forward predictor: %s", predictor.name)


def register_conditions(predictor: BaseConditionsPredictor) -> None:
    if predictor.name in _CONDITIONS:
        raise ValueError(f"Duplicate conditions predictor: {predictor.name}")
    _CONDITIONS[predictor.name] = predictor
    logger.info("Registered conditions predictor: %s", predictor.name)


def mark_unavailable(name: str, kind: str, reason: str) -> None:
    _UNAVAILABLE[name] = (kind, reason)
    logger.warning("Predictor %s (%s) unavailable: %s", name, kind, reason)


def get_forward(name: str) -> BaseForwardPredictor:
    return _FORWARD[name]


def get_conditions(name: str) -> BaseConditionsPredictor:
    return _CONDITIONS[name]


def list_forward() -> list[BaseForwardPredictor]:
    return list(_FORWARD.values())


def list_conditions() -> list[BaseConditionsPredictor]:
    return list(_CONDITIONS.values())


def unavailable() -> dict[str, tuple[str, str]]:
    return dict(_UNAVAILABLE)


_DISCOVERY_DONE = False

_FORWARD_MODULES = [
    "chemclaw_mcp_rxnpredict.engine.predictors.forward.reaction_t5",
    "chemclaw_mcp_rxnpredict.engine.predictors.forward.t5chem",
    "chemclaw_mcp_rxnpredict.engine.predictors.forward.molecular_transformer",
    "chemclaw_mcp_rxnpredict.engine.predictors.forward.megan",
    "chemclaw_mcp_rxnpredict.engine.predictors.forward.graphrxn",
    "chemclaw_mcp_rxnpredict.engine.predictors.forward.chemformer",
]

_CONDITIONS_MODULES = [
    "chemclaw_mcp_rxnpredict.engine.predictors.conditions.rxn_insight",
    "chemclaw_mcp_rxnpredict.engine.predictors.conditions.parrot",
    "chemclaw_mcp_rxnpredict.engine.predictors.conditions.reagents_mt",
    "chemclaw_mcp_rxnpredict.engine.predictors.conditions.two_stage_dnn",
    "chemclaw_mcp_rxnpredict.engine.predictors.conditions.askcos_condition",
]


def discover_predictors() -> None:
    """Import all predictor modules; record import failures as unavailable."""
    global _DISCOVERY_DONE
    if _DISCOVERY_DONE:
        return
    for modname in _FORWARD_MODULES + _CONDITIONS_MODULES:
        try:
            importlib.import_module(modname)
        except Exception as exc:
            # Predictor modules call mark_unavailable themselves when their hard deps fail;
            # this is the catch-all for truly broken modules.
            short = modname.rsplit(".", 1)[-1]
            kind = "forward" if "forward" in modname else "conditions"
            mark_unavailable(short, kind, f"import failed: {exc!r}")
    _DISCOVERY_DONE = True
