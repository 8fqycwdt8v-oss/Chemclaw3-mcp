"""Predictor registry, and the one place this server records that it lost a predictor.

Each predictor module registers itself by importing this module's `register_forward` /
`register_conditions` decorators. Modules that fail to import are caught and logged; the
corresponding predictor is marked unavailable rather than crashing server startup.

**"Marked unavailable" was a log line and a dictionary, and neither is a signal.** Every one of
those losses makes the ensemble smaller, and `tools.py`'s own docstring calls that out — "an answer
computed from one predictor when the deployment expected five is a silent degradation otherwise" —
while resting the whole claim on `list_available_models`, a tool somebody has to think to call. From
a scrape there was nothing: a pod that lost four of its five predictors and one that was asked easy
questions published identical metrics.

So `mark_unavailable` is now the funnel for both halves of what was missing. It counts on
`chemclaw_mcp_degraded_total`, and it records **why** as one of `mcp_server_kit.degradation`'s
clamped causes rather than only as prose. The cause is what `engine/readiness.py` reads, and the
distinction it needs is the one this fleet already makes for `rxnlabel`'s optional models: a
predictor whose extra is simply not installed is a deployment's decision and this pod is ready; a
predictor whose module is present and blew up is a broken image and it is not. Both arrive here as
`Exception`, and the reason strings every module writes say "missing optional deps" for either —
which is why the exception itself is passed in and classified rather than its text being read.
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
        reason: The module's own sentence, naming the extra to install. What a person reads.
        cause: A `mcp_server_kit.degradation` cause. What `engine/readiness.py` acts on, and the
            only half of this that separates "not installed here" from "broken here".
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
    name: str, kind: str, reason: str, *, exc: BaseException | None = None
) -> None:
    """Record — and count — that this deployment will answer without `name`.

    Args:
        name: The predictor's registry name, or the module's short name where the module blew up
            before its class existed. Either way a constant in this package's source, which is what
            keeps the metric label bounded; nothing a request can influence reaches it.
        kind: `forward` or `conditions`.
        reason: The sentence a person reads, naming the extra to install.
        exc: What actually went wrong, so the cause is classified rather than guessed from the
            reason text. Omitted only where there is no exception — a predictor excluded by an
            `ENABLED_*_MODELS` allow-list is a deployment's decision and is `not_installed`.
    """
    cause = degradation.classify(exc) if exc is not None else degradation.CAUSE_NOT_INSTALLED
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
            # this is the catch-all for truly broken modules. It is the one that most needs the
            # exception passed through: a module that raised *outside* its own guard did not fail
            # on an optional import, so it classifies as `failed` and `engine/readiness.py` refuses
            # to take traffic for it.
            short = modname.rsplit(".", 1)[-1]
            kind = "forward" if "forward" in modname else "conditions"
            mark_unavailable(short, kind, f"import failed: {exc!r}", exc=exc)
    _DISCOVERY_DONE = True
