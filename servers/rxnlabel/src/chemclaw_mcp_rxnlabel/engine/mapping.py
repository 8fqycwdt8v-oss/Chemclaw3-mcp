"""Atom-atom mapping, when a mapper is installed — and a truthful answer when one is not.

RXNMapper (Schwaller et al., *Science Advances* 2021; MIT) is an ALBERT transformer trained without
supervision on patent reactions, and it maps 99.4% of a 49,000-reaction unbalanced USPTO test set
correctly. It is also a transformer, so it drags torch behind it: the image installs it and the
`models` extra carries it, and a developer's checkout does not.

**Absence degrades, it does not fail.** Without the mapper `contributing_reactants` returns `None`,
which the role assignment reads as "no evidence either way" and falls back to the slot the species
was written in. What makes that safe rather than silent is that `engine/version.py` puts the
mapper's presence into `labeller_version`: rows labelled without it carry a different version and go
stale the moment a deployment installs it, so the corpus repairs itself instead of quietly holding
two qualities of answer under one label.

Loaded lazily and once. The model is ~50 MB of weights and several seconds to construct, and a
server that paid that at import would fail its readiness probe on a cold start.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

from mcp_server_kit import degradation
from mcp_server_kit.limits import atom_count_error
from rdkit import Chem

logger = logging.getLogger(__name__)

SERVER = "rxnlabel"

# What this module is, as a metric label and in an answer. A module constant rather than a string
# spelled at each call site, because the two would drift and the label rule in
# `mcp_server_kit/metrics.py` only holds while the set of them is closed.
COMPONENT = "atom_mapper"

_LOCK = threading.Lock()
_MAPPER: Any | None = None
_TRIED = False


@dataclass(frozen=True)
class MapResult:
    """The atom map, and whether the mapper *ran and failed* — which `None` alone cannot say.

    This type exists because `map_reaction` used to answer `None` to three different questions and
    the caller could not tell them apart: no mapper installed, a reaction RDKit could not read, and
    a mapper that raised. The first is a deployment's decision and the last is a broken pod, and
    they reached a chemist as the same unmapped reaction stamped with the same `labeller_version` —
    so a corpus labelled by a pod whose checkpoint had gone would never re-label, because the stamp
    claimed the mapper had contributed.

    Attributes:
        mapped: The atom-mapped reaction, or `None` where there is none.
        failure: `None` when nothing went wrong, otherwise the `mcp_server_kit.degradation` cause.
            A cause here means the answer is *degraded* rather than merely mapless.
    """

    mapped: str | None = None
    failure: str | None = None


def available() -> bool:
    """Whether a mapper could be constructed in this process."""
    return _mapper() is not None


def map_reaction(reaction_smiles: str) -> MapResult:
    """The atom-mapped form of `reaction_smiles`, and whether the mapper failed producing it.

    Catching every exception is still right, and for the reason it always was: RXNMapper raises on
    inputs it cannot tokenise (an over-long reaction, an element outside its vocabulary) and the
    correct response is an unmapped reaction rather than a failed batch of two hundred. **What was
    wrong is that the answer did not say so.** A torch OOM, a checkpoint that will not parse and an
    `EgressForbidden` from a loader reaching for weights all became a plain `None` — the same value
    a deployment with no mapper installed produces on purpose — with one unconditional WARNING that
    named neither the reaction nor the fault, and no counter anywhere.

    So the cause is classified, counted on `chemclaw_mcp_degraded_total` and returned. The log line
    carries the exception's `repr` rather than only a sentence, because "this reaction could not be
    mapped" is what a malformed input and a broken pod both look like from a log with nothing in it.

    Args:
        reaction_smiles: `reactants>agents>products`.

    Returns:
        A `MapResult` whose `failure` is `None` on the two normal paths — no mapper, or a reaction
        the mapper had nothing to say about — and a degradation cause when the mapper raised.
    """
    mapper = _mapper()
    if mapper is None:
        return MapResult()
    try:
        results = mapper.get_attention_guided_atom_maps([reaction_smiles], canonicalize_rxns=False)
    except Exception as exc:
        cause = degradation.classify(exc)
        degradation.record(server=SERVER, component=COMPONENT, cause=cause)
        logger.warning(
            "atom mapping failed (%s); this reaction is labelled without a map and stamped as "
            "degraded: %r",
            cause,
            exc,
        )
        return MapResult(failure=cause)
    if not results:
        return MapResult()
    mapped = results[0].get("mapped_rxn")
    return MapResult(mapped=str(mapped) if mapped else None)


def inference_threads() -> int:
    """Cores one mapped batch may spend, which is what the admission gate charges it.

    **Read from torch rather than assumed**, for the reason `engine/admission.py` gives at length:
    torch's intra-op width is sized from the machine's physical cores and not from the container's
    cgroup, so a pod limited to two cores on a large node gives one forward pass a thread count
    nobody chose — and no image in this fleet pins `OMP_NUM_THREADS` for it. Charging the ceiling
    what the process is *actually* configured to spend is the only honest number available, and it
    follows a deployment that does pin the variable without this function knowing that it did.

    `1` with no mapper installed, which is the measured truth of the RDKit-only path: SMARTS
    matching holds the GIL, and 1, 2 and 4 threads labelling 50 reactions each measured 581, 425
    and 418 reactions/s — one core's worth at every width. `1` also when torch is present but
    cannot be asked, because a cost of zero would make the tool uncounted.
    """
    if _mapper() is None:
        return 1
    try:
        import torch
    except Exception:  # pragma: no cover - the mapper loaded, so torch is installed
        return 1
    return max(1, int(torch.get_num_threads()))


def contributing_reactants(mapped: str | None) -> set[str] | None:
    """The reactants that put at least one atom into a product, as canonical SMILES.

    This is the reactant-versus-reagent split — the classical one from Schneider, Lowe, Sayle and
    Landrum's "What's What" (JCIM 2016), computed here from the map rather than from their
    heuristics because a map is available and is strictly better evidence. A species written on the
    left that contributes no atoms to the right did not become the product: it is a base, an
    oxidant, a coupling agent.

    Returns `None` — not an empty set — when no mapper is installed. The distinction matters: an
    empty set means "nothing contributed", which would demote every substrate to a reagent.

    **Takes the mapped reaction rather than mapping it**, because the caller needs that string too
    and a forward pass is the cost the batch bound is set against: mapping here as well ran the
    transformer twice per reaction — 1000 passes for a 500-reaction batch — for two identical
    results.

    Args:
        mapped: The atom-mapped reaction from `map_reaction`, or `None` where there was no map.
    """
    if mapped is None:
        return None
    parts = mapped.split(">")
    if len(parts) != 3:
        return None
    product_labels = _labels(parts[2])
    if not product_labels:
        return None
    contributing = set()
    for token in parts[0].split("."):
        mol = Chem.MolFromSmiles(token)
        if mol is None or atom_count_error(mol.GetNumAtoms()) is not None:
            # An oversize component is dropped rather than canonicalised: `MolToSmiles` on a large
            # linear molecule overflows the C stack (an uncatchable SIGSEGV). See `mcp_server_kit`.
            continue
        if _labels(token) & product_labels:
            # Re-canonicalised *without* the map, because that is the form every other module in
            # this server compares against.
            for atom in mol.GetAtoms():
                atom.SetAtomMapNum(0)
            contributing.add(Chem.MolToSmiles(mol))
    return contributing


def _labels(smiles: str) -> set[int]:
    """The atom-map numbers present in a (possibly multi-component) SMILES."""
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return set()
    return {atom.GetAtomMapNum() for atom in mol.GetAtoms() if atom.GetAtomMapNum()}


def _mapper() -> Any | None:
    """The process-wide mapper, constructed once, or `None` where the extra is not installed."""
    global _MAPPER, _TRIED
    with _LOCK:
        if _TRIED:
            return _MAPPER
        _TRIED = True
        try:
            from rxnmapper import RXNMapper
        except ImportError:
            logger.info(
                "rxnmapper is not installed; reactions will be labelled without an atom map, and "
                "`labeller_version` records that so the rows re-label when it arrives"
            )
            return None
        try:
            _MAPPER = RXNMapper()
        except Exception as exc:
            # Constructing it downloads or loads weights. In this fleet the image bakes them at
            # build time, so a failure here means a broken image rather than a missing network —
            # and the server must still start and still assign roles.
            #
            # **Counted as well as logged**, and this branch is why the counter takes a cause: a
            # deployment whose weights are absent from the image raises here, and one whose loader
            # reached for the hub raises `EgressForbidden` here. `readiness.verify_labeller` already
            # refuses to take traffic in both cases; the counter is what makes the difference
            # visible from a scrape rather than from a pod's first log lines.
            degradation.record(server=SERVER, component=COMPONENT, cause=degradation.classify(exc))
            logger.exception("rxnmapper is installed but could not be constructed")
            return None
        return _MAPPER
