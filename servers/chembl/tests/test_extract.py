"""The extraction script, run against a miniature release in ChEMBL's own layout.

`scripts/chembl_extract_subset.py` is the only code that turns ChEMBL into this server's data, and
it runs once, at image build, where nobody is watching. So its rules are proved here on rows built
to
break each one: the aggregation, the exclusions, the parent closure, the determinism of the output,
and the refusal of an archive that is not the pinned one.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp_server_kit import load_dataset

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "chembl_extract_subset.py"


@pytest.fixture(scope="module")
def extract() -> Any:
    """The script as a module (it is not on any import path, deliberately)."""
    spec = importlib.util.spec_from_file_location("chembl_extract_subset", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _built(
    extract: Any, release_db: Path, out_dir: Path, ids: tuple[str, ...]
) -> sqlite3.Connection:
    extract.FIXTURE_IDS = ids
    extract.build_subset(release_db, out_dir, "fixture", "unverified (test)")
    connection = sqlite3.connect(out_dir / extract.DB_FILE)
    connection.row_factory = sqlite3.Row
    return connection


def test_a_summary_counts_what_it_aggregates_and_excludes_what_chembl_doubts(
    extract: Any, release_db: Path, tmp_path: Path
) -> None:
    """Exact values give the statistics, censored ones are counted, suspect ones are absent."""
    db = _built(extract, release_db, tmp_path / "out", ("CHEMBL1",))
    row = db.execute(
        "SELECT * FROM activity_summary WHERE compound_chembl_id = 'CHEMBL1' "
        "AND target_chembl_id = 'CHEMBL203' AND standard_type = 'IC50' AND standard_units = 'nM'"
    ).fetchone()
    # 10, 20, 30 are exact; the flagged duplicate and the 'Outside typical range' row are out.
    assert (row["n_exact"], row["n_censored"], row["n_assays"]) == (3, 1, 2)
    assert (row["median"], row["minimum"], row["maximum"]) == (20.0, 10.0, 30.0)
    assert row["median_pchembl"] == pytest.approx(7.85)
    assert (row["assay_confidence_min"], row["assay_confidence_max"]) == (8, 9)


def test_units_are_never_merged_and_a_target_keeps_its_own_rows(
    extract: Any, release_db: Path, tmp_path: Path
) -> None:
    db = _built(extract, release_db, tmp_path / "out", ("CHEMBL1",))
    keys = {
        (r["target_chembl_id"], r["standard_type"], r["standard_units"])
        for r in db.execute("SELECT * FROM activity_summary")
    }
    assert keys == {
        ("CHEMBL203", "IC50", "nM"),
        ("CHEMBL203", "IC50", "ug.mL-1"),
        ("CHEMBL203", "Ki", "nM"),
        ("CHEMBL204", "IC50", "nM"),
    }


def test_a_group_whose_relation_is_unknown_has_no_summary(
    extract: Any, release_db: Path, tmp_path: Path
) -> None:
    """A value with no relation is neither exact nor a bound, so it supports no statement at all."""
    db = _built(extract, release_db, tmp_path / "out", ("CHEMBL1",))
    assert (
        db.execute("SELECT COUNT(*) FROM activity_summary WHERE standard_type = 'Kd'").fetchone()[0]
        == 0
    )


def test_only_targets_with_a_summary_are_kept(
    extract: Any, release_db: Path, tmp_path: Path
) -> None:
    db = _built(extract, release_db, tmp_path / "out", ("CHEMBL1",))
    assert [r["chembl_id"] for r in db.execute("SELECT chembl_id FROM target ORDER BY 1")] == [
        "CHEMBL203",
        "CHEMBL204",
    ]


def test_a_salt_form_brings_its_parent_and_a_censored_only_group_has_no_statistics(
    extract: Any, release_db: Path, tmp_path: Path
) -> None:
    db = _built(extract, release_db, tmp_path / "out", ("CHEMBL3",))
    compounds = db.execute("SELECT chembl_id, parent_chembl_id FROM compound ORDER BY 1").fetchall()
    assert [(r[0], r[1]) for r in compounds] == [("CHEMBL2", None), ("CHEMBL3", "CHEMBL2")]
    row = db.execute(
        "SELECT * FROM activity_summary WHERE compound_chembl_id = 'CHEMBL3'"
    ).fetchone()
    assert (row["n_exact"], row["n_censored"]) == (0, 1)
    assert row["median"] is None and row["median_pchembl"] is None


def test_names_are_folded_and_a_synonym_equal_to_the_preferred_name_is_listed_once(
    extract: Any, release_db: Path, tmp_path: Path
) -> None:
    db = _built(extract, release_db, tmp_path / "out", ("CHEMBL1", "CHEMBL3"))
    names = db.execute(
        "SELECT name, name_norm, is_pref FROM name WHERE chembl_id = 'CHEMBL1' ORDER BY name"
    ).fetchall()
    assert [(r[0], r[2]) for r in names] == [("ETHANOL", 1), ("Ethyl Alcohol", 0), ("alcohol", 0)]
    assert db.execute("SELECT name_norm FROM name WHERE chembl_id = 'CHEMBL3'").fetchone()[0] == (
        "sodium acetate"
    )


def test_a_fixture_id_missing_from_the_release_refuses_the_build(
    extract: Any, release_db: Path, tmp_path: Path
) -> None:
    """A fixture that silently lost a compound would pass every test written against the rest."""
    extract.FIXTURE_IDS = ("CHEMBL1", "CHEMBL987654321")
    with pytest.raises(SystemExit, match="CHEMBL987654321"):
        extract.build_subset(release_db, tmp_path / "out", "fixture", "unverified (test)")


def test_the_output_is_deterministic_and_the_manifest_passes_the_loader(
    extract: Any, release_db: Path, tmp_path: Path
) -> None:
    """Same release, same script, same bytes; and `dataset.json` is what `load_dataset` accepts."""
    extract.FIXTURE_IDS = ("CHEMBL1", "CHEMBL3")
    shas = []
    for name in ("a", "b"):
        extract.build_subset(release_db, tmp_path / name, "fixture", "unverified (test)")
        shas.append(hashlib.sha256((tmp_path / name / extract.DB_FILE).read_bytes()).hexdigest())
    assert shas[0] == shas[1]
    dataset = load_dataset(tmp_path / "a", records_file=extract.DB_FILE)
    assert dataset.version == f"{extract.RELEASE}-fixture"
    assert "CC-BY-SA-3.0" in dataset.licence and "adaptation" in dataset.licence


def _fake_download(tmp_path: Path, extract: Any, listed: str, content: bytes) -> None:
    """Replace the network with an archive and a `checksums.txt` that names `listed`."""

    def fetch(url: str, target: Path) -> None:
        if url.endswith("checksums.txt"):
            target.write_text(f"SHA256\tfile\n\n{listed}\t{extract.ARCHIVE}\n", encoding="utf-8")
        else:
            target.write_bytes(content)

    extract.fetch = fetch


def test_the_archive_is_refused_unless_both_digests_agree(
    extract: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pinned digest, the published one and the downloaded file must be the same string."""
    content = b"not a ChEMBL archive"
    digest = hashlib.sha256(content).hexdigest()
    monkeypatch.setattr(extract, "fetch", extract.fetch)
    monkeypatch.setattr(extract, "SQLITE_SHA256", digest)

    # All three agree: accepted.
    _fake_download(tmp_path, extract, digest, content)
    assert extract.verified_archive(tmp_path / "ok").name == extract.ARCHIVE

    # EMBL-EBI lists another digest than the file has: refused.
    _fake_download(tmp_path, extract, "0" * 64, content)
    with pytest.raises(SystemExit, match="not the approved file"):
        extract.verified_archive(tmp_path / "published-differs")

    # The file and the published digest agree with each other but not with the pin: refused, so a
    # release EMBL-EBI re-issued is a reviewed edit and not a silent follow.
    monkeypatch.setattr(extract, "SQLITE_SHA256", "f" * 64)
    _fake_download(tmp_path, extract, digest, content)
    with pytest.raises(SystemExit, match="not the approved file"):
        extract.verified_archive(tmp_path / "pin-differs")


