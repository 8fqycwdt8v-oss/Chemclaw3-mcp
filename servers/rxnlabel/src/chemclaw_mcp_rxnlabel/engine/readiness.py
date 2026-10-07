"""What this server has to have working before it may take traffic, and what it is allowed to lack.

RXNMapper and Rxn-INSIGHT are optional: a distribution that is not installed is a deployment's
decision and the pod is ready. One that *is* installed and will not construct, or that raises on the
fixture for a permanent cause, is a broken image and the pod is not ready, because its rows would be
stamped as if the component were absent and never re-label. The never-optional half —
canonicalisation, scaffold, functional groups and role rules — is checked by labelling a fixture
reaction end to end.

The verdict (ready or refused) is cached for `VERDICT_TTL_SECONDS`: expiring success catches a
component that breaks later, and caching a refusal bounds the fixture's forward pass to once a
minute.
"""

from __future__ import annotations

import threading
import time
from types import ModuleType

from mcp_server_kit import Dataset, degradation

from chemclaw_mcp_rxnlabel.engine import mapping, naming, roles, species, version

__all__ = ["VERDICT_TTL_SECONDS", "forget_verdict", "verify_labeller"]

# A small esterification in record form, one species per slot: exercises canonicalisation, role
# assignment and the functional groups. The path is checked, not the answer.
_PROBE_REACTION = "CC(=O)O.CCO>>CC(=O)OCC.O"
_PROBE_SPECIES = ["CC(=O)O", "CCO", "CC(=O)OCC"]

# How long a verdict — ready or refused — is believed: six probe intervals, so a broken component
# leaves rotation within this plus the Deployment's `failureThreshold x periodSeconds`.
VERDICT_TTL_SECONDS = 60.0


# Each optional component: its name in a refusal, the distribution whose presence says a deployment
# asked for it, and the module answering `available()` and `construction_failure()`. The module, not
# its functions, is bound so a test patching the module attribute reaches this loop.
_OPTIONAL: tuple[tuple[str, str, ModuleType], ...] = (
    ("atom mapper", "rxnmapper", mapping),
    ("reaction namer", "rxn-insight", naming),
)

_LOCK = threading.Lock()
# (expiry, verdict): the datasets verified, a refusal sentence, or the exception the labelling path
# raised. The exception is cached too, so deriving it does not cost a forward pass per probe.
_VERDICT: tuple[float, tuple[Dataset, ...] | str | BaseException] | None = None


def forget_verdict() -> None:
    """Drop the cached verdict, so the next call probes. For tests and for nothing else."""
    global _VERDICT
    with _LOCK:
        _VERDICT = None


def verify_labeller() -> tuple[Dataset, ...]:
    """Label a fixture reaction, and refuse if a component this image installed will not load.

    Returns:
        An empty tuple: this server vendors no corpus, so `/healthz` publishes `datasets: []`.

    Raises:
        RuntimeError: An installed component could not be constructed, or raised on the fixture for
            a permanent cause.
        Exception: Whatever the labelling path raised, re-raised unchanged so `connector_app` alone
            decides between a 503 and a 200 carrying `degraded`.
    """
    global _VERDICT
    with _LOCK:
        cached = _VERDICT
        if cached is None or time.monotonic() >= cached[0]:
            try:
                verdict: tuple[Dataset, ...] | str | BaseException = _probe()
            # BLE001: any failure is the probe's verdict; it is cached and re-raised below.
            except Exception as exc:  # noqa: BLE001
                verdict = exc
            _VERDICT = (time.monotonic() + VERDICT_TTL_SECONDS, verdict)
            cached = _VERDICT
    if isinstance(cached[1], BaseException):
        raise cached[1]
    if isinstance(cached[1], str):
        raise RuntimeError(cached[1])
    return cached[1]


def _probe() -> tuple[Dataset, ...] | str:
    """Run the labeller once. `()` for ready, or the sentence a 503 should carry.

    An installed component that failed to construct refuses whatever the cause: serving would stamp
    rows `<component>@absent`, indistinguishable from a deployment without it. A transient
    construction failure is retried by the component, so the pod becomes ready again without a
    restart. The labelling path's own raises are not caught here; `connector_app` classifies them.
    """
    for component, distribution, module in _OPTIONAL:
        if module.available() or version._installed(distribution) == "absent":
            continue
        cause = module.construction_failure()
        detail = module.construction_detail()
        return (
            f"the {component} is installed in this image ({distribution}) and could not be "
            f"constructed ({cause or 'cause not recorded'}"
            f"{f': {detail}' if detail else ''}), so every reaction would be "
            f"labelled as though no {component} existed — indistinguishable from a deployment "
            "that chose not to install one. Check the checkpoint mount and the container logs."
        )
    attempt = mapping.map_reaction(_PROBE_REACTION)
    permanent = _permanent("atom mapper", attempt.failure) or _permanent(
        "reaction namer", naming.name(_PROBE_REACTION).failure
    )
    if permanent:
        return permanent
    roles.assign(_PROBE_REACTION, _PROBE_SPECIES, attempt.mapped)
    for smiles in _PROBE_SPECIES:
        if species.canonical_smiles(smiles) is None:
            return (
                f"the labelling path could not canonicalise its own probe species ({smiles}); "
                "this pod cannot label anything"
            )
        species.functional_groups(smiles)
    return ()


def _permanent(component: str, cause: str | None) -> str | None:
    """The refusal a component's probe cause earns, or `None` where it earns none.

    Only `degradation.PERMANENT_CAUSES` refuse (an unparseable checkpoint, `EgressForbidden`); a
    transient cause such as memory pressure on the heavy fixture is counted on
    `chemclaw_mcp_degraded_total` by the component and leaves the pod in service.

    Args:
        component: What to name in the refusal.
        cause: The `mcp_server_kit.degradation` cause the component reported, or `None`.

    Returns:
        The sentence a 503 should carry, or `None`.
    """
    if cause not in degradation.PERMANENT_CAUSES:
        return None
    return (
        f"the {component} is installed in this image and raised ({cause}) on the readiness "
        "fixture, so every reaction would come back degraded. This pod must not take a batch "
        "of five hundred reactions to label; check the checkpoint mount and the container logs."
    )
