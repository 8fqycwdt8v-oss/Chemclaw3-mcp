"""The `rxnpredict` MCP tool surface: what will this reaction give, and under what conditions.

The tool docstrings are the prompt and state what an answer is not evidence of: a consensus from
sequence models is a literature-shaped guess, not a result. Every prediction returns `per_model`,
`contributing_models` and `n_models_succeeded` so the agent can report agreement; a consensus of
none is refused (`_survivors`). `list_available_models` names every predictor and why it did not
load. Inference runs in worker threads so the event loop stays free.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import Annotated, Any, ParamSpec, TypeVar

from mcp.server.fastmcp import FastMCP
from mcp_server_kit import degradation
from mcp_server_kit.limits import env_bound
from pydantic import Field

from chemclaw_mcp_rxnpredict.engine.admission import (
    ADMISSION_MARKER,
    DEFAULT_MAX_CONCURRENT_PREDICTIONS,
    Admission,
)
from chemclaw_mcp_rxnpredict.engine.base_doubles import register_requested
from chemclaw_mcp_rxnpredict.engine.config import get_settings, inference_threads
from chemclaw_mcp_rxnpredict.engine.meta.aggregator import (
    aggregate_conditions,
    aggregate_forward,
)
from chemclaw_mcp_rxnpredict.engine.meta.classifier import classify_reaction as _classify
from chemclaw_mcp_rxnpredict.engine.predictors import (
    SERVER,
    discover_predictors,
    list_conditions,
    list_forward,
    unavailable,
)
from chemclaw_mcp_rxnpredict.engine.preprocessing import canonical_multi_smiles, canonical_smiles
from chemclaw_mcp_rxnpredict.engine.schemas import (
    ClassifyResponse,
    ConditionsPrediction,
    ConditionsResponse,
    ForwardPrediction,
    ForwardResponse,
    ModelInfo,
    ModelsResponse,
)

logger = logging.getLogger(__name__)

server = FastMCP("rxnpredict")

# The pod's ceiling on concurrent inference, built at import so the gate enforces the number it was
# built from; a test replaces this attribute, not the variable. `env_bound` refuses `0`, which here
# would mean "serve nothing". See `engine/admission.py`.
_MAX_CONCURRENT_PREDICTIONS = env_bound(
    "CHEMCLAW_RXNPREDICT_MAX_CONCURRENT_PREDICTIONS",
    default=DEFAULT_MAX_CONCURRENT_PREDICTIONS,
    minimum=1,
    consequence="every prediction this server is asked for would be refused",
)
_admission = Admission(_MAX_CONCURRENT_PREDICTIONS)

_P = ParamSpec("_P")

# The largest `top_k` a caller may ask any tool here for.
#
# It must be on the tool signatures, the only schema a caller sees: `reaction_t5` passes `top_k`
# into beam search, so an unbounded value is an unbounded allocation, and a negative one would
# truncate the consensus.
MAX_TOP_K = 50

TopK = Annotated[int, Field(ge=1, le=MAX_TOP_K)]

# Import every predictor module once so `list_available_models` is truthful from the first request.
discover_predictors()

# Then register any doubles the configuration named. After discovery, so a real predictor of the
# same name wins.
register_requested()


def _safe_canon_reactants(smiles: str) -> str:
    """Canonical reactant SMILES, tolerating a full `reactants>agents>products` string."""
    try:
        return canonical_multi_smiles(smiles.split(">")[0])
    except ValueError:
        return smiles


def _safe_canon_single(smiles: str) -> str:
    """Canonical SMILES for one molecule, returning the input unchanged if RDKit refuses it."""
    try:
        return canonical_smiles(smiles)
    except ValueError:
        return smiles


def _provenance(predictors: list[str]) -> str:
    """The sentence that travels with a prediction: who voted, and under which weighting."""
    settings = get_settings()
    weighting = (
        "per-reaction-class trust priors"
        if settings.use_class_priors and settings.class_priors()
        else "global per-model trust priors (no per-class calibration is loaded)"
    )
    return (
        f"Borda-weighted consensus over {len(predictors)} predictor(s) "
        f"[{', '.join(sorted(predictors)) or 'none'}], weighted by {weighting}."
    )


def _select(available: list[str], requested: list[str] | None) -> set[str] | None:
    """Which predictors to query: the caller's subset, narrowed by configuration."""
    settings = get_settings()
    disabled = settings.parse_disabled()
    chosen = {name for name in available if name not in disabled}
    if requested is not None:
        chosen &= set(requested)
    return chosen


