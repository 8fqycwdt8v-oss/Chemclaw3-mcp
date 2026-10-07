"""Vendored datasets: installed at build time, checksummed, licensed, and never fetched.

Every `dataset.json` field is required: licence (a legal record), `sha256` (proof it is the
approved file), `retrieved_from` (provenance — never read as an address; the egress guard would
refuse), and `refresh_owner`/`refresh_cadence` (who re-checks the source, how often).

The checksum is verified on load, and no server loads a corpus at import, so a bad file is
`/healthz`'s 503 naming the two hashes rather than a crash loop. The manifest model forbids extra
keys, so a misspelt field is reported as such rather than as a missing one.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator
from pydantic_core import PydanticCustomError

__all__ = ["Dataset", "DatasetError", "DatasetManifest", "load_dataset", "read_records"]


class DatasetError(RuntimeError):
    """A vendored dataset is missing, malformed, or is not the file that was approved."""


#: A refresh owner is a team or a role, never a person: `team:<slug>` or `role:<slug>`. A person's
#: name goes stale the day they change jobs, and the corpus goes on looking owned.
REFRESH_OWNER = re.compile(r"(team|role):[a-z0-9][a-z0-9-]*")

#: A refresh cadence in whole months or years (`P6M`, `P1Y`): this fleet mirrors snapshots, and a
#: weekly-refreshed corpus would be a feed.
REFRESH_CADENCE = re.compile(r"P[1-9][0-9]*[MY]")


class DatasetManifest(BaseModel):
    """What a `dataset.json` must be: eight provenance strings and nothing else.

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
    refresh_owner: str
    refresh_cadence: str

    @field_validator("*")
    @classmethod
    def _is_not_blank(cls, value: str) -> str:
        """Reject a field that is present and empty, which is what a template leaves behind."""
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("refresh_owner")
    @classmethod
    def _names_a_team_or_role(cls, value: str) -> str:
        """`team:<slug>` or `role:<slug>`; a blank value is `_is_not_blank`'s to report."""
        if value.strip() and not REFRESH_OWNER.fullmatch(value):
            raise PydanticCustomError(
                "malformed", "must be team:<slug> or role:<slug>, lowercase, never a person"
            )
        return value

    @field_validator("refresh_cadence")
    @classmethod
    def _is_a_whole_month_duration(cls, value: str) -> str:
        """An ISO 8601 duration in months or years; a blank value is `_is_not_blank`'s."""
        if value.strip() and not REFRESH_CADENCE.fullmatch(value):
            raise PydanticCustomError(
                "malformed", "must be an ISO 8601 duration in whole months or years, e.g. P12M"
            )
        return value


#: The provenance fields, derived from the model so a new field is tested the day it is added.
_REQUIRED: tuple[str, ...] = tuple(DatasetManifest.model_fields)


def _explain(manifest_path: Path, error: ValidationError) -> str:
    """Turn a `ValidationError` into the sentence a reviewer of a `dataset.json` needs.

    A field the author wrote must never be reported as missing, so pydantic's error types map to
    distinct sentences: `missing` (absent), `value_error` (blank), `malformed` (wrong shape),
    any other type on a field (not a string — reported with the type found), and `extra_forbidden`
    (unknown key, reported alongside, since a misspelling yields two errors).

    Numbers are refused rather than coerced: JSON turns version `1.10` into `1.1` and strips a
    digest's leading zeros.
    """
    problems = error.errors()
    # An empty `loc` is an error about the whole document (not an object), with no field to name.
    if any(not item["loc"] for item in problems):
        found = type(json.loads(manifest_path.read_text(encoding="utf-8"))).__name__
        return f"{manifest_path} must contain a JSON object, got {found}"
    named: dict[str, list[str]] = {
        "missing": [],
        "blank": [],
        "malformed": [],
        "wrong_type": [],
        "extra": [],
    }
    for item in problems:
        field = str(item["loc"][-1])
        if item["type"] == "extra_forbidden":
            named["extra"].append(field)
        elif item["type"] == "missing":
            named["missing"].append(field)
        elif item["type"] == "value_error":
            named["blank"].append(field)
        elif item["type"] == "malformed":
            named["malformed"].append(f"{field} ({item['msg']}, got {item.get('input')!r})")
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
    if listed["malformed"]:
        parts.append(
            f"malformed field(s) {listed['malformed']}; a refresh owner or cadence nobody can "
            "parse is one nobody can hold a corpus to"
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
        """A one-line provenance string a tool returns beside its answer, so wording is shared."""
        return f"{self.name} v{self.version} ({self.licence}) — {self.retrieved_from}"


def _digest(path: Path) -> str:
    """The SHA-256 of `path`, read in blocks so a large corpus does not land in memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def load_dataset(directory: Path, *, records_file: str = "records.csv") -> Dataset:
    """Read `directory/dataset.json`, verify `records_file` against it, and return the manifest.

    Raises `DatasetError` when a file is missing, the manifest is invalid (see `_explain`), or the
    records file does not match its `sha256`.
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

    Untyped here: what the columns mean is the server's business.
    """
    with dataset.records_path.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]