class _Response:
    """A stand-in for `urlopen`'s response: a status, and a body that may die part-way."""

    def __init__(self, body: bytes, status: int, dies_after: int | None = None) -> None:
        self.status = status
        self._body = body
        self._dies_after = dies_after
        self._sent = 0

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        if self._dies_after is not None and self._sent >= self._dies_after:
            raise ConnectionResetError("connection reset")
        limit = len(self._body) - self._sent
        if self._dies_after is not None:
            limit = min(limit, self._dies_after - self._sent)
        chunk = self._body[self._sent : self._sent + min(size if size > 0 else limit, limit)]
        self._sent += len(chunk)
        return chunk


def test_a_download_that_dies_is_resumed_from_the_bytes_on_disk(
    extract: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 5.8 GB archive is not restarted from zero, and the resumed file is the whole file."""
    content = bytes(range(256)) * 40
    requests: list[str | None] = []

    def urlopen(request: Any, timeout: float) -> _Response:
        wanted = request.headers.get("Range")
        requests.append(wanted)
        if wanted is None:
            return _Response(content, 200, dies_after=4000)
        start = int(wanted.removeprefix("bytes=").removesuffix("-"))
        return _Response(content[start:], 206)

    monkeypatch.setattr(extract.urllib.request, "urlopen", urlopen)
    target = tmp_path / "archive"
    extract.fetch("https://example.invalid/archive", target)
    assert target.read_bytes() == content
    assert requests == [None, "bytes=4000-"]
