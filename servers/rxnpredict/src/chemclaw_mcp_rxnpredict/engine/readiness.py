"""What this server has to have before it may take traffic, and what it is allowed to lack.

Every predictor is optional, so readiness is about capability, not the ensemble's size:

* a predictor whose optional dependency is absent (`not_installed`) is a deployment's decision —
  every dev checkout looks like this — and the pod is ready;
* a kind of prediction (forward or conditions) with no predictor left, where a permanent cause took
  the last one, is a broken image and the pod is not ready;
* a predictor named in an `ENABLED_*_MODELS` allow-list but not registered is never ready, whatever
  the cause, because somebody configured something that cannot work.

One broken predictor among several optional ones is not a reason to leave: a restart cannot fix a
missing checkpoint, and refusing would turn a thinner ensemble into an outage. That loss is counted
on `chemclaw_mcp_degraded_total` and named by `list_available_models`. Transient causes never refuse
(`degradation.PERMANENT_CAUSES`). Not cached: it only reads two dictionaries filled at import.
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
        RuntimeError: No predictor of a kind is registered and a permanent cause took the last one,
            or an allow-list names an unregistered predictor; `connector_app` answers 503 with it.
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
            # Nothing of this kind loaded and nothing broke: no extras installed, as in every dev
            # checkout.
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
        enabled: The parsed allow-list, or `None` where the deployment named none.

    Returns:
        `kind/name` strings, one per name nothing is registered under.
    """
    if enabled is None:
        return []
    registered = {
        predictor.name for predictor in (list_forward() if kind == "forward" else list_conditions())
    }
    return [f"{kind}/{name}" for name in enabled - registered]
