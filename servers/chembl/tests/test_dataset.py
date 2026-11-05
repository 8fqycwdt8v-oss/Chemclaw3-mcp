"""The shipped subset validated against itself, and the loader's refusals of a file that is not it.

The subset is a ChEMBL adaptation, so its checks are pairs of independently produced facts that must
agree: the InChIKey ChEMBL recorded against the one RDKit derives from ChEMBL's own SMILES, a
summary's median against its own minimum and maximum, a parent against the compound it names. A
transposed row fails here rather than answering a question.
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
from chemclaw_mcp_chembl.engine import store
from chemclaw_mcp_chembl.engine.chem import inchi_key
from chemclaw_mcp_chembl.engine.store import DB_FILE, SCHEMA_VERSION, StoreError
from mcp_server_kit import DatasetError, load_dataset
from rdkit import Chem


@pytest.fixture
def shipped() -> Iterator[sqlite3.Connection]:
    """A read-only connection to the subset the package ships."""
    with store.connect() as connection:
        yield connection


def test_the_subset_is_the_file_the_manifest_approved() -> None:
    """Checksum, schema version and release agree between the file and its manifest."""
    found = store.dataset()
    recorded = store.meta()
    assert found.name == "chembl-subset"
    assert recorded["schema_version"] == SCHEMA_VERSION
    assert found.version == f"{recorded['release']}-{recorded['tier']}"
    assert recorded["tier"] in {"fixture", "full"}


def test_the_manifest_names_the_licence_the_adaptation_and_the_release() -> None:
    """What a reviewer of `dataset.json` needs: share-alike, adapted, which release, from where."""
    found = store.dataset()
    release = store.meta()["release"]
    assert "CC-BY-SA-3.0" in found.licence
    assert "adaptation" in found.licence and "ChEMBL ids" in found.licence
    assert f"release {release}" in found.licence
    assert found.retrieved_from.startswith("https://ftp.ebi.ac.uk/pub/databases/chembl/")
    assert f"chembl_{release}_sqlite.tar.gz" in found.retrieved_from
    assert "SHA-256" in found.retrieved_from


def test_a_tampered_file_is_refused_with_both_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One flipped byte is a 503 naming the approved and the found checksum, not a wrong answer."""
    copy = tmp_path / "data"
    shutil.copytree(store.data_dir(), copy)
    target = copy / DB_FILE
    content = bytearray(target.read_bytes())
    content[-1] ^= 0xFF
    target.write_bytes(content)
    monkeypatch.setattr(store, "data_dir", lambda: copy)
    with pytest.raises(DatasetError, match="does not match the approved checksum"):
        store.dataset()


def _rewritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, statement: str) -> None:
    """Point the store at a copy whose `meta` `statement` changed, under a matching manifest."""
    import hashlib
    import json

    copy = tmp_path / "data"
    shutil.copytree(store.data_dir(), copy)
    with closing(sqlite3.connect(copy / DB_FILE)) as connection:
        connection.execute(statement)
        connection.commit()
    manifest = json.loads((copy / "dataset.json").read_text(encoding="utf-8"))
    manifest["sha256"] = hashlib.sha256((copy / DB_FILE).read_bytes()).hexdigest()
    (copy / "dataset.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(store, "data_dir", lambda: copy)


def test_a_subset_of_another_schema_version_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _rewritten(tmp_path, monkeypatch, "UPDATE meta SET value = '0' WHERE key = 'schema_version'")
    with pytest.raises(StoreError, match="schema_version"):
        store.dataset()


def test_a_subset_that_cannot_carry_its_attribution_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without its source URL an answer could not name where it came from: the file is unusable."""
    _rewritten(tmp_path, monkeypatch, "DELETE FROM meta WHERE key = 'source_url'")
    with pytest.raises(StoreError, match="source_url"):
        store.dataset()


def test_the_manifest_loads_without_the_store(shipped: sqlite3.Connection) -> None:
    """The fleet's loader accepts it as it does every other corpus (`DatasetManifest`)."""
    assert load_dataset(store.data_dir(), records_file=DB_FILE).sha256


def test_chembl_inchi_keys_agree_with_the_ones_rdkit_derives_from_chembl_smiles(
    shipped: sqlite3.Connection,
) -> None:
    """Two independent derivations of one identifier, over every compound that has a structure."""
    checked = 0
    for row in shipped.execute(
        "SELECT chembl_id, canonical_smiles, inchi_key FROM compound WHERE inchi_key IS NOT NULL"
    ):
        mol = Chem.MolFromSmiles(row["canonical_smiles"])
        assert mol is not None, f"{row['chembl_id']}: RDKit cannot read ChEMBL's own SMILES"
        assert inchi_key(mol) == row["inchi_key"], row["chembl_id"]
        checked += 1
    assert checked >= 10


def test_every_reference_resolves_and_every_skeleton_is_its_key_prefix(
    shipped: sqlite3.Connection,
) -> None:
    ids = {r[0] for r in shipped.execute("SELECT chembl_id FROM compound")}
    for chembl_id, parent in shipped.execute("SELECT chembl_id, parent_chembl_id FROM compound"):
        assert parent is None or parent in ids, f"{chembl_id}: parent {parent} is not in the subset"
    for key, skeleton in shipped.execute("SELECT inchi_key, skeleton FROM compound"):
        assert (key or "")[:14] == (skeleton or "")
    targets = {r[0] for r in shipped.execute("SELECT chembl_id FROM target")}
    for compound, target in shipped.execute(
        "SELECT compound_chembl_id, target_chembl_id FROM activity_summary"
    ):
        assert compound in ids and target in targets
    for (chembl_id,) in shipped.execute("SELECT chembl_id FROM name"):
        assert chembl_id in ids


def test_every_summary_is_consistent_with_itself(shipped: sqlite3.Connection) -> None:
    for row in shipped.execute("SELECT * FROM activity_summary"):
        where = (row["compound_chembl_id"], row["target_chembl_id"], row["standard_type"])
        assert row["n_exact"] >= 0 and row["n_censored"] >= 0 and row["n_assays"] >= 1, where
        assert row["n_exact"] + row["n_censored"] >= 1, where
        if row["n_exact"]:
            assert row["minimum"] <= row["median"] <= row["maximum"], where
        else:
            assert row["median"] is None and row["minimum"] is None, where
        if row["median_pchembl"] is not None:
            assert 0 < row["median_pchembl"] < 15, where
        low, high = row["assay_confidence_min"], row["assay_confidence_max"]
        assert (low is None) == (high is None), where
        if low is not None:
            assert 0 <= low <= high <= 9, where


def test_a_name_is_stored_folded_and_findable(shipped: sqlite3.Connection) -> None:
    for name, norm in shipped.execute("SELECT name, name_norm FROM name"):
        assert norm == " ".join(name.casefold().split())
