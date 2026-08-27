"""Vendored datasets: installed at build time, checksummed, licensed, and never fetched.

Chemclaw3 already decided this shape (`data/vendored/README.md`): a corpus arrives the way a
dependency arrives — pinned to a version, checksummed, labelled with its licence, and reviewed once
by a person in a pull request. This is the same contract, moved to where the tools now live.

Every field of `dataset.json` is required, and each for a reason that has already cost somebody
something: a corpus with no recorded licence is a legal question nobody can answer a year later,
one with no checksum cannot be shown to be what the review approved, and `retrieved_from` is the
only record of where a human obtained the file. **Nothing reads `retrieved_from` as an address and
nothing can fetch it** — the egress guard would refuse if it tried.

The checksum is verified on load, not on build. A dataset that was truncated by a bad COPY or
swapped in a rebuild fails at startup with the two hashes in the message, rather than answering
chemistry questions from a file nobody approved.
"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["Dataset", "DatasetError", "load_dataset", "read_records"]

_REQUIRED = ("name", "version", "licence", "retrieved_from", "description", "sha256")


class DatasetError(RuntimeError):
    """A vendored dataset is missing, malformed, or is not the file that was approved."""


@dataclass(frozen=True, slots=True)
class Dataset:
    """One vendored corpus and the provenance a reviewer signed off on."""

    name: str
    version: str
    licence: str
    retrieved_from: str
    description: str
    sha256: str
    records_path: Path

    def citation(self) -> str:
        """A one-line provenance string a tool can return beside its answer.

        Tools quote this rather than inventing their own wording, because a number without its
        source is what a chemist cannot put in a report.
        """
        return f"{self.name} v{self.version} ({self.licence}) — {self.retrieved_from}"


def _digest(path: Path) -> str:
    """The SHA-256 of `path`, read in blocks so a large corpus does not land in memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def load_dataset(directory: Path, *, records_file: str = "records.csv") -> Dataset:
    """Read `directory/dataset.json`, verify the records file against it, and return the manifest.

    Args:
        directory: The dataset directory — `dataset.json` plus the records file beside it.
        records_file: The data file the manifest describes. Named rather than assumed so a server
            whose corpus is not a CSV can still use this contract.

    Returns:
        The verified `Dataset`.

    Raises:
        DatasetError: The manifest or records file is missing, a required field is absent, or the
            file on disk is not the one the manifest's `sha256` names.
    """
    manifest_path = directory / "dataset.json"
    records_path = directory / records_file
    if not manifest_path.is_file():
        raise DatasetError(f"no dataset manifest at {manifest_path}")
    if not records_path.is_file():
        raise DatasetError(f"no records file at {records_path}")
    parsed: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise DatasetError(
            f"{manifest_path} must contain a JSON object, got {type(parsed).__name__}"
        )
    manifest: dict[str, Any] = parsed
    missing = [field for field in _REQUIRED if not str(manifest.get(field, "")).strip()]
    if missing:
        raise DatasetError(
            f"{manifest_path} is missing required field(s) {', '.join(missing)}; a dataset with no "
            "recorded licence or checksum is one nobody can review"
        )
    found = _digest(records_path)
    if not _matches(found, str(manifest["sha256"])):
        raise DatasetError(
            f"{records_path} does not match the approved checksum: manifest says "
            f"{manifest['sha256']}, file is {found}"
        )
    return Dataset(
        name=str(manifest["name"]),
        version=str(manifest["version"]),
        licence=str(manifest["licence"]),
        retrieved_from=str(manifest["retrieved_from"]),
        description=str(manifest["description"]),
        sha256=found,
        records_path=records_path,
    )


def _matches(found: str, declared: str) -> bool:
    """Whether the computed digest equals the declared one, ignoring case and a `sha256:` prefix."""
    return found.lower() == declared.lower().removeprefix("sha256:").strip()


def read_records(dataset: Dataset) -> list[dict[str, str]]:
    """Every row of the dataset's CSV, as dictionaries keyed by the header row.

    Deliberately untyped at this layer: what the columns *mean* is the server's business, and a
    loader that knew would have to be edited for every new corpus.
    """
    with dataset.records_path.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]
