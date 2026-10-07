"""Which named reaction this is: Rxn-INSIGHT's 527 curated SMIRKS, where it is installed.

Rxn-INSIGHT (Dobbelaere et al., *J. Cheminform.* 2024; MIT) names reactions at a granularity a
chemist recognises ("Heck terminal vinyl"). A rule engine rather than a learned classifier, because
a SMIRKS match can be shown to the chemist who reads the name. Without the extra, `name` returns
nothing and `engine/version.py` records that in `labeller_version`, so the corpus re-labels once it
is installed. No RXNO id is emitted: no audited mapping exists and a wrong id is worse than none.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from mcp_server_kit import degradation
from mcp_server_kit.limits import echo

from chemclaw_mcp_rxnlabel.engine import construction

logger = logging.getLogger(__name__)

SERVER = "rxnlabel"

# What this module is, as a metric label and in an answer — see `mapping.COMPONENT`.
COMPONENT = "reaction_namer"
degradation.register_components(COMPONENT)

# : How long a transient construction failure waits before it is tried again; aliased from
# `engine/construction.py` so both components share one window.
CONSTRUCTION_RETRY_SECONDS = construction.RETRY_SECONDS

_LOCK = threading.Lock()
_NAMER: Any | None = None
_TRIED = False
# : When the last construction attempt ran. A transient failure is retried; a permanent cause
# latches; an absent extra is not a failure.
_ATTEMPTED_AT: float | None = None
# The cause the last construction attempt failed with, read by `readiness._probe`. Classified the
# same way as `mapping`'s so both components behind one probe report a fault alike.
_FAILURE: str | None = None
# The exception behind `_FAILURE`, bounded for quoting — see `mapping._FAILURE_DETAIL`.
_FAILURE_DETAIL: str | None = None

# The top-level module whose absence is a deployment's decision; see `mapping._OPTIONAL_MODULES`.
_OPTIONAL_MODULES = ("rxn_insight",)

# What Rxn-INSIGHT answers when no SMIRKS matched; mapped to `None` so "unknown" never tops a
# frequency table.
_UNNAMED = {"otherreaction", "other", "unknown", ""}


@dataclass(frozen=True)
class Naming:
    """One reaction's classification. Every field optional, because a miss is a real answer.

    `failure` separates a namer that matched nothing (all `None`) from one that raised; a cause
    means the classification is missing.
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


def construction_detail() -> str | None:
    """The exception the last failed construction raised, bounded for quoting, or `None`."""
    return _FAILURE_DETAIL


def name(reaction_smiles: str) -> Naming:
    """Classify one reaction, or answer that nothing matched — or that the namer broke.

    A raise is caught so one unreadable reaction does not fail a batch, but its cause is classified,
    counted on `chemclaw_mcp_degraded_total`, logged and returned in `failure`, so a broken rule
    table is not mistaken for "nothing matched".

    Args:
        reaction_smiles: `reactants>agents>products`.

    Returns:
        A `Naming`. `failure` is `None` when no namer is installed or nothing matched, and a
        degradation cause when it raised.
    """
    namer = _namer()
    if namer is None:
        return Naming()
    try:
        info = namer(reaction_smiles)
    # BLE001: blind on purpose and classified on the next line.
    except Exception as exc:  # noqa: BLE001
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
        # Only where something matched: a method on an unnamed row would claim a derivation that did
        # not happen.
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

    A closure confines Rxn-INSIGHT's release-to-release API drift to this one function.
    """
    global _NAMER, _TRIED, _ATTEMPTED_AT, _FAILURE, _FAILURE_DETAIL
    with _LOCK:
        if _NAMER is not None or (_TRIED and not _retry_due()):
            return _NAMER
        _TRIED = True
        _ATTEMPTED_AT = time.monotonic()
        try:
            from rxn_insight.reaction import Reaction
        except Exception as exc:
            if degradation.is_not_installed(exc, _OPTIONAL_MODULES):
                logger.info(
                    "rxn-insight is not installed; reactions will be labelled without a name, and "
                    "`labeller_version` records that so the rows re-label when it arrives"
                )
                # Not a retry: `_FAILURE` stays `None`, so an absent distribution is never
                # re-imported.
                return None
            # An installed distribution whose import raises (broken shared library, missing
            # dependency) is recorded, so readiness can take the pod out of rotation.
            _FAILURE = degradation.classify(exc, optional=_OPTIONAL_MODULES)
            _FAILURE_DETAIL = echo(repr(exc))
            degradation.record(server=SERVER, component=COMPONENT, cause=_FAILURE)
            logger.exception(
                "rxn-insight is installed but could not be imported (%s); it will be retried in "
                "%.0fs if that cause is transient",
                _FAILURE,
                CONSTRUCTION_RETRY_SECONDS,
            )
            return None

        def call(reaction_smiles: str) -> dict[str, Any]:
            info: dict[str, Any] = Reaction(reaction_smiles).get_reaction_info()
            return info

        _NAMER = call
        _FAILURE = None
        _FAILURE_DETAIL = None
        return _NAMER


def _retry_due() -> bool:
    """Whether a failed construction may be attempted again. Called under `_LOCK`."""
    return construction.retry_due(
        failure=_FAILURE, attempted_at=_ATTEMPTED_AT, window_seconds=CONSTRUCTION_RETRY_SECONDS
    )
