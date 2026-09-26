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
swapped in a rebuild fails with the two hashes in the message, rather than answering chemistry
questions from a file nobody approved. **Where that failure lands is the readiness probe**, because
no server in this fleet loads a corpus at import — `/healthz` answers 503 naming the file, which an
operator reads without a shell on the pod, instead of the process dying before it can serve the
route (`D-2026-09-18-a-corpus-that-cannot-be-read-is-a-probe-s-answer-not-an-import-error`).

**The manifest is a pydantic model with `extra="forbid"`, and the forbidding is the point.** This
package already ships pydantic, so the hand-rolled version of it — a `_REQUIRED` tuple, a `missing`
comprehension, an `isinstance` check and six `str(...)` coercions — was a model written twice. What
it could not do is notice a key it did not recognise, and the consequence was a message that
actively misled on the one file whose whole purpose is that a reviewer can audit it: a manifest
written with `"license"` parsed clean and then reported `licence` as *missing*, so the error named a
field the author had written rather than the spelling they had written it under. Forbidding extras
reports both halves. It also found three manifests carrying `text_column`/`smiles_column`, which
nothing in this repository reads: they belong to Chemclaw3's vendored-dataset schema
(`ingest/sources/vendored_dataset.py`, where `text_column` is required and both are read), and this
fleet's `load_dataset` does not share that schema.
"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

__all__ = ["Dataset", "DatasetError", "DatasetManifest", "load_dataset", "read_records"]


class DatasetError(RuntimeError):
    """A vendored dataset is missing, malformed, or is not the file that was approved."""


