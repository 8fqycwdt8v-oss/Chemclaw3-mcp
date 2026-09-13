"""What this server has to have before it may take traffic, and what it is allowed to lack.

`/healthz` here verified the trust priors and nothing else, which left the thing this server *is*
outside the probe entirely: the ensemble. Every predictor is optional, loaded by importing its
module at startup, and a module that raises is caught, logged and recorded in `predictors`'s
unavailable map — so a pod that lost every forward predictor answered **200**, took traffic, and
raised "no forward predictors are available in this deployment" on every call. Measured: with
`CHEMCLAW_RXNPREDICT_ENABLED_FORWARD_MODELS=reaction_t5_v2` against a build that does not carry it,
`/healthz` was 200 and `predict_forward_reaction` raised on the first call.

**The distinction is the same one `rxnlabel` makes, and it has to be, because the dev checkout is
the counter-example.** A developer's tree carries none of the ML extras and every predictor reports
`ModuleNotFoundError`; that is the design working, not a broken image, and a probe that refused it
would make the whole suite unrunnable and teach everybody to ignore the signal. So:

* **a predictor whose optional dependency is absent** — `not_installed` — is a deployment's
  decision, and this pod is ready;
* **a kind of prediction this pod cannot make at all** — no forward predictor registered, or no
  conditions predictor — when a permanent cause is what took the last one, is a broken image and
  this
  pod is not. The measured defect is exactly that: `predict_forward_reaction` raised "no forward
  predictors are available in this deployment" on the first call while `/healthz` answered 200;
* **a predictor this deployment *named* in an allow-list and does not have** is the loudest case of
  all, because somebody wrote the name down. Ready is not an option there whatever the cause: an
  operator asking for `reaction_t5_v2` and getting a server that will answer with nothing has a
  configuration that cannot work, and it is better found by a probe than by the first chemist. This
  arm also covers the narrower case the bullet above does not — a named predictor that is broken
  while ten others serve — because naming it is what makes a thin ensemble wrong rather than normal.

**What is deliberately *not* a reason to leave is one broken predictor among eleven optional ones**,
and the first version of this module got that wrong in the direction that costs an outage. It
refused
for *any* entry whose cause was permanent, and a missing checkpoint file raises `FileNotFoundError`,
which is an `OSError`, which classifies `failed`, which is permanent. Driven on the real app: a pod
that had been answering with ten of its eleven predictors — the normal case by design — answered
**503** for one broken one, and since a restart cannot recreate a missing file the result was
`CrashLoopBackOff` and a capability that was simply down. A pod serving ten of eleven is serving.
The loss is counted on `chemclaw_mcp_degraded_total` and named in `list_available_models`, which is
where an operator's alert belongs; taking the pod out of rotation for it is not a smaller harm than
the degradation, it is a larger one.

`resource_exhausted` is deliberately not a reason to leave either, and the same arithmetic applies
to
every cause in the arms above: the refusal is about *configuration that cannot work*, never about
the
ensemble's size on its own. `degradation.PERMANENT_CAUSES` is what carries the transient/permanent
half of that rule for the whole fleet, and `connector_app`'s `/healthz` enforces it for any raise
this
module does not classify itself.

Not cached, and this is the one readiness check in the fleet that need not be: it reads two
dictionaries that are filled once at import and never written again, so it costs a comprehension.
"""

from __future__ import annotations

from mcp_server_kit import degradation

from chemclaw_mcp_rxnpredict.engine.config import get_settings
from chemclaw_mcp_rxnpredict.engine.predictors import (
    list_conditions,
    list_forward,
    unavailable,
)

__all__ = ["verify_predictors"]


def verify_predictors() -> None:
    """Refuse traffic for a kind of prediction this pod cannot make, or for a name it was given.

    Raises:
        RuntimeError: no predictor of a kind is registered and a permanent cause is what took the
            last one, or an `ENABLED_*_MODELS` allow-list names a predictor that is not registered.
            `connector_app` turns that into a 503 naming the predictor and the reason.
    """
    for kind, registered in (("forward", list_forward()), ("conditions", list_conditions())):
        if registered:
            continue
        broken = {
            name: entry
            for name, entry in unavailable().items()
            if entry.kind == kind and entry.cause in degradation.PERMANENT_CAUSES
        }
        if not broken:
            # Nothing of this kind loaded and nothing of this kind *broke*: a deployment that
            # installed none of the extras, which is every developer checkout and is the constraint
            # this whole rule is shaped around.
            continue
        detail = "; ".join(
            f"{name} ({entry.cause}): {entry.reason}" for name, entry in sorted(broken.items())
        )
        raise RuntimeError(
            f"this pod has no {kind} predictor at all and at least one it carries could not be "
            f"loaded, so every {kind} call would fail rather than answer with a smaller "
            f"ensemble: {detail}"
        )
    settings = get_settings()
    missing = sorted(
        _named_but_absent("forward", settings.parse_enabled(settings.enabled_forward_models))
        + _named_but_absent(
            "conditions", settings.parse_enabled(settings.enabled_conditions_models)
        )
    )
    if missing:
        causes = {
            name: entry.cause
            for name, entry in unavailable().items()
            if any(named.endswith(f"/{name}") for named in missing)
        }
        raise RuntimeError(
            "this deployment's enabled-model list names predictors that are not registered here, "
            f"so those calls cannot be answered: {', '.join(missing)}"
            + (f" ({causes})" if causes else "")
            + ". Check CHEMCLAW_RXNPREDICT_ENABLED_FORWARD_MODELS / _CONDITIONS_MODELS against "
            "list_available_models."
        )


def _named_but_absent(kind: str, enabled: set[str] | None) -> list[str]:
    """Names in an allow-list that no predictor of `kind` answers to.

    Args:
        kind: `forward` or `conditions`, used only in the message.
        enabled: The parsed allow-list, or `None` where the deployment named none — which means
            "whatever loaded" and cannot be wrong about a name.

    Returns:
        `kind/name` strings, one per name nothing is registered under.
    """
    if enabled is None:
        return []
    registered = {
        predictor.name for predictor in (list_forward() if kind == "forward" else list_conditions())
    }
    return [f"{kind}/{name}" for name in enabled - registered]
