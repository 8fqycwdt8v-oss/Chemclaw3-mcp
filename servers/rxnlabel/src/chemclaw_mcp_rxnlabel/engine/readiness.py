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

Cached, because both halves are startup properties of an image: the components are constructed once
and memoised behind `mapping`/`naming`'s own `_TRIED` flags, and re-running the fixture on a
ten-second probe interval would buy nothing. `lru_cache` does not cache exceptions, so a broken pod
is re-probed and stays 503.
"""

from __future__ import annotations

from functools import lru_cache

from mcp_server_kit import Dataset

from chemclaw_mcp_rxnlabel.engine import mapping, naming, roles, species, version

__all__ = ["verify_labeller"]

# An esterification written in the record form the tools take, with one species per slot: enough to
# drive canonicalisation, the role assignment and the functional-group vocabulary, and small enough
# that the probe costs nothing. What is checked is the *path*, not the answer.
_PROBE_REACTION = "CC(=O)O.CCO>>CC(=O)OCC.O"
_PROBE_SPECIES = ["CC(=O)O", "CCO", "CC(=O)OCC"]

# Each optional component, as the pair that has to agree: the distribution whose presence says a
# deployment asked for it, and the predicate that says it actually built.
_OPTIONAL = (
    ("atom mapper", "rxnmapper", mapping.available),
    ("reaction namer", "rxn-insight", naming.available),
)


@lru_cache(maxsize=1)
def verify_labeller() -> tuple[Dataset, ...]:
    """Label a fixture reaction, and refuse if a component this image installed will not load.

    Returns:
        An empty tuple. This server vendors no corpus — its inputs are the callers' reactions — so
        `/healthz` publishes `datasets: []`. The field being present is what says the check ran;
        which components this pod actually has is the `labeller_version` tool's answer, on the
        authenticated surface where a caller needs it to decide whether a stored label is stale.

    Raises:
        RuntimeError: a component's distribution is installed and could not be constructed.
        Exception: the labelling path itself failed. Either way `connector_app` turns it into a 503
            naming the reason, which is the point: a pod that cannot label must not be sent a
            batch of five hundred reactions to label.
    """
    for component, distribution, built in _OPTIONAL:
        if built():
            continue
        if version._installed(distribution) != "absent":
            raise RuntimeError(
                f"the {component} is installed in this image ({distribution}) and could not be "
                "constructed, so every reaction would be labelled as though no "
                f"{component} existed — indistinguishable from a deployment that chose not to "
                "install one. Check the checkpoint mount and the container logs."
            )
    mapped = mapping.map_reaction(_PROBE_REACTION)
    roles.assign(_PROBE_REACTION, _PROBE_SPECIES, mapped)
    for smiles in _PROBE_SPECIES:
        if species.canonical_smiles(smiles) is None:
            raise RuntimeError(
                f"the labelling path could not canonicalise its own probe species ({smiles}); "
                "this pod cannot label anything"
            )
        species.functional_groups(smiles)
    return ()
