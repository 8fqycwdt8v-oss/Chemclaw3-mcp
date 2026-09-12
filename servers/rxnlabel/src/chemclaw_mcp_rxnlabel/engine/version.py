"""The labeller version: what a stored label was produced by, so staleness is decidable.

The client never derives this and asks for it instead — `Chemclaw3`'s own client says so at length,
and the reason is that the version is half of what decides whether a stored row needs re-labelling.
A version this process cannot see the inputs of would be *well-formed* and match nothing: every row
would look stale forever, the drain would re-label the whole corpus on every pass, and nothing
would raise.

So this string names **every component whose output survives into a label, and nothing that does
not** — the rule `Chemclaw3`'s calculation key states as
`D-2026-08-01-a-key-names-what-ran`. Concretely:

* the server's own version, because the role rules and the functional-group vocabulary are in it;
* RDKit's, because canonicalisation, the SMARTS matching and the scaffolds are all its;
* the atom mapper's, **or `absent`** — a reaction labelled without a map has a coarser
  reactant-versus-reagent split, and that difference has to be visible or a deployment that installs
  the mapper would keep serving the coarse answers forever;
* the namer's, or `absent`, for the same reason.

The two `absent` cases are what make optional dependencies safe here rather than silently
degrading: the corpus repairs itself the moment they arrive.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from importlib import metadata

from chemclaw_mcp_rxnlabel.engine import mapping, naming

# The server's own version, which covers the parts of a label that are this repository's opinion:
# the role rules in `agents.py`/`roles.py` and the functional-group vocabulary in `species.py`.
# **Bump it when either changes**, including a renamed functional group — those names are stored in
# an array a query compares against by exact match, so a rename that did not bump this would leave
# a corpus half in one spelling and half in the other, answering neither.
# 2: the base rules gained bicarbonate (whose pattern matched nothing), the two-deprotonation
# phosphate and the aromatic-nitrogen bases, and every species is now parsed whole or not at all —
# all three change stored labels, which is what a bump is for.
SERVER_VERSION = "2"

_ABSENT = "absent"

# A component that was present, ran, and raised. **A third word rather than reuse of `absent`**, and
# the distinction is the whole reason this parameter exists: the rule this module opens with is that
# the string names every component whose output *survives into a label*, and a mapper that raised
# contributed nothing. Stamping such a row with the mapper's version made it indistinguishable from
# a correctly mapped one forever — no drain would ever re-derive it, because its stored version
# already equalled a healthy pod's. `failed` differs from both a version and from `absent`, so the
# row is stale against a healthy pod and legible to a chemist reading it directly.
_FAILED = "failed"


def labeller_version(failed: Collection[str] = ()) -> str:
    """The identity a label produced by this process is stamped with.

    Args:
        failed: Component keys — `atom_mapper`, `reaction_namer` — that ran and raised while
            producing *this* label. Empty for the deployment's own version, which is what
            `labeller_version` the tool answers and what a healthy row carries.
    """
    return ":".join(
        (
            f"rxnlabel@{SERVER_VERSION}",
            f"rdkit@{_installed('rdkit')}",
            f"mapper@{_component('atom_mapper', 'rxnmapper', mapping.available, failed)}",
            f"namer@{_component('reaction_namer', 'rxn-insight', naming.available, failed)}",
        )
    )


def components(failed: Collection[str] = ()) -> dict[str, str]:
    """The same facts, itemised — what an operator reads to see why a version changed.

    Args:
        failed: As `labeller_version`.
    """
    return {
        "server": SERVER_VERSION,
        "rdkit": _installed("rdkit"),
        "atom_mapper": _component("atom_mapper", "rxnmapper", mapping.available, failed),
        "reaction_namer": _component("reaction_namer", "rxn-insight", naming.available, failed),
    }


def _component(
    key: str,
    distribution: str,
    available: Callable[[], bool],
    failed: Collection[str],
) -> str:
    """One component's word: `failed`, `absent`, or the installed version, in that order.

    `failed` wins over a version because it is a statement about *this* label rather than about the
    image, and it wins over `absent` because the two are different facts an operator acts on
    differently — nothing to install versus something to replace.
    """
    if key in failed:
        return _FAILED
    return _installed(distribution) if available() else _ABSENT


def _installed(distribution: str) -> str:
    """A distribution's version, or `absent`.

    Note what this deliberately does *not* do: report a version for a package that is installed but
    could not be constructed. `mapping.available()` is what the caller checks first, and it answers
    "did a mapper actually build", because a broken image that carries the distribution and cannot
    load its weights produces unmapped labels — and stamping those with the mapper's version would
    make them indistinguishable from mapped ones forever.
    """
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return _ABSENT