def _forward_predictors(requested: list[str] | None) -> list[object]:
    """The enabled forward predictors, after configuration and the caller's subset."""
    settings = get_settings()
    enabled = settings.parse_enabled(settings.enabled_forward_models)
    allowed = _select([p.name for p in list_forward()], requested)
    return [
        p
        for p in list_forward()
        if p.name in (allowed or set()) and (enabled is None or p.name in enabled)
    ]


def _conditions_predictors(requested: list[str] | None) -> list[object]:
    """The enabled condition predictors, after configuration and the caller's subset."""
    settings = get_settings()
    enabled = settings.parse_enabled(settings.enabled_conditions_models)
    allowed = _select([p.name for p in list_conditions()], requested)
    return [
        p
        for p in list_conditions()
        if p.name in (allowed or set()) and (enabled is None or p.name in enabled)
    ]


def _served_names(predictors: list[object]) -> set[str]:
    """The predictor IDs this deployment will actually answer with."""
    return {p.name for p in predictors}  # type: ignore[attr-defined]


def _not_served(kind: str, model_name: str, served: list[object]) -> ValueError:
    """The error for a predictor this deployment does not serve — absent *or* switched off.

    The single-model tools use the same served set as the ensembles, so a disabled predictor is
    unreachable through every tool.
    """
    names = ", ".join(sorted(_served_names(served))) or "none"
    return ValueError(
        f"this deployment does not serve a {kind} predictor named {model_name!r} "
        f"(serving: {names}). It is either not installed or switched off by configuration; "
        "list_available_models says which, and why."
    )


_Prediction = TypeVar("_Prediction")


_T = TypeVar("_T")


def _single_model_slots() -> int:
    """What one named-model call costs: one predictor's forward pass, at its configured width."""
    return inference_threads()


def _forward_ensemble_slots() -> int:
    """What one `predict_forward_reaction` costs, in cores: fan-out width times thread width.

    Read from the deployment's enabled list, not the caller's `models` argument, so a caller cannot
    lower the charge.
    """
    return max(1, len(_forward_predictors(None))) * inference_threads()


def _conditions_ensemble_slots() -> int:
    """`_forward_ensemble_slots` for the conditions ensemble, over its own predictor registry."""
    return max(1, len(_conditions_predictors(None))) * inference_threads()


def _admitted(
    work: Callable[_P, Awaitable[_T]], *, cost: Callable[[], int] = _single_model_slots
) -> Callable[_P, Coroutine[Any, Any, _T]]:
    """Bound how much inference runs at once, refusing promptly when the pod is full.

    Stamped with `ADMISSION_MARKER` so `tests/test_admission.py` checks the gated set against the
    served surface. `asyncio.shield` releases slots when the work finishes, not when the caller
    stops waiting, since cancellation does not stop the worker threads. `functools.wraps` is
    required: FastMCP builds the argument schema from the wrapped signature.
    """

    @functools.wraps(work)
    async def _guarded(*args: _P.args, **kwargs: _P.kwargs) -> _T:
        return await _admission.admit(
            work(*args, **kwargs), lambda: _admission.acquire(work.__name__, cost())
        )

    setattr(_guarded, ADMISSION_MARKER, True)
    return _guarded


def _admitted_forward_ensemble(
    work: Callable[_P, Awaitable[_T]],
) -> Callable[_P, Coroutine[Any, Any, _T]]:
    """`_admitted` for the forward consensus, which costs one thread set per enabled predictor."""
    return _admitted(work, cost=_forward_ensemble_slots)


def _admitted_conditions_ensemble(
    work: Callable[_P, Awaitable[_T]],
) -> Callable[_P, Coroutine[Any, Any, _T]]:
    """`_admitted` for the conditions consensus. See `_forward_ensemble_slots`."""
    return _admitted(work, cost=_conditions_ensemble_slots)


