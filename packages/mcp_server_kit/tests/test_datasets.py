"""The dataset contract: every provenance field required, and the checksum actually verified.

A swapped corpus and a corpus without licence or provenance are loud errors at load time.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
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
    "refresh_owner": "team:test-owners",
    "refresh_cadence": "P12M",
}

RECORDS = "name,value\nalpha,1\nbeta,2\n"


def _write(
    directory: Path,
    *,
    records: str = RECORDS,
    replace: Mapping[str, object] | None = None,
    overrides: Mapping[str, object] | None = None,
) -> Path:
    """Write a dataset directory whose manifest is correct except for `overrides`.

    `replace` supplies the whole manifest instead, the only way to write one with a key absent. Both
    are mappings because call sites name the field by variable.
    """
    import hashlib

    (directory / "records.csv").write_text(records, encoding="utf-8")
    manifest = dict(MANIFEST if replace is None else replace)
    if "sha256" in manifest:
        manifest["sha256"] = hashlib.sha256(records.encode("utf-8")).hexdigest()
    manifest.update(overrides or {})
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

    Parametrized over `_REQUIRED` itself so a new field is covered the day it is added.
    """
    with pytest.raises(DatasetError, match="blank field"):
        load_dataset(_write(tmp_path, overrides={field: ""}))
    absent = {name: value for name, value in MANIFEST.items() if name != field}
    with pytest.raises(DatasetError, match="missing required field"):
        load_dataset(_write(tmp_path, replace=absent))


@pytest.mark.parametrize("field", datasets._REQUIRED)
def test_a_field_the_author_wrote_is_not_reported_as_missing(tmp_path: Path, field: str) -> None:
    """A field that is present but wrong is not reported as missing.

    Absent, wrong-type and blank are three distinct messages. The wrong-type arm pins refusal over
    coercion: a JSON number cannot hold `1.10` and a digest loses a leading zero, so provenance is
    taken verbatim or not at all.
    """
    with pytest.raises(DatasetError, match=f"non-string field.*{field}.*got int"):
        load_dataset(_write(tmp_path, overrides={field: 7}))
    with pytest.raises(DatasetError, match="blank field"):
        load_dataset(_write(tmp_path, overrides={field: "   "}))


def test_a_manifest_that_is_not_a_mapping_is_named(tmp_path: Path) -> None:
    """A `dataset.json` holding a JSON array gets a named error, not an `AttributeError`."""
    directory = _write(tmp_path)
    (directory / "dataset.json").write_text('["not", "a", "mapping"]', encoding="utf-8")
    with pytest.raises(DatasetError, match="must contain a JSON object"):
        load_dataset(directory)


def test_a_missing_dataset_is_named(tmp_path: Path) -> None:
    """The error says which path was expected, so a bad image COPY is diagnosable."""
    with pytest.raises(DatasetError, match="no dataset manifest"):
        load_dataset(tmp_path)


def test_a_manifest_with_a_null_tools_key_is_named(tmp_path: Path) -> None:
    """A manifest with `tools:` null is refused naming the field, not with a `TypeError`.

    It is a refusal rather than a "declares []" mismatch, because the consumer refuses a bare list
    key at startup.
    """
    from mcp_server_kit.testing import assert_manifest_matches

    manifest = tmp_path / "connector.yaml"
    manifest.write_text(
        "name: probe\ndescription: a probe\nendpoint:\n"
        "  transport: http\n"
        "  url: http://127.0.0.1:8850/mcp\n"
        "  auth:\n    mode: bearer\n    token_env: PROBE_TOKEN\n"
        "  tools:\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"not a connector manifest(.|\n)*tools"):
        assert_manifest_matches(manifest, ["a_tool"])


def test_a_misspelled_key_is_named_beside_the_field_it_makes_look_absent(tmp_path: Path) -> None:
    """A misspelled key is named beside the field it makes look absent.

    `"license"` must not produce only "licence missing": the message carries both the unrecognised
    key and the absent field, since either alone misleads.
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
    """A manifest key with no reader is refused rather than ignored.

    An ignored key looks to a reviewer as if something consumed it.
    """
    with pytest.raises(DatasetError, match="unrecognised key"):
        load_dataset(_write(tmp_path, overrides={"text_column": "name"}))


def test_no_shipped_manifest_carries_a_key_the_loader_does_not_read(tmp_path: Path) -> None:
    """Every `dataset.json` in this fleet loads against the model with no stray key."""
    root = Path(__file__).resolve().parents[3]
    manifests = sorted(root.glob("servers/*/src/*/data/**/dataset.json"))
    assert manifests, "no shipped dataset manifests found — the glob is wrong, not the fleet"
    for path in manifests:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        assert set(parsed) == set(datasets._REQUIRED), (
            f"{path} declares {sorted(set(parsed) - set(datasets._REQUIRED))} beyond the "
            "provenance fields; nothing reads them"
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("refresh_owner", "Jane Doe"),
        ("refresh_owner", "person:jdoe"),
        ("refresh_owner", "team:"),
        ("refresh_owner", "Team:Process-Safety"),
        ("refresh_cadence", "yearly"),
        ("refresh_cadence", "P0M"),
        ("refresh_cadence", "P2W"),
        ("refresh_cadence", "P1Y2M"),
        ("refresh_cadence", "12"),
    ],
)
def test_a_refresh_owner_or_cadence_nobody_can_parse_is_refused(
    tmp_path: Path, field: str, value: str
) -> None:
    """A refresh owner or cadence that does not parse is refused with its own message.

    The fix differs from "blank": the key is there with the wrong kind of value. A person's name is
    malformed on purpose, because it goes stale when they move.
    """
    with pytest.raises(DatasetError, match=rf"malformed field\(s\) {field}"):
        load_dataset(_write(tmp_path, overrides={field: value}))


@pytest.mark.parametrize("value", ["P1M", "P6M", "P12M", "P1Y", "P3Y"])
def test_a_whole_month_or_year_cadence_is_accepted(tmp_path: Path, value: str) -> None:
    """The positive half, so the pattern cannot tighten into refusing every shipped corpus."""
    assert load_dataset(_write(tmp_path, overrides={"refresh_cadence": value})).name


def test_every_shipped_corpus_names_a_refresh_owner_and_cadence() -> None:
    """Every shipped `dataset.json` loads its refresh fields through `DatasetManifest`."""
    root = Path(__file__).resolve().parents[3]
    manifests = sorted(root.glob("servers/*/src/*/data/**/dataset.json"))
    assert manifests, "no shipped dataset manifests found — the glob is wrong, not the fleet"
    for path in manifests:
        manifest = datasets.DatasetManifest.model_validate_json(path.read_text(encoding="utf-8"))
        assert datasets.REFRESH_OWNER.fullmatch(manifest.refresh_owner), path
        assert datasets.REFRESH_CADENCE.fullmatch(manifest.refresh_cadence), path
