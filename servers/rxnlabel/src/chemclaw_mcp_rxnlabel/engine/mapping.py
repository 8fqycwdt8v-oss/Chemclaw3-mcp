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
from typing import Any

from rdkit import Chem

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_MAPPER: Any | None = None
_TRIED = False


def available() -> bool:
    """Whether a mapper could be constructed in this process."""
    return _mapper() is not None


def map_reaction(reaction_smiles: str) -> str | None:
    """The atom-mapped form of `reaction_smiles`, or `None` if it could not be mapped.

    `None` covers three different things on purpose — no mapper installed, a reaction RDKit could
    not read, a mapper that raised on this input — because the caller does the same thing with all
    three: records no mapping and moves on. Which it was is in the log and in the version string.
    """
    mapper = _mapper()
    if mapper is None:
        return None
    try:
        results = mapper.get_attention_guided_atom_maps([reaction_smiles], canonicalize_rxns=False)
    except Exception:
        # Every exception, and this is the one place in this server where that is right: RXNMapper
        # raises on inputs it cannot tokenise (an over-long reaction, an element outside its
        # vocabulary) and the correct response to all of them is an unmapped reaction rather than a
        # failed batch of two hundred.
        logger.warning("atom mapping failed for a reaction; it will be labelled without a map")
        return None
    if not results:
        return None
    mapped = results[0].get("mapped_rxn")
    return str(mapped) if mapped else None


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
        if mol is None:
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
        except Exception:
            # Constructing it downloads or loads weights. In this fleet the image bakes them at
            # build time, so a failure here means a broken image rather than a missing network —
            # and the server must still start and still assign roles.
            logger.exception("rxnmapper is installed but could not be constructed")
            return None
        return _MAPPER
