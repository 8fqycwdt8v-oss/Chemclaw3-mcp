"""The dataset contract: every provenance field required, and the checksum actually verified.

A checksum nobody checks is a comment. These tests pin the two failure modes that matter — a
corpus swapped for a different file, and a corpus shipped without the licence or provenance a
reviewer needs — as loud errors at load time rather than wrong answers at call time.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp_server_kit import DatasetError, datasets, load_dataset, read_records

MANIFEST = {
    "name": "test-corpus",
    "version": "1.0.0",
    "licence": "CC0-1.0",
    "retrieved_from": "hand-authored in this test",
    "description": "two rows",
    "sha256": "",
}

RECORDS = "name,value\nalpha,1\nbeta,2\n"


def _write(directory: Path, *, records: str = RECORDS, **overrides: object) -> Path:
    """Write a dataset directory whose manifest is correct except for `overrides`."""
    import hashlib

    (directory / "records.csv").write_text(records, encoding="utf-8")
    manifest = dict(MANIFEST)
    manifest["sha256"] = hashlib.sha256(records.encode("utf-8")).hexdigest()
    manifest.update(overrides)
    (directory / "dataset.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory


def test_a_matching_dataset_loads(tmp_path: Path) -> None:
    """The happy path, and the citation string a tool returns beside its answer."""
    dataset = load_dataset(_write(tmp_path))
    assert dataset.name == "test-corpus"
    assert "CC0-1.0" in dataset.citation()
    assert read_records(dataset) == [
        {"name": "alpha", "value": "1"},
        {"name": "beta", "value": "2"},
    ]


def test_a_changed_file_is_refused(tmp_path: Path) -> None:
    """A truncated COPY or a swapped corpus fails at load, with both hashes in the message."""
    directory = _write(tmp_path)
    (directory / "records.csv").write_text("name,value\nalpha,999\n", encoding="utf-8")
    with pytest.raises(DatasetError, match="approved checksum"):
        load_dataset(directory)


@pytest.mark.parametrize("field", datasets._REQUIRED)
def test_every_provenance_field_is_required(tmp_path: Path, field: str) -> None:
    """A corpus with no recorded licence is a legal question nobody can answer later.

    Parametrized over `_REQUIRED` itself rather than over a hand-written copy of it. The copy was
    short by one — `description`, the least obviously load-bearing of the six — so it could have
    been dropped from the enforcement with the suite green, and a corpus could ship with no
    human-readable statement of what it is. A seventh field is now covered the day it is added.
    """
    with pytest.raises(DatasetError, match="missing required field"):
        load_dataset(_write(tmp_path, **{field: ""}))


def test_a_manifest_that_is_not_a_mapping_is_named(tmp_path: Path) -> None:
    """A `dataset.json` holding a JSON array raised `AttributeError: 'list' object has no get`.

    A diagnosable condition turned into an undiagnosable one: the same shape gets a named
    `ValueError` from `mcp_server_kit.testing.load_manifest`, and this is the one place a corpus's
    provenance is read.
    """
    directory = _write(tmp_path)
    (directory / "dataset.json").write_text('["not", "a", "mapping"]', encoding="utf-8")
    with pytest.raises(DatasetError, match="must contain a JSON object"):
        load_dataset(directory)


def test_a_missing_dataset_is_named(tmp_path: Path) -> None:
    """The error says which path was expected, so a bad image COPY is diagnosable."""
    with pytest.raises(DatasetError, match="no dataset manifest"):
        load_dataset(tmp_path)


def test_a_manifest_with_a_null_tools_key_is_named(tmp_path: Path) -> None:
    """`assert_manifest_matches` raised `TypeError: 'NoneType' object is not iterable` for `tools:`.

    Which defeats its own point: a manifest with a blank tool list should fail saying it declares
    nothing while the server serves something, not with a Python type error naming a line in the
    helper.
    """
    from mcp_server_kit.testing import assert_manifest_matches

    manifest = tmp_path / "connector.yaml"
    manifest.write_text(
        "name: probe\ndescription: a probe\nendpoint:\n"
        "  url: http://127.0.0.1:8850/mcp\n"
        "  auth:\n    mode: bearer\n    token_env: PROBE_TOKEN\n"
        "  tools:\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="declares \\[\\]"):
        assert_manifest_matches(manifest, ["a_tool"])


def test_a_misspelled_key_is_named_beside_the_field_it_makes_look_absent(tmp_path: Path) -> None:
    """The defect `extra="forbid"` exists for, on the one file whose purpose is to be auditable.

    Before the manifest was a model, a `dataset.json` written with `"license"` parsed clean and the
    loader then reported **`licence`** as missing — an error naming a field the author *had*
    written, under a spelling they had not. A reviewer reading that message looks at the file, sees
    a licence, and concludes the loader is broken.

    Both halves must be in the message, because either one alone sends the reader to the wrong
    conclusion: the unrecognised key says what was written, and the absent field says what it
    should have been written as.
    """
    manifest = dict(MANIFEST)
    manifest["license"] = manifest.pop("licence")
    directory = _write(tmp_path)
    manifest["sha256"] = json.loads((directory / "dataset.json").read_text())["sha256"]
    (directory / "dataset.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(DatasetError) as raised:
        load_dataset(directory)
    message = str(raised.value)
    assert "license" in message, "the key that was actually written is not in the message"
    assert "licence" in message, "the key it should have been is not in the message"


def test_a_key_nothing_reads_is_refused_rather_than_ignored(tmp_path: Path) -> None:
    """A manifest key with no reader is a claim, and this loader used to accept every one of them.

    Measured when the model was introduced: three shipped manifests — `chem`'s reagent table,
    `safety`'s copy of it and `props`' solvent sheet — carried `text_column` and `smiles_column`,
    read by nothing in either repository. They were invisible precisely because the loader took the
    six keys it knew and said nothing about the rest, so a reviewer seeing them had every reason to
    think something consumed them.
    """
    with pytest.raises(DatasetError, match="unrecognised key"):
        load_dataset(_write(tmp_path, text_column="name"))


def test_no_shipped_manifest_carries_a_key_the_loader_does_not_read(tmp_path: Path) -> None:
    """Every `dataset.json` in this fleet, against the model — the direction the unit tests cannot.

    `load_dataset` is called at import or at first use by each server, so a manifest with a stray
    key already fails that server's own suite. This says so in one place and in one line, which is
    what makes the deletion above a fleet fact rather than three servers that happened to be fixed.
    """
    root = Path(__file__).resolve().parents[3]
    manifests = sorted(root.glob("servers/*/src/*/data/**/dataset.json"))
    assert manifests, "no shipped dataset manifests found — the glob is wrong, not the fleet"
    for path in manifests:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        assert set(parsed) == set(datasets._REQUIRED), (
            f"{path} declares {sorted(set(parsed) - set(datasets._REQUIRED))} beyond the six "
            "provenance fields; nothing reads them"
        )