class DatasetManifest(BaseModel):
    """What a `dataset.json` must be: six provenance strings and nothing else.

    Every field is required and non-blank, and each for a reason that has already cost somebody
    something — the module docstring has them. `extra="forbid"` is the half a hand-rolled check
    cannot have: a required-field loop reads the keys it knows and is silent about the ones it does
    not, so a manifest with a misspelled key fails by naming the *correct* spelling as absent.

    `frozen=True` because a manifest is what a reviewer approved; nothing downstream may edit it
    after the checksum has been verified against it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    version: str
    licence: str
    retrieved_from: str
    description: str
    sha256: str

    @field_validator("*")
    @classmethod
    def _is_not_blank(cls, value: str) -> str:
        """Reject a field that is present and empty, which is what a template leaves behind.

        Presence was never the property worth checking. A `dataset.json` generated from a skeleton
        carries every key with an empty string, and a corpus with `"licence": ""` is exactly as
        unreviewable as one with no licence key at all.
        """
        if not value.strip():
            raise ValueError("must not be blank")
        return value


#: The provenance fields, derived from the model rather than restated beside it. Read by
#: `packages/mcp_server_kit/tests/test_datasets.py`, which parametrizes over it so that a seventh
#: field is covered by the enforcement test the day it is added — the reason it is derived and not
#: a tuple literal is that the literal it replaced was short by one.
_REQUIRED: tuple[str, ...] = tuple(DatasetManifest.model_fields)


def _explain(manifest_path: Path, error: ValidationError) -> str:
    """Turn a `ValidationError` into the sentence a reviewer of a `dataset.json` needs.

    **Four shapes, and the fourth is the one this function was written to prevent and then
    committed itself** (`D-2026-09-16-a-field-the-author-wrote-is-not-a-field-that-is-missing`).
    The module docstring above says the hand-rolled predecessor "actively misled on the one file
    whose whole purpose is that a reviewer can audit it" by naming as *missing* a field the author
    had written. This function bucketed every non-`extra_forbidden` error as absent, so
    `"version": 1` — a JSON number, on a line the author can see — was reported as
    `missing required field(s) version`. A `"licence": ""` a template left behind got the same
    sentence, and it is a third condition wanting a third fix.

    So the buckets are pydantic's own error types rather than "extra, and everything else":

    - `missing` — the key is not in the file.
    - `value_error` — the key is there and blank, which `DatasetManifest._is_not_blank` raises.
    - anything else about a named field — the key is there and is not a string. Reported with the
      type that was found, because "must be a string" without "got a number" sends a reviewer back
      to a line that looks correct to them.
    - `extra_forbidden` — a key nobody recognises, reported beside the others because one
      misspelling produces two errors and only both together say what happened.

    **Why a number is refused rather than coerced**, which the predecessor did with
    `str(manifest.get(field, ""))`. JSON has no way to hold `1.10` as a number: it parses to `1.1`,
    so a corpus at version 1.10 would be recorded as a different version than the one a reviewer
    approved, silently and in the field that exists to tell two builds apart. `sha256` is worse — a
    digest that happens to be all digits loses its leading zeros, and one long enough reaches
    scientific notation. This is the module's own "refuse rather than approximate" rule applied to
    provenance, and the cost is a startup failure with a sentence naming the line, which is the
    loud direction. Measured over the eight `dataset.json` files this fleet ships: every field of
    every one is already a JSON string, so nothing shipped changes behaviour.
    """
    problems = error.errors()
    # An empty `loc` is an error about the whole document rather than about a field — a JSON array
    # or a bare string where an object belongs. It is checked first because there are no field
    # names to report for it, and because it sends the reader to a different fix entirely.
    if any(not item["loc"] for item in problems):
        found = type(json.loads(manifest_path.read_text(encoding="utf-8"))).__name__
        return f"{manifest_path} must contain a JSON object, got {found}"
    named: dict[str, list[str]] = {"missing": [], "blank": [], "wrong_type": [], "extra": []}
    for item in problems:
        field = str(item["loc"][-1])
        if item["type"] == "extra_forbidden":
            named["extra"].append(field)
        elif item["type"] == "missing":
            named["missing"].append(field)
        elif item["type"] == "value_error":
            named["blank"].append(field)
        else:
            found = type(item.get("input")).__name__
            named["wrong_type"].append(f"{field} (got {found}, expected string)")
    listed = {kind: ", ".join(sorted(fields)) for kind, fields in named.items()}
    parts = []
    if listed["missing"]:
        parts.append(
            f"missing required field(s) {listed['missing']}; a dataset with no recorded licence "
            "or checksum is one nobody can review"
        )
    if listed["blank"]:
        parts.append(
            f"blank field(s) {listed['blank']}; a key a template left empty is exactly as "
            "unreviewable as one that is not there"
        )
    if listed["wrong_type"]:
        parts.append(
            f"non-string field(s) {listed['wrong_type']}; provenance is recorded verbatim rather "
            "than coerced, because JSON cannot hold version 1.10 or a digest with a leading zero"
        )
    if listed["extra"]:
        blamed = listed["missing"] or "a required field"
        parts.append(
            f"unrecognised key(s) {listed['extra']}; a key nothing reads is one a reviewer "
            f"believes is doing something, and a misspelled one is why {blamed} reads as absent"
        )
    return f"{manifest_path} is not a dataset manifest: {' — and '.join(parts)}"


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
        DatasetError: The manifest or records file is missing, the manifest is not a JSON object,
            a required field is absent, blank or not a string, a key is not one of the six, or the
            file on disk is not the one the manifest's `sha256` names. Those four field cases are
            reported as four different sentences, which is `_explain`'s whole subject: naming a
            field the author wrote as "missing" is the failure this contract exists to avoid.
    """
    manifest_path = directory / "dataset.json"
    records_path = directory / records_file
    if not manifest_path.is_file():
        raise DatasetError(f"no dataset manifest at {manifest_path}")
    if not records_path.is_file():
        raise DatasetError(f"no records file at {records_path}")
    parsed: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
    try:
        manifest = DatasetManifest.model_validate(parsed)
    except ValidationError as error:
        raise DatasetError(_explain(manifest_path, error)) from error
    found = _digest(records_path)
    if not _matches(found, manifest.sha256):
        raise DatasetError(
            f"{records_path} does not match the approved checksum: manifest says "
            f"{manifest.sha256}, file is {found}"
        )
    return Dataset(
        name=manifest.name,
        version=manifest.version,
        licence=manifest.licence,
        retrieved_from=manifest.retrieved_from,
        description=manifest.description,
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
