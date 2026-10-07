"""Predictor registry, and the one place this server records that it lost a predictor.

Each predictor module registers itself via `register_forward` / `register_conditions`. A module that
fails to import is marked unavailable rather than crashing startup. `mark_unavailable` counts the
loss on `chemclaw_mcp_degraded_total` and records a classified cause from the exception itself (not
its text), which `engine/readiness.py` reads: an extra that is not installed is a deployment's
decision; a module present but broken is a broken image.
"""

from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING, NamedTuple

from mcp_server_kit import degradation

if TYPE_CHECKING:
    from .base import BaseConditionsPredictor, BaseForwardPredictor

logger = logging.getLogger(__name__)

SERVER = "rxnpredict"


class Unavailable(NamedTuple):
    """Why one predictor is not in this deployment's ensemble.

    Attributes:
        kind: `forward` or `conditions`.
        reason: The module's own sentence, naming the extra to install.
        cause: A `mcp_server_kit.degradation` cause; what separates "not installed" from "broken".
    """

    kind: str
    reason: str
    cause: str


_FORWARD: dict[str, BaseForwardPredictor] = {}
_CONDITIONS: dict[str, BaseConditionsPredictor] = {}
_UNAVAILABLE: dict[str, Unavailable] = {}


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


def mark_unavailable(
    name: str,
    kind: str,
    reason: str,
    *,
    exc: BaseException | None = None,
    optional: tuple[str, ...] | None = None,
) -> None:
    """Record — and count — that this deployment will answer without `name`.

    Args:
        name: The predictor's registry name; always a source constant, so the metric label is
            bounded.
        kind: `forward` or `conditions`.
        reason: The sentence a person reads, naming the extra to install.
        exc: What went wrong, classified rather than guessed from `reason`. Omitted only when there
            is no exception (an `ENABLED_*_MODELS` exclusion, recorded as `not_installed`).
        optional: The top-level modules the guard imports (the extra itself), so a missing
            dependency *of* an installed extra classifies as broken. `tests/test_readiness.py` holds
            each guard's list equal to its imports. `None` takes a `ModuleNotFoundError` at its
            word.
    """
    cause = (
        degradation.classify(exc, optional=optional)
        if exc is not None
        else degradation.CAUSE_NOT_INSTALLED
    )
    _UNAVAILABLE[name] = Unavailable(kind, reason, cause)
    degradation.record(server=SERVER, component=name, cause=cause)
    logger.warning("Predictor %s (%s) unavailable [%s]: %s", name, kind, cause, reason)


def get_forward(name: str) -> BaseForwardPredictor:
    return _FORWARD[name]


def get_conditions(name: str) -> BaseConditionsPredictor:
    return _CONDITIONS[name]


def list_forward() -> list[BaseForwardPredictor]:
    return list(_FORWARD.values())


def list_conditions() -> list[BaseConditionsPredictor]:
    return list(_CONDITIONS.values())


def unavailable() -> dict[str, Unavailable]:
    """Every predictor this build knows about and is not serving, by name."""
    return dict(_UNAVAILABLE)


_DISCOVERY_DONE = False

# Module path -> the registry name its predictor answers to (they differ for `reaction_t5`), so the
# catch-all files a failure under the same name as the module's own guard. It is also the complete
# component set for `degradation.register_components`. `tests/test_readiness.py` checks the values
# against each class's `name`.
_FORWARD_MODULES = {
    "chemclaw_mcp_rxnpredict.engine.predictors.forward.reaction_t5": "reaction_t5_v2",
    "chemclaw_mcp_rxnpredict.engine.predictors.forward.t5chem": "t5chem",
    "chemclaw_mcp_rxnpredict.engine.predictors.forward.molecular_transformer": (
        "molecular_transformer"
    ),
    "chemclaw_mcp_rxnpredict.engine.predictors.forward.megan": "megan",
    "chemclaw_mcp_rxnpredict.engine.predictors.forward.graphrxn": "graphrxn",
    "chemclaw_mcp_rxnpredict.engine.predictors.forward.chemformer": "chemformer",
}

_CONDITIONS_MODULES = {
    "chemclaw_mcp_rxnpredict.engine.predictors.conditions.rxn_insight": "rxn_insight",
    "chemclaw_mcp_rxnpredict.engine.predictors.conditions.parrot": "parrot",
    "chemclaw_mcp_rxnpredict.engine.predictors.conditions.reagents_mt": "reagents_mt",
    "chemclaw_mcp_rxnpredict.engine.predictors.conditions.two_stage_dnn": "two_stage_dnn",
    "chemclaw_mcp_rxnpredict.engine.predictors.conditions.askcos_condition": "askcos_condition",
}

# Every component name this server may publish on `chemclaw_mcp_degraded_total`, declared before the
# first `mark_unavailable` can fire.
degradation.register_components(*_FORWARD_MODULES.values(), *_CONDITIONS_MODULES.values())


def discover_predictors() -> None:
    """Import all predictor modules; record import failures as unavailable."""
    global _DISCOVERY_DONE
    if _DISCOVERY_DONE:
        return
    for modname, name in (*_FORWARD_MODULES.items(), *_CONDITIONS_MODULES.items()):
        try:
            importlib.import_module(modname)
        # BLE001: catch-all for a module that raised outside its own guard; `mark_unavailable`
        # classifies `exc`.
        except Exception as exc:  # noqa: BLE001
            # A module that raised outside its own guard did not fail an optional import, so it
            # classifies as `failed`. Filed under the registry name, which `list_available_models`
            # uses.
            kind = "forward" if modname in _FORWARD_MODULES else "conditions"
            mark_unavailable(name, kind, f"import failed: {exc!r}", exc=exc)
    _DISCOVERY_DONE = True
