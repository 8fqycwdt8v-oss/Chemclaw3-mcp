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

from mcp_server_kit import degradation

logger = logging.getLogger(__name__)

SERVER = "rxnlabel"

# What this module is, as a metric label and in an answer — see `mapping.COMPONENT`.
COMPONENT = "reaction_namer"
degradation.register_components(COMPONENT)

_LOCK = threading.Lock()
_NAMER: Any | None = None
_TRIED = False
# The cause the last construction attempt failed with, read by `readiness._probe`. **Symmetric with
# `mapping` deliberately**: this module caught only `ImportError` around the import, so a
# distribution that is present and whose import raises anything else — a broken shared library, say
# — propagated out of `available()` into whatever happened to call it, counted nowhere, while the
# same failure one module over was classified and counted. Two components behind one probe cannot
# report differently about the same kind of fault.
_FAILURE: str | None = None

# What Rxn-INSIGHT answers when no SMIRKS matched. Mapped to `None` rather than stored, because a
# frequency table with "OtherReaction" at the top is a table whose largest row means "we do not
# know" — which the coverage sentence already says, properly.
_UNNAMED = {"otherreaction", "other", "unknown", ""}


@dataclass(frozen=True)
class Naming:
    """One reaction's classification. Every field optional, because a miss is a real answer.

    `failure` is what separates the two answers that used to be the same object. A namer that runs
    and matches nothing returns all-`None`, and so did a namer that raised — so "most of a patent
    corpus has no name", which is true, covered for a pod whose rule table would not load, which is
    a fault. A cause here means the classification is *missing*, not that nothing matched.
    """

    named_reaction: str | None = None
    reaction_class: str | None = None
    method: str | None = None
    failure: str | None = None


def available() -> bool:
    """Whether a namer could be constructed in this process."""
    return _namer() is not None


def construction_failure() -> str | None:
    """The `degradation` cause the last construction attempt failed with, or `None`."""
    return _FAILURE


def name(reaction_smiles: str) -> Naming:
    """Classify one reaction, or answer that nothing matched — or that the namer broke.

    A raise from the namer is still caught, and for the reason it always was: Rxn-INSIGHT parses
    the reaction itself and throws on inputs it cannot read, and the correct response is one
    unnamed reaction rather than a failed batch of two hundred. **What was wrong is that the
    answer was `Naming()` — byte-identical to the commonest correct answer this server gives.** A
    pod whose SMIRKS table had gone therefore reported "nothing matched" for every reaction in a
    corpus, stamped with the namer's own version, and nothing moved.

    So the cause is classified, counted on `chemclaw_mcp_degraded_total`, and carried back in
    `failure`. The log line names the cause and the exception, which the unconditional one-line
    warning it replaces did not.

    Args:
        reaction_smiles: `reactants>agents>products`.

    Returns:
        A `Naming`. `failure` is `None` on both normal paths — no namer installed, or a namer that
        matched nothing — and a degradation cause when it raised.
    """
    namer = _namer()
    if namer is None:
        return Naming()
    try:
        info = namer(reaction_smiles)
    except Exception as exc:
        cause = degradation.classify(exc)
        degradation.record(server=SERVER, component=COMPONENT, cause=cause)
        logger.warning(
            "reaction naming failed (%s); this reaction is recorded unnamed and stamped as "
            "degraded rather than as an unmatched rule: %r",
            cause,
            exc,
        )
        return Naming(failure=cause)
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
    global _NAMER, _TRIED, _FAILURE
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
        except Exception as exc:
            # Not reachable by an absent extra — that is the branch above — but by a distribution
            # that *is* installed and whose import raises: a broken shared library, a version of a
            # dependency it cannot use. `readiness` treats installed-and-unbuilt as a pod to
            # take out of rotation, and it can only do that if this is recorded rather than
            # propagated out of `available()` into whichever caller happened to ask first.
            _FAILURE = degradation.classify(exc)
            degradation.record(server=SERVER, component=COMPONENT, cause=_FAILURE)
            logger.exception("rxn-insight is installed but could not be imported (%s)", _FAILURE)
            return None

        def call(reaction_smiles: str) -> dict[str, Any]:
            info: dict[str, Any] = Reaction(reaction_smiles).get_reaction_info()
            return info

        _NAMER = call
        return _NAMER
