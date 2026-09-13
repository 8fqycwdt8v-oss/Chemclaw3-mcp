"""What this server has to have working before it may take traffic, and what it is allowed to lack.

`/healthz` was a constant `{"status": "ok"}` here, and this is the server where that was easiest to
justify and hardest to defend. Its two heavy components — RXNMapper and Rxn-INSIGHT — are optional
*by design*: without them a reaction is labelled without an atom map and without a name,
`engine/version.py` writes that absence into `labeller_version`, and a corpus labelled that way
re-labels itself the day a deployment installs them. "Optional" then got read as "nothing to be
unready about", so the server passed no `readiness=` at all. The consequence is not a missing
feature: a pod whose `rxnmapper` checkpoint failed to load passed its kubelet probe, took traffic,
and wrote coarse labels stamped `mapper@absent` — indistinguishable, forever, from the rows a
deployment that never installed a mapper produced on purpose.

**So the rule is about the distinction rather than about presence.** A distribution that is not
installed is a deployment's decision and this pod is ready. A distribution that *is* installed and
will not construct is a broken image, and this pod is not. `version._installed` already knows the
difference — it reports a version for a distribution the metadata carries — and `mapping.available`
and `naming.available` report whether the thing actually built. The two disagreeing is the fault.

**And what is optional is not the whole server.** The half that is never optional is the labelling
path itself: RDKit's canonicalisation, the scaffold, the functional-group vocabulary and the role
rules. A pod whose RDKit will not parse anything would otherwise be caught by nothing here, so the
probe labels a fixture reaction end to end rather than checking that modules import.

**The verdict is cached for a minute, and it used to be cached for the life of the process.**
`lru_cache` was the mechanism and its docstring argued the cache was safe because "`lru_cache` does
not cache exceptions, so a broken pod is re-probed and stays 503" — true of a pod broken at
*startup* and false of one that breaks later, which is the realistic shape: a weight file truncated
on a mount, a rule table on a share that was remounted. Driven on the real app through one client:
three probes 200, the namer then made to raise on every reaction, probe four **200**. A cache with
no
expiry cannot report a component that was working and stopped.

So the verdict — ready *or* the refusal — expires after `VERDICT_TTL_SECONDS`. Both halves matter.
Expiring the success is what catches the component that broke later. Caching the *refusal* is what
stops the one thing this probe does that is expensive: a full RXNMapper forward pass, off the
admission ceiling (`engine/admission.py` governs tool calls, not this), which on the failure path
ran
once per probe interval forever — `connector_app`'s own five-second memo is shorter than the
ten-second probe period, so it bounded a burst and not the steady state. One fixture pass per minute
instead, and a pod that breaks is out of rotation within the minute plus the
`failureThreshold x periodSeconds` the Deployment declares.
"""

from __future__ import annotations

import threading
import time
from types import ModuleType

from mcp_server_kit import Dataset, degradation

from chemclaw_mcp_rxnlabel.engine import mapping, naming, roles, species, version

__all__ = ["VERDICT_TTL_SECONDS", "forget_verdict", "verify_labeller"]

# An esterification written in the record form the tools take, with one species per slot: enough to
# drive canonicalisation, the role assignment and the functional-group vocabulary, and small enough
# that the probe costs nothing. What is checked is the *path*, not the answer.
_PROBE_REACTION = "CC(=O)O.CCO>>CC(=O)OCC.O"
_PROBE_SPECIES = ["CC(=O)O", "CCO", "CC(=O)OCC"]

# How long a verdict — ready or refused — is believed. Six readiness probe intervals, so a component
# that breaks mid-life is out of rotation within this plus the Deployment's
# `failureThreshold x periodSeconds` (30 s), and the fixture's forward pass is paid once a minute
# rather than once a probe. Not a setting: a deployment has no information this number depends on.
VERDICT_TTL_SECONDS = 60.0


# Each optional component: the name to use in a refusal, the distribution whose presence says a
# deployment asked for it, and the module that answers both `available()` and
# `construction_failure()` — the second being the cause `_mapper` classified, counted and then threw
# away, which is why a refusal could not say whether the pod needed replacing or a moment.
#
# **The module rather than its two functions**, and that is a correction rather than a style:
# binding `mapping.available` here captured the function object at import, so patching the module
# attribute in a test did not reach this loop at all — the two tests that do that were passing
# because the real predicate happened to answer the same way in a checkout with no extras
# installed. An attribute lookup per probe costs nothing and is what makes those tests about their
# subject.
_OPTIONAL: tuple[tuple[str, str, ModuleType], ...] = (
    ("atom mapper", "rxnmapper", mapping),
    ("reaction namer", "rxn-insight", naming),
)

