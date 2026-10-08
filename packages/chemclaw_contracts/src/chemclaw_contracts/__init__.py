"""The contracts the Chemclaw3 MCP fleet serves, in one installable package.

Owns every `connector.yaml` as package data (`manifests/` for the connectors a consumer mounts,
`manifests_internal/` for the backends it must not) and the typed wire of the two backends
(`chemclaw_contracts.calc`, `chemclaw_contracts.rxnlabel`). `servers/<name>/connector.yaml` and the
repository's `manifests/` are symlinks to these files, never copies.

Invariants: a manifest's `contract_version` is a semver string or absent; `manifests_dir()` holds
only connectors a consumer may mount; nothing here touches the network.
"""

from __future__ import annotations

import re
from functools import cache
from importlib.resources import as_file, files
from pathlib import Path

import yaml

# Equal to `version` in pyproject.toml; `tests/test_manifests.py` holds it.
__version__ = "1.0.0"

__all__ = [
    "CONTRACT_VERSION_PATTERN",
    "__version__",
    "contract_version",
    "declared_contract_version",
    "internal_manifests_dir",
    "manifest_names",
    "manifest_path",
    "manifests_dir",
]

#: The shape a manifest's `contract_version` must have (`MAJOR.MINOR.PATCH`, digits only).
CONTRACT_VERSION_PATTERN = r"^\d+\.\d+\.\d+$"

_MANIFEST_FILE = "connector.yaml"


def _package_dir(name: str) -> Path:
    """A directory inside this package, as a filesystem path.

    `as_file` yields the real directory for an installed package; the package is never zipped, so
    the path outlives the call.
    """
    with as_file(files(__name__)) as root:
        return root / name


def manifests_dir() -> Path:
    """The directory of connector manifests a consumer may put on `CHEMCLAW_CONNECTORS_DIR`.

    One subdirectory per connector, each holding `connector.yaml`. Backends are not here.
    """
    return _package_dir("manifests")


def internal_manifests_dir() -> Path:
    """The backends' manifests (`mount: backend`); mounting this directory is a startup error."""
    return _package_dir("manifests_internal")


def manifest_names(*, internal: bool = False) -> tuple[str, ...]:
    """The sorted names of the connectors (default) or of the backends (`internal=True`)."""
    root = internal_manifests_dir() if internal else manifests_dir()
    return tuple(sorted(path.parent.name for path in root.glob(f"*/{_MANIFEST_FILE}")))


def manifest_path(name: str) -> Path:
    """The `connector.yaml` of the server called `name`, connector or backend.

    Raises:
        KeyError: no server of that name is served.
    """
    for root in (manifests_dir(), internal_manifests_dir()):
        candidate = root / name / _MANIFEST_FILE
        if candidate.is_file():
            return candidate
    served = [*manifest_names(), *manifest_names(internal=True)]
    raise KeyError(f"no manifest of that name; this package serves {sorted(served)}")


def declared_contract_version(path: Path) -> str | None:
    """The `contract_version` the manifest file at `path` declares, or `None` where it has none.

    Raises:
        ValueError: the file is not a mapping, or its version is not `MAJOR.MINOR.PATCH`.
    """
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError(f"{path} is not a mapping")
    declared = parsed.get("contract_version")
    if declared is None:
        return None
    if not isinstance(declared, str) or re.fullmatch(CONTRACT_VERSION_PATTERN, declared) is None:
        raise ValueError(f"{path}: contract_version {declared!r} is not MAJOR.MINOR.PATCH")
    return declared


@cache
def contract_version(name: str) -> str | None:
    """The `contract_version` the manifest of the server `name` declares, or `None`.

    Raises:
        KeyError: no server of that name is served.
        ValueError: see `declared_contract_version`.
    """
    return declared_contract_version(manifest_path(name))
