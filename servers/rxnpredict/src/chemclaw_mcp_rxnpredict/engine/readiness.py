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
* **a predictor that is present and broke** — `failed`, or `egress_refused` from a loader reaching
  for a hub this fleet does not let it reach — is a broken image, and this pod is not;
* **a predictor this deployment *named* in an allow-list and does not have** is the loudest case of
  all, because somebody wrote the name down. Ready is not an option there whatever the cause: an
  operator asking for `reaction_t5_v2` and getting a server that will answer with nothing has a
  configuration that cannot work, and it is better found by a probe than by the first chemist.

`resource_exhausted` is deliberately not a reason to leave: every Deployment in this repository
points `readinessProbe` and `livenessProbe` at this same `/healthz`, so an unready answer restarts
the pod rather than shedding load, and a memory spike that restarted the pod would arrive back into
the same pressure. `degradation.PERMANENT_CAUSES` is what carries that rule for the whole fleet.

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
    """Refuse traffic for a broken predictor, or for one this deployment asked for and lacks.

    Raises:
        RuntimeError: a predictor is unavailable for a cause in `degradation.PERMANENT_CAUSES`, or
            an `ENABLED_*_MODELS` allow-list names a predictor that is not registered.
            `connector_app` turns that into a 503 naming the predictor and the reason.
    """
    broken = {
        name: entry
        for name, entry in unavailable().items()
        if entry.cause in degradation.PERMANENT_CAUSES
    }
    if broken:
        detail = "; ".join(
            f"{name} ({entry.cause}): {entry.reason}" for name, entry in sorted(broken.items())
        )
        raise RuntimeError(
            "a predictor this image carries could not be loaded, so the ensemble this pod would "
            f"answer with is smaller than the one it was built to be: {detail}"
        )
    settings = get_settings()
    missing = sorted(
        _named_but_absent("forward", settings.parse_enabled(settings.enabled_forward_models))
        + _named_but_absent(
            "conditions", settings.parse_enabled(settings.enabled_conditions_models)
        )
    )
    if missing:
        raise RuntimeError(
            "this deployment's enabled-model list names predictors that are not registered here, "
            f"so those calls cannot be answered: {', '.join(missing)}. Check "
            "CHEMCLAW_RXNPREDICT_ENABLED_FORWARD_MODELS / _CONDITIONS_MODELS against "
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