_LOCK = threading.Lock()
# (expiry, what the probe concluded): the datasets verified, the sentence a refusal should carry, or
# the exception the labelling path raised. All three are cached, and the third is why: an exception
# that escaped this cache would be re-derived on every probe, and deriving it costs the forward pass
# this window exists to bound.
_VERDICT: tuple[float, tuple[Dataset, ...] | str | BaseException] | None = None


def forget_verdict() -> None:
    """Drop the cached verdict, so the next call probes. For tests and for nothing else."""
    global _VERDICT
    with _LOCK:
        _VERDICT = None


def verify_labeller() -> tuple[Dataset, ...]:
    """Label a fixture reaction, and refuse if a component this image installed will not load.

    Returns:
        An empty tuple. This server vendors no corpus — its inputs are the callers' reactions — so
        `/healthz` publishes `datasets: []`. The field being present is what says the check ran;
        which components this pod actually has is the `labeller_version` tool's answer, on the
        authenticated surface where a caller needs it to decide whether a stored label is stale.

    Raises:
        RuntimeError: a component's distribution is installed and could not be constructed, or a
            component raised on the fixture for a permanent cause.
        Exception: whatever the labelling path itself raised, re-raised unchanged so that
            `connector_app`'s funnel classifies it — which is what decides between a 503 and a 200
            carrying `degraded`. Classifying it *here* as well would be the same decision in two
            places, and the two would drift.
    """
    global _VERDICT
    with _LOCK:
        cached = _VERDICT
        if cached is None or time.monotonic() >= cached[0]:
            try:
                verdict: tuple[Dataset, ...] | str | BaseException = _probe()
            except Exception as exc:
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

    **The construction branch refuses whatever the cause, and that is a correction to the brief this
    was written against.** The obvious reading of `PERMANENT_CAUSES` says a mapper that failed to
    construct on a `MemoryError` is transient and the pod should stay in service, counted. Driven,
    that is the *worse* answer: with the mapper installed and unbuilt, `version._component` reads
    `available()` False and stamps the row `mapper@absent` — byte-identical to a deployment that
    never installed one, which is precisely the defect
    `D-2026-09-12-a-degradation-that-is-not-counted-is-a-degradation-nobody-sees` exists to end. A
    pod in that state writes permanently indistinguishable rows for as long as it serves. So it
    sheds traffic, and what made that verdict dangerous — a 503 on this route being a *kill* —
    is gone rather than worked around: see
    `D-2026-09-13-a-probe-that-can-kill-the-pod-is-not-a-readiness-probe`.

    What the cause is used for instead is whether the refusal can ever be lifted.
    `mapping._mapper` now retries a transient construction failure, so this branch is a pod
    that is *not ready yet* rather than a pod that is stuck — measured before that change, `_TRIED`
    latched on the first attempt and the process never looked again, so 503 here was terminal and
    only a restart could clear it.

    **The labelling path's own raises are not caught here at all**, and that is the point rather
    than an omission. `connector_app`'s `/healthz` classifies whatever a readiness callable raises
    and decides between a 503 and a 200 carrying `degraded` — so catching, classifying and counting
    the same exception here would be that decision written twice, in two places that drift. What
    this module owns is the *cause*-shaped arms above, where there is no exception to classify: a
    `MapResult.failure` and a `Naming.failure` are already causes, recorded by the module that
    produced them. `verify_labeller` caches the exception so the fixture's forward pass is still
    paid once per `VERDICT_TTL_SECONDS` rather than once per probe.
    """
    for component, distribution, module in _OPTIONAL:
        if module.available() or version._installed(distribution) == "absent":
            continue
        cause = module.construction_failure()
        return (
            f"the {component} is installed in this image ({distribution}) and could not be "
            f"constructed ({cause or 'cause not recorded'}), so every reaction would be "
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

    **Construction is not the only way a component breaks, and checking only construction left the
    larger half open.** The loop above catches a distribution that will not build; measured against
    a namer that imported cleanly and raised on every reaction, `/healthz` answered **200** and the
    server labelled a corpus "nothing matched" under a version string carrying the namer's own
    number. Running the component on the fixture is what closes that, and it now costs one forward
    pass per `VERDICT_TTL_SECONDS` rather than one per process — the earlier `lru_cache` closed it
    exactly once, which this function's docstring used to claim was closing it at all.

    **The transient exclusion is deliberate and is the risk this function has to get right.** A
    transformer that runs out of memory on a busy minute is not a pod to replace, and this probe's
    fixture is heavier than most of the traffic it gates, so a signal that flipped on it would
    shed a
    pod that could still serve. `degradation.PERMANENT_CAUSES` is therefore what this acts on: a
    checkpoint that will not parse and an `EgressForbidden` from a loader reaching for the hub are
    properties of the image and will not improve. The transient case is not ignored — `mapping` and
    `naming` counted it on `chemclaw_mcp_degraded_total` before returning, and that counter is where
    a pod thrashing on memory shows up without anything being taken out of rotation for it.

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