def _survivors(
    kind: str,
    predictors: list[object],
    results: list[Any],
) -> dict[str, list[_Prediction]]:
    """Pair each predictor with its result, drop the ones that failed, and refuse if none is left.

    One predictor failing must not cost the ensemble; every predictor failing must not return an
    empty consensus that looks like a healthy answer. Shared by both ensemble tools.

    Raises:
        ValueError: Every queried predictor failed. Passed to the model verbatim, so it names each
            fault by exception *type* only (a message could carry a path or credential); the full
            `repr` is logged under the same predictor name.
    """
    per_model: dict[str, list[_Prediction]] = {}
    failures: list[str] = []
    for predictor, result in zip(predictors, results, strict=True):
        name = predictor.name  # type: ignore[attr-defined]
        if isinstance(result, BaseException):
            # Counted, since a partial failure is still `outcome="ok"`. The label is the registry
            # name, a source constant: `/metrics` is unauthenticated, so nothing a caller sends may
            # become a label.
            degradation.record(server=SERVER, component=name, cause=degradation.classify(result))
            logger.warning("%s predictor %s failed: %r", kind, name, result)
            failures.append(f"{name} ({type(result).__name__})")
            continue
        per_model[name] = result
    if not per_model:
        raise ValueError(
            f"every {kind} predictor this deployment queried failed, so there is no prediction "
            f"to report: {', '.join(failures)}. This is a fault in the server rather than a "
            "statement about the chemistry — a checkpoint that will not load, or an environment "
            "that refuses what a model tries to fetch. Call list_available_models to see what "
            "this build has, and do not re-ask the same question until it is fixed."
        )
    return per_model


def _no_predictors(kind: str) -> ValueError:
    """The error an agent should see when this build has nothing to answer with.

    Names what is installed and points at `list_available_models`.
    """
    return ValueError(
        f"no {kind} predictors are available in this deployment "
        f"({len(unavailable())} known predictor(s) failed to load). "
        "Call list_available_models to see which, and why — this server cannot answer without one."
    )


@server.tool()
@_admitted_forward_ensemble
async def predict_forward_reaction(
    reactants: str,
    top_k: TopK = 5,
    models: list[str] | None = None,
) -> ForwardResponse:
    """Predict the products of a reaction from its reactants — the consensus of several models.

    Answers "what will this give?" by running every installed forward predictor and combining their
    ranked outputs by Borda-weighted voting, so a product several architectures agree on outranks
    one only the strongest model proposed. Use it to sanity-check a proposed step, to spot the
    obvious side-product, or to ask whether a transformation is one the literature-trained models
    recognise at all.

    **This is a prediction, not a result, and the ensemble does not make it a measurement.** These
    models are trained largely on USPTO patent reactions: they are strong on common couplings and
    weak on stereochemistry, on rare reaction classes, and on anything under conditions the training
    data does not contain. They will return a confident-looking product for chemistry that does not
    work. Read `n_models_succeeded` and `contributing_models` before quoting a consensus — one model
    agreeing with itself is not agreement — and present the answer as a hypothesis for the bench.

    `consensus_score` is the share of the weight a candidate could have attained if every voting
    model had ranked it first at full confidence. A lone predictor's weak guess therefore scores
    low; it is not 1.0 at rank 1 by definition. It is still a *relative* number over the models
    that answered, so quote it with `vote_count` and `n_models_succeeded`, never on its own.

    Args:
        reactants: Dot-separated reactant SMILES, e.g. `CC(=O)Cl.Nc1ccccc1`. A full
            `reactants>agents>` reaction SMILES is accepted and the reactant half is used.
        top_k: How many ranked products to return per model, and in the consensus (default 5).
        models: Restrict to these predictor IDs. Leave unset to use every enabled predictor, which
            is what makes the answer a consensus.

    Returns:
        The ranked consensus with per-product vote counts and contributing models, every model's own
        ranked output under `per_model`, how many were queried and how many succeeded, and `source`.

    Raises:
        ValueError: if this deployment has no forward predictor installed, or if every predictor it
            queried failed. The second is a fault in the server — an unloadable checkpoint, an
            environment refusing what a model tries to fetch — and not a statement about the
            chemistry, so do not re-ask the same question until it is fixed. A third case is
            neither: a consensus runs every enabled predictor at once, so a busy pod refuses this
            call promptly rather than queueing it. That refusal names the ceiling and is worth
            retrying, or worth replacing with `predict_forward_single_model`, which costs less.
    """
    settings = get_settings()
    predictors = _forward_predictors(models)
    if not predictors:
        raise _no_predictors("forward")

    results = await asyncio.gather(
        *(p.predict(reactants, top_k) for p in predictors),  # type: ignore[attr-defined]
        return_exceptions=True,
    )
    per_model: dict[str, list[ForwardPrediction]] = _survivors("forward", predictors, results)

    return ForwardResponse(
        consensus=aggregate_forward(per_model, settings, top_k, reactants=reactants),
        per_model=per_model,
        canonical_reactants=_safe_canon_reactants(reactants),
        n_models_queried=len(predictors),
        n_models_succeeded=len(per_model),
        source=_provenance(list(per_model)),
    )


