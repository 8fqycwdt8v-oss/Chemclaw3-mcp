"""Atom-atom mapping, when a mapper is installed — and a truthful answer when one is not.

RXNMapper (Schwaller et al., *Science Advances* 2021; MIT) is an ALBERT transformer, so it needs
torch; the image installs it via the `models` extra. Without it `contributing_reactants` returns
`None` ("no evidence") and roles fall back to the written slot. `engine/version.py` puts the
mapper's presence into `labeller_version`, so rows labelled without it go stale once it is
installed. Loaded lazily and once, so a cold start does not fail its readiness probe.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from mcp_server_kit import degradation
from mcp_server_kit.limits import atom_count_error, echo
from rdkit import Chem

from chemclaw_mcp_rxnlabel.engine import construction

logger = logging.getLogger(__name__)

SERVER = "rxnlabel"

# This component's name as a metric label and in an answer; one constant keeps the label set closed.
COMPONENT = "atom_mapper"
degradation.register_components(COMPONENT)

# How long a transient construction failure stands before the mapper is built again; matches
# `readiness.VERDICT_TTL_SECONDS`, so at most one retry per probe.
CONSTRUCTION_RETRY_SECONDS = construction.RETRY_SECONDS

_LOCK = threading.Lock()
_MAPPER: Any | None = None
_TRIED = False
# When the last construction attempt ran and what it failed with, so readiness can tell a corrupt
# checkpoint from a transient failure and a transient one is retried.
_ATTEMPTED_AT: float | None = None
_FAILURE: str | None = None
# The exception behind `_FAILURE`, bounded for quoting, so a refusal can say what broke.
_FAILURE_DETAIL: str | None = None

# The module whose absence is a deployment's decision; any other missing module is a broken image.
_OPTIONAL_MODULES = ("rxnmapper",)


@dataclass(frozen=True)
class MapResult:
    """The atom map, and whether the mapper *ran and failed* — which `None` alone cannot say.

    A failed mapper must be distinguishable from an absent one, or a broken pod's rows carry a
    version claiming the mapper contributed and never re-label.

    Attributes:
        mapped: The atom-mapped reaction, or `None` where there is none.
        failure: `None` when nothing went wrong, otherwise the `mcp_server_kit.degradation` cause; a
        cause means the answer is degraded, not merely mapless.
    """

    mapped: str | None = None
    failure: str | None = None


def available() -> bool:
    """Whether a mapper could be constructed in this process."""
    return _mapper() is not None


def construction_failure() -> str | None:
    """The `degradation` cause the last construction attempt failed with, or `None`.

    Read by `readiness._probe` to tell a pod that needs replacing from one that needs a moment.
    """
    return _FAILURE


def construction_detail() -> str | None:
    """The exception the last failed construction raised, bounded for quoting, or `None`."""
    return _FAILURE_DETAIL


def map_reaction(reaction_smiles: str) -> MapResult:
    """The atom-mapped form of `reaction_smiles`, and whether the mapper failed producing it.

    Every exception is caught so one untokenisable reaction does not fail a batch, but the cause is
    classified, counted on `chemclaw_mcp_degraded_total`, logged with its `repr` and returned.

    Args:
        reaction_smiles: `reactants>agents>products`.

    Returns:
        A `MapResult` whose `failure` is `None` when there is no mapper or nothing to say, and a
        degradation cause when the mapper raised.
    """
    mapper = _mapper()
    if mapper is None:
        return MapResult()
    try:
        results = mapper.get_attention_guided_atom_maps([reaction_smiles], canonicalize_rxns=False)
    # BLE001: blind on purpose and classified on the next line.
    except Exception as exc:  # noqa: BLE001
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

    Read from torch (`torch.get_num_threads()`), so the charge follows whatever the deployment pins
    (the image sets `OMP_NUM_THREADS=1`). `1` with no mapper, since the RDKit path holds the GIL,
    and `1` when torch cannot be asked, since a zero cost would leave the tool uncounted.
    """
    if _mapper() is None:
        return 1
    try:
        import torch
    # BLE001: an import guard whose only answer is a conservative cost of 1.
    except Exception:  # noqa: BLE001  # pragma: no cover - the mapper loaded, so torch is installed
        return 1
    return max(1, int(torch.get_num_threads()))


def contributing_reactants(mapped: str | None) -> set[str] | None:
    """The reactants that put at least one atom into a product, as canonical SMILES.

    The reactant-versus-reagent split (Schneider et al., JCIM 2016), computed from the map. Returns
    `None`, not an empty set, when there is no map: an empty set would demote every substrate to a
    reagent. Takes the mapped reaction so the transformer runs once per reaction.

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
            # Oversize components are dropped: `MolToSmiles` can overflow the C stack (SIGSEGV).
            continue
        if _labels(token) & product_labels:
            # Canonicalised without the map, the form every other module compares against.
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
    """The process-wide mapper, or `None` where the extra is absent or would not build.

    Built at most once on success. A transient failure is retried after `CONSTRUCTION_RETRY_SECONDS`
    so the pod can recover without a restart; a permanent cause latches, since re-parsing a corrupt
    checkpoint learns nothing.
    """
    global _MAPPER, _TRIED, _ATTEMPTED_AT, _FAILURE, _FAILURE_DETAIL
    with _LOCK:
        if _MAPPER is not None or (_TRIED and not _retry_due()):
            return _MAPPER
        _TRIED = True
        _ATTEMPTED_AT = time.monotonic()
        try:
            from rxnmapper import RXNMapper

            _MAPPER = RXNMapper()
        except Exception as exc:
            # Only a `ModuleNotFoundError` naming `rxnmapper` itself is an absent extra; a missing
            # dependency of an installed mapper is a broken image.
            if degradation.is_not_installed(exc, _OPTIONAL_MODULES):
                logger.info(
                    "rxnmapper is not installed; reactions will be labelled without an atom map, "
                    "and `labeller_version` records that so the rows re-label when it arrives"
                )
                return None
            # Weights load from the installed package, so a failure here is a broken image (or an
            # `EgressForbidden` from a loader reaching for the hub). The server still starts and
            # assigns roles; readiness refuses, and the counter makes the cause visible from a
            # scrape.
            _FAILURE = degradation.classify(exc, optional=_OPTIONAL_MODULES)
            _FAILURE_DETAIL = echo(repr(exc))
            degradation.record(server=SERVER, component=COMPONENT, cause=_FAILURE)
            logger.exception(
                "rxnmapper is installed but could not be imported or constructed (%s); it will be "
                "retried in %.0fs if that cause is transient",
                _FAILURE,
                CONSTRUCTION_RETRY_SECONDS,
            )
            return None
        _FAILURE = None
        _FAILURE_DETAIL = None
        return _MAPPER


def _retry_due() -> bool:
    """Whether a failed construction may be attempted again. Called under `_LOCK`.

    Wraps `construction.retry_due` so the window stays a module attribute a test can shorten.
    """
    return construction.retry_due(
        failure=_FAILURE, attempted_at=_ATTEMPTED_AT, window_seconds=CONSTRUCTION_RETRY_SECONDS
    )
