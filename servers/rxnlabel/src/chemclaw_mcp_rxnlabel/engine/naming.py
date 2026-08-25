"""Which named reaction this is: Rxn-INSIGHT's 527 curated SMIRKS, where it is installed.

Rxn-INSIGHT (Dobbelaere et al., *J. Cheminform.* 2024; MIT) classifies a reaction into one of ten
classes and names it from 527 hand-curated SMIRKS, working from bond-electron matrices rather than
from a learned embedding. Reported >91% class and >95% name accuracy on 50,000 benchmark reactions,
at 40-100 ms each. It is the only open tool that does this at a granularity a chemist recognises —
"Heck terminal vinyl", not "class 3".

**Why a rule engine rather than a classifier.** The alternatives — rxnfp's BERT, SynCat's GNN — are
more accurate on benchmark splits and produce a class *index* that has to be mapped back to a name
through the label set they were trained on, which for the best available models is Pistachio's
NameRxn taxonomy. That mapping is the thing this system most needs to be able to argue about: a
name is quoted to a chemist and counted in a frequency table. A SMIRKS match can be shown; a
softmax cannot.

**Absence is reported, not hidden.** Without the extra, `name` returns nothing and
`engine/version.py` records that in `labeller_version` — so a corpus labelled without it re-labels
when a deployment installs it, rather than sitting there permanently unnamed under a version that
claims to have looked.

**No RXNO id is emitted.** Rxn-INSIGHT names reactions in its own vocabulary and does not carry the
ontology id, and mapping one to the other is a lookup table nobody here has audited — a wrong
`rxno_id` is worse than none, because the id is what a caller uses to escape the
three-vocabularies problem in the first place. A corpus that ships its own `rxno_id` (Pistachio
does) keeps it; a derived name does not invent one.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_NAMER: Any | None = None
_TRIED = False

# What Rxn-INSIGHT answers when no SMIRKS matched. Mapped to `None` rather than stored, because a
# frequency table with "OtherReaction" at the top is a table whose largest row means "we do not
# know" — which the coverage sentence already says, properly.
_UNNAMED = {"otherreaction", "other", "unknown", ""}


@dataclass(frozen=True)
class Naming:
    """One reaction's classification. Every field optional, because a miss is a real answer."""

    named_reaction: str | None = None
    reaction_class: str | None = None
    method: str | None = None


def available() -> bool:
    """Whether a namer could be constructed in this process."""
    return _namer() is not None


def name(reaction_smiles: str) -> Naming:
    """Classify one reaction, or answer that nothing matched.

    A raise from the namer is caught and reported as unnamed: Rxn-INSIGHT parses the reaction
    itself and throws on inputs it cannot read, and the correct response is one unnamed reaction
    rather than a failed batch of two hundred.
    """
    namer = _namer()
    if namer is None:
        return Naming()
    try:
        info = namer(reaction_smiles)
    except Exception:
        logger.warning("reaction naming failed for one reaction; it is recorded as unnamed")
        return Naming()
    named = _clean(info.get("NAME"))
    return Naming(
        named_reaction=named,
        reaction_class=_clean(info.get("CLASS")),
        # Only where something actually matched: `method` is what a chemist reads to tell "our
        # SMIRKS matched Buchwald-Hartwig" from "the corpus said so", and a method on a row with no
        # name would claim a derivation that did not happen.
        method="smirks" if named else None,
    )


def _clean(value: Any) -> str | None:
    """A non-empty, non-sentinel string, or `None`."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return None if stripped.lower() in _UNNAMED else stripped


def _namer() -> Any | None:
    """A callable `reaction_smiles -> dict`, built once, or `None` where the extra is absent.

    Wrapped in a closure rather than exposed as the library's own class because Rxn-INSIGHT's
    surface has moved between releases (`rxnpredict`'s adapter carries the same note): what is
    stable is that a `Reaction` exposes a dictionary of what it worked out. Pinning that one call
    here keeps the version drift in one function instead of in every caller.
    """
    global _NAMER, _TRIED
    with _LOCK:
        if _TRIED:
            return _NAMER
        _TRIED = True
        try:
            from rxn_insight.reaction import Reaction
        except ImportError:
            logger.info(
                "rxn-insight is not installed; reactions will be labelled without a name, and "
                "`labeller_version` records that so the rows re-label when it arrives"
            )
            return None

        def call(reaction_smiles: str) -> dict[str, Any]:
            info: dict[str, Any] = Reaction(reaction_smiles).get_reaction_info()
            return info

        _NAMER = call
        return _NAMER