@server.tool()
@_admitted_conditions_ensemble
async def predict_reaction_conditions(
    reactants: str,
    product: str,
    top_k: TopK = 5,
    models: list[str] | None = None,
) -> ConditionsResponse:
    """Suggest catalyst, solvent, reagent and temperature for a known transformation.

    Answers "how would people run this?" for a reaction whose product you already know. The voting
    unit is the whole condition set, with temperature bucketed to 10 °C bins, so near-agreement
    between models counts as agreement instead of being split across three almost-identical
    suggestions.

    **A suggestion is a starting point for a screen, not a procedure.** These models reproduce what
    is common in the training literature, which is not the same as what is best, safe, or available
    in your plant — and they know nothing about your substrate's other functionality, the scale, the
    equipment, or the hazard profile of what they propose. Check any suggested solvent against its
    ICH class and hazard data before it reaches a plan (the `props` server answers that), and treat
    the temperature as a bucket rather than a set point.

    `consensus_score` means what it does in `predict_forward_reaction`: the share of the weight
    this condition set could have attained had every voting model ranked it first at full
    confidence. Quote it with `vote_count`.

    Args:
        reactants: Dot-separated reactant SMILES.
        product: SMILES of the intended product. Required — conditions are predicted *for* a known
            transformation, so this is what distinguishes an amidation from an esterification of the
            same acid.
        top_k: How many ranked condition sets to return (default 5).
        models: Restrict to these predictor IDs. Leave unset to use every enabled predictor.

    Returns:
        Ranked condition sets with vote counts and contributing models, each model's own output, and
        `source`. Temperatures are in degrees Celsius; `null` means the model offered none.

    Raises:
        ValueError: if this deployment has no condition predictor installed, or if every predictor
            it queried failed — which is a fault in the server rather than a statement about the
            chemistry, and the message says which predictors and what kind of fault. A busy pod
            also refuses this call promptly rather than queueing it, because a consensus runs every
            enabled predictor at once; that refusal names the ceiling and is worth retrying.
    """
    settings = get_settings()
    predictors = _conditions_predictors(models)
    if not predictors:
        raise _no_predictors("conditions")

    results = await asyncio.gather(
        *(p.predict(reactants, product, top_k) for p in predictors),  # type: ignore[attr-defined]
        return_exceptions=True,
    )
    per_model: dict[str, list[ConditionsPrediction]]
    per_model = _survivors("conditions", predictors, results)

    return ConditionsResponse(
        consensus=aggregate_conditions(
            per_model, settings, top_k, reactants=reactants, product=product
        ),
        per_model=per_model,
        canonical_reactants=_safe_canon_reactants(reactants),
        canonical_product=_safe_canon_single(product),
        n_models_queried=len(predictors),
        n_models_succeeded=len(per_model),
        source=_provenance(list(per_model)),
    )


@server.tool()
@_admitted
async def predict_forward_single_model(
    model_name: str,
    reactants: str,
    top_k: TopK = 5,
) -> list[ForwardPrediction]:
    """Ask one named forward predictor on its own, bypassing the consensus.

    For when the question is about a *model* rather than about a reaction: checking whether one
    predictor is the reason a consensus looks odd, or comparing two architectures on a case where
    they disagree. Prefer `predict_forward_reaction` for chemistry questions — a single model's
    output carries none of the agreement that makes the ensemble worth having.

    Args:
        model_name: A predictor ID from `list_available_models`, e.g. `reaction_t5_v2`.
        reactants: Dot-separated reactant SMILES.
        top_k: How many ranked products to return (default 5).

    Returns:
        That model's ranked predictions, each with its own score and rank.

    Raises:
        ValueError: if no predictor of that name is loaded — the message names what is.
    """
    matches = [p for p in _forward_predictors([model_name])]
    if not matches:
        raise _not_served("forward", model_name, _forward_predictors(None))
    return await matches[0].predict(reactants, top_k)  # type: ignore[attr-defined,no-any-return]


