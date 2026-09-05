"""The `rxnpredict` MCP tool surface: what will this reaction give, and under what conditions.

**These docstrings are the prompt.** They say what each tool is for, what its arguments mean in a
chemist's terms, and — the part most easily left out — what the answer is *not* evidence of. A
consensus product from a set of sequence models is a literature-shaped guess, not a result; every
docstring here says so, because the number that reaches a chemist without that sentence attached is
the one that gets believed.

Two properties of this server shape every tool below:

- **It is an ensemble, and the spread is information.** `per_model` and `contributing_models` come
  back with every prediction precisely so the agent can say "four of five models agree" or "only the
  rule-based one produced this". A consensus of one is not a consensus, and `n_models_succeeded`
  is how you tell. A consensus of *none* is not an answer at all, and the ensemble tools refuse
  rather than return one — see `_survivors`, which is the floor under that degradation.
- **Predictors that are not installed are reported, not hidden.** `list_available_models` names
  every predictor this build knows about, whether it loaded, and why not. An answer computed from
  one predictor when the deployment expected five is a silent degradation otherwise.

Inference is CPU-bound and runs in a worker thread (`BasePredictor.predict` uses
`asyncio.to_thread`), so a slow model does not block the event loop serving the other requests.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any, TypeVar

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from chemclaw_mcp_rxnpredict.engine.base_doubles import register_requested
from chemclaw_mcp_rxnpredict.engine.config import get_settings
from chemclaw_mcp_rxnpredict.engine.meta.aggregator import (
    aggregate_conditions,
    aggregate_forward,
)
from chemclaw_mcp_rxnpredict.engine.meta.classifier import classify_reaction as _classify
from chemclaw_mcp_rxnpredict.engine.predictors import (
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

# The largest `top_k` a caller may ask any tool here for.
#
# **It has to be on the tool signature, because that is the only schema a caller ever sees.** The
# same bound was already written on `ForwardRequest`/`ConditionsRequest` in `engine/schemas.py`,
# which nothing imports — so it read as present in review and was absent at runtime, and the served
# input schema was a bare `{"default": 5, "type": "integer"}`. `reaction_t5` passes `top_k` into
# `num_beams` and `num_return_sequences`, so an unbounded integer is an unbounded allocation inside
# a worker thread that a client timeout cannot stop; and a negative one reached
# `sorted_candidates[:top_k]`, which dropped the only prediction and returned a `consensus` that
# contradicted `per_model`.
MAX_TOP_K = 50

TopK = Annotated[int, Field(ge=1, le=MAX_TOP_K)]

# Import every predictor module once, here, so `list_available_models` is truthful from the first
# request and a missing optional dependency is a recorded reason rather than a stack trace.
discover_predictors()

# Then register any deterministic doubles the configuration named. After discovery, never before:
# a real predictor of the same name must win, and `register_requested` skips names already in the
# registry. This is what gives `CHEMCLAW_RXNPREDICT_ENABLED_FORWARD_MODELS=fake_a` a working tool
# surface with no model weights; with no double named, it registers nothing.
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
        if settings.use_class_priors and settings.model_trust_priors_by_class
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

    One function decides what is served, for the ensemble tools and for the single-model ones.
    They used to disagree: the ensemble narrowed the registry through `_select`, while these looked
    the predictor up in the raw registry and called it, so a predictor an operator had disabled
    after a bad checkpoint bake stayed reachable through a declared, advertised `read_only` tool.
    """
    names = ", ".join(sorted(_served_names(served))) or "none"
    return ValueError(
        f"this deployment does not serve a {kind} predictor named {model_name!r} "
        f"(serving: {names}). It is either not installed or switched off by configuration; "
        "list_available_models says which, and why."
    )


_Prediction = TypeVar("_Prediction")


def _survivors(
    kind: str,
    predictors: list[object],
    results: list[Any],
) -> dict[str, list[_Prediction]]:
    """Pair each predictor with its result, drop the ones that failed, and refuse if none is left.

    **One predictor failing must not cost the ensemble; every predictor failing must not cost the
    caller the truth.** `gather(..., return_exceptions=True)` degrades an ensemble one model at a
    time, which is right, and it kept degrading all the way to nothing: with every installed
    predictor raising, both tools returned `consensus: []`, `per_model: {}`,
    `n_models_succeeded: 0` and no error at all. So a vanished checkpoint mount — or the egress
    guard refusing every weight fetch, which arrives as `EgressForbidden`, an `OSError` — read from
    outside the pod as a healthy server answering a hard question: `chemclaw_mcp_tool_calls_total`
    booked `outcome="ok"`, and the `refused`/`failed` split that exists for exactly this showed
    nothing. A consensus over zero models is not a weak answer, it is the absence of one.

    Written once for both tools because the loop was already identical in both, and a rule about
    when this server refuses that held in one of them would be the more dangerous half of a bug.

    Raises:
        ValueError: every queried predictor failed. Worded for the model — `connector_app` passes a
            `ValueError` through verbatim — which is also why it names each fault by its exception
            *type* and never its message: a predictor's own text is where a checkpoint path, a DSN
            or a token would be. The full `repr` goes to the log beside it, under the same
            predictor name, so the two are one fault an operator can join.
    """
    per_model: dict[str, list[_Prediction]] = {}
    failures: list[str] = []
    for predictor, result in zip(predictors, results, strict=True):
        name = predictor.name  # type: ignore[attr-defined]
        if isinstance(result, BaseException):
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

    Deliberately names what *is* installed, so the next step is obvious: either call
    `list_available_models` to see why a predictor did not load, or stop asking this server.
    """
    return ValueError(
        f"no {kind} predictors are available in this deployment "
        f"({len(unavailable())} known predictor(s) failed to load). "
        "Call list_available_models to see which, and why — this server cannot answer without one."
    )


@server.tool()
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
            chemistry, so do not re-ask the same question until it is fixed.
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
            chemistry, and the message says which predictors and what kind of fault.
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
                unavailable_reason=reason,
            )
            for name, (found_kind, reason) in unavailable_by_name.items()
            if found_kind == kind
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
