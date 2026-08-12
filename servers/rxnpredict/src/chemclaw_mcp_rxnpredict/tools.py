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
  is how you tell.
- **Predictors that are not installed are reported, not hidden.** `list_available_models` names
  every predictor this build knows about, whether it loaded, and why not. An answer computed from
  one predictor when the deployment expected five is a silent degradation otherwise.

Inference is CPU-bound and runs in a worker thread (`BasePredictor.predict` uses
`asyncio.to_thread`), so a slow model does not block the event loop serving the other requests.
"""

from __future__ import annotations

import asyncio
import logging

from mcp.server.fastmcp import FastMCP

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

# Import every predictor module once, here, so `list_available_models` is truthful from the first
# request and a missing optional dependency is a recorded reason rather than a stack trace.
discover_predictors()


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
    top_k: int = 5,
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
        ValueError: if this deployment has no forward predictor installed.
    """
    settings = get_settings()
    predictors = _forward_predictors(models)
    if not predictors:
        raise _no_predictors("forward")

    results = await asyncio.gather(
        *(p.predict(reactants, top_k) for p in predictors),  # type: ignore[attr-defined]
        return_exceptions=True,
    )
    per_model: dict[str, list[ForwardPrediction]] = {}
    for predictor, result in zip(predictors, results, strict=True):
        name = predictor.name  # type: ignore[attr-defined]
        if isinstance(result, BaseException):
            # One predictor failing must not cost the ensemble: the answer is worth less, and
            # `n_models_succeeded` is how the caller learns that it is.
            logger.warning("forward predictor %s failed: %r", name, result)
            continue
        per_model[name] = result

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
    top_k: int = 5,
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
        ValueError: if this deployment has no condition predictor installed.
    """
    settings = get_settings()
    predictors = _conditions_predictors(models)
    if not predictors:
        raise _no_predictors("conditions")

    results = await asyncio.gather(
        *(p.predict(reactants, product, top_k) for p in predictors),  # type: ignore[attr-defined]
        return_exceptions=True,
    )
    per_model: dict[str, list[ConditionsPrediction]] = {}
    for predictor, result in zip(predictors, results, strict=True):
        name = predictor.name  # type: ignore[attr-defined]
        if isinstance(result, BaseException):
            logger.warning("conditions predictor %s failed: %r", name, result)
            continue
        per_model[name] = result

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
    top_k: int = 5,
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
    matches = [p for p in list_forward() if p.name == model_name]
    if not matches:
        loaded = ", ".join(sorted(p.name for p in list_forward())) or "none"
        raise ValueError(f"no forward predictor named {model_name!r} is loaded (loaded: {loaded})")
    return await matches[0].predict(reactants, top_k)


@server.tool()
async def predict_conditions_single_model(
    model_name: str,
    reactants: str,
    product: str,
    top_k: int = 5,
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
    matches = [p for p in list_conditions() if p.name == model_name]
    if not matches:
        loaded = ", ".join(sorted(p.name for p in list_conditions())) or "none"
        raise ValueError(
            f"no conditions predictor named {model_name!r} is loaded (loaded: {loaded})"
        )
    return await matches[0].predict(reactants, product, top_k)


@server.tool()
def list_available_models() -> ModelsResponse:
    """List every predictor this build knows about, whether it loaded, and why not.

    Call this before trusting a consensus, and always when a prediction looks thin. A deployment
    that expected five predictors and installed one still returns an answer — it is just an answer
    from one model wearing the word "consensus", and this is the tool that reveals it.

    Each entry carries the predictor's citation, so a result can be attributed to the paper behind
    the model rather than to "the server".

    Returns:
        Forward and condition predictors, each with `available`, a description, a citation, the pip
        extra that would install it, and — when it did not load — the reason.
    """
    unavailable_by_name = unavailable()

    def _rows(kind: str, loaded: list[object]) -> list[ModelInfo]:
        rows = [
            ModelInfo(
                name=p.name,  # type: ignore[attr-defined]
                kind=kind,  # type: ignore[arg-type]
                available=True,
                description=p.description,  # type: ignore[attr-defined]
                citation=p.citation,  # type: ignore[attr-defined]
                extras_install=p.extras_install,  # type: ignore[attr-defined]
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
        forward=_rows("forward", list(list_forward())),
        conditions=_rows("conditions", list(list_conditions())),
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