@server.tool()
@_admitted
async def predict_conditions_single_model(
    model_name: str,
    reactants: str,
    product: str,
    top_k: TopK = 5,
) -> list[ConditionsPrediction]:
    """Ask one named condition predictor on its own, bypassing the consensus.

    The condition-side counterpart of `predict_forward_single_model`, and the same caveat applies:
    this is for interrogating a model, not for answering a chemistry question.

    Args:
        model_name: A predictor ID from `list_available_models`, e.g. `rxn_insight`.
        reactants: Dot-separated reactant SMILES.
        product: SMILES of the intended product.
        top_k: How many ranked condition sets to return (default 5).

    Returns:
        That model's ranked condition sets. Temperatures are in degrees Celsius.

    Raises:
        ValueError: if no predictor of that name is loaded — the message names what is.
    """
    matches = [p for p in _conditions_predictors([model_name])]
    if not matches:
        raise _not_served("conditions", model_name, _conditions_predictors(None))
    return await matches[0].predict(  # type: ignore[attr-defined,no-any-return]
        reactants, product, top_k
    )


@server.tool()
def list_available_models() -> ModelsResponse:
    """List every predictor this build knows about, whether it loaded, and why not.

    Call this before trusting a consensus, and always when a prediction looks thin. A deployment
    that expected five predictors and installed one still returns an answer — it is just an answer
    from one model wearing the word "consensus", and this is the tool that reveals it.

    Each entry carries the predictor's citation, so a result can be attributed to the paper behind
    the model rather than to "the server".

    **`available` means "this deployment will answer with it", not "the import succeeded".** A
    predictor an operator switched off through `CHEMCLAW_RXNPREDICT_DISABLED_MODELS` or the
    `ENABLED_*_MODELS` allow-lists is reported unavailable with that as its reason — it read
    `available: true` until the day this became the same question the prediction tools ask.

    Returns:
        Forward and condition predictors, each with `available`, a description, a citation, the pip
        extra that would install it, and — when this deployment will not answer with it — the
        reason: it did not load, or configuration turned it off.
    """
    unavailable_by_name = unavailable()

    def _rows(kind: str, loaded: list[object], served: set[str]) -> list[ModelInfo]:
        rows = [
            ModelInfo(
                name=p.name,  # type: ignore[attr-defined]
                kind=kind,  # type: ignore[arg-type]
                available=p.name in served,  # type: ignore[attr-defined]
                description=p.description,  # type: ignore[attr-defined]
                citation=p.citation,  # type: ignore[attr-defined]
                extras_install=p.extras_install,  # type: ignore[attr-defined]
                unavailable_reason=(
                    None
                    if p.name in served  # type: ignore[attr-defined]
                    else "installed and loaded, but switched off by this deployment's "
                    "configuration (CHEMCLAW_RXNPREDICT_DISABLED_MODELS / ENABLED_*_MODELS)"
                ),
            )
            for p in loaded
        ]
        rows.extend(
            ModelInfo(
                name=name,
                kind=kind,  # type: ignore[arg-type]
                available=False,
                description="(not loaded)",
                unavailable_reason=entry.reason,
                unavailable_cause=entry.cause,
            )
            for name, entry in unavailable_by_name.items()
            if entry.kind == kind
        )
        return rows

    return ModelsResponse(
        forward=_rows("forward", list(list_forward()), _served_names(_forward_predictors(None))),
        conditions=_rows(
            "conditions", list(list_conditions()), _served_names(_conditions_predictors(None))
        ),
    )


@server.tool()
def classify_reaction(reactants: str, product: str | None = None) -> ClassifyResponse:
    """Name the coarse reaction class of a transformation, by SMARTS rules.

    The same classifier the ensemble uses internally to pick per-class trust weights, exposed
    because "what kind of reaction is this?" is a question worth being able to ask directly — and
    because seeing the class explains why the aggregator weighted the models the way it did.

    **Coarse by design.** It is a small set of SMARTS rules over a handful of common classes, not a
    reaction-classification model: it answers `other` freely, and `other` means "no rule matched",
    never "this is unusual chemistry". For a real classification use Chemclaw3's `rxnfp` similarity
    search, which compares against actual precedent.

    Args:
        reactants: Dot-separated reactant SMILES.
        product: Optional product SMILES. Supplying it lets the stricter rules fire, so the class
            is more often something other than `other`.

    Returns:
        The class label, the canonical inputs it was decided from, and `source`.
    """
    return ClassifyResponse(
        reaction_class=_classify(reactants, product=product),
        canonical_reactants=_safe_canon_reactants(reactants),
        canonical_product=_safe_canon_single(product) if product else None,
        source=(
            "SMARTS rule set in engine/meta/classifier.py — coarse, and `other` means that no "
            "rule matched rather than that the chemistry is unusual"
        ),
    )
