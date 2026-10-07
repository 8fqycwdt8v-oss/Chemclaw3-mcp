"""The labeller version: what a stored label was produced by, so staleness is decidable.

The client asks for this rather than deriving it. It names every component whose output survives
into a label and nothing else:

* the server's own version (role rules and functional-group vocabulary);
* RDKit's (canonicalisation, SMARTS, scaffolds);
* the atom mapper's, or `absent` — a label without a map is coarser;
* the namer's, or `absent`.

The `absent` cases make optional dependencies safe: rows go stale and re-label once a component is
installed.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from importlib import metadata

from chemclaw_mcp_rxnlabel.engine import mapping, naming

# The server's own version: the role rules (`agents.py`/`roles.py`) and functional-group vocabulary
# (`species.py`). Bump it whenever either changes, including a renamed group, since stored names are
# matched exactly.
SERVER_VERSION = "2"

_ABSENT = "absent"

# A component that was present, ran, and raised. Distinct from a version and from `absent`, so the
# row is stale against a healthy pod and re-labels.
_FAILED = "failed"


def labeller_version(failed: Collection[str] = ()) -> str:
    """The identity a label produced by this process is stamped with.

    Args:
        failed: Component keys (`atom_mapper`, `reaction_namer`) that raised while producing this
            label. Empty for the deployment's own version.
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

    `failed` describes this label rather than the image, so it wins; it differs from `absent`
    because one means replace and the other means install.
    """
    if key in failed:
        return _FAILED
    return _installed(distribution) if available() else _ABSENT


def _installed(distribution: str) -> str:
    """A distribution's version, or `absent`.

    Says nothing about whether the component built; callers check `available()` first so unbuilt
    components are not stamped as present.
    """
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return _ABSENT
