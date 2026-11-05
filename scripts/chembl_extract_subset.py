"""Build the `servers/chembl` subset from a ChEMBL release: download, verify, select, aggregate.

An operator's script, run outside any serving process (`scripts/README.md`); it is the one place
this fleet fetches ChEMBL, and the only network access it needs is to EMBL-EBI's FTP host.

    python scripts/chembl_extract_subset.py --tier full    --out-dir <dir> --workdir <dir>
    python scripts/chembl_extract_subset.py --tier fixture --out-dir <dir> --workdir <dir>

1. **Download** `chembl_<release>_sqlite.tar.gz` and `checksums.txt` into `--workdir` (skipped for a
   file already there), and **refuse** the archive unless its SHA-256 equals both the digest pinned
   in `SQLITE_SHA256` and the one in EMBL-EBI's `checksums.txt`. A new release is a reviewed edit
   of `RELEASE` and `SQLITE_SHA256`, never a silent follow of `latest/`.
2. **Select** compounds (see `FULL_SELECTION`; the fixture tier takes the ids in `FIXTURE_IDS`),
   plus the parents of any salt form selected.
3. **Aggregate** each selected compound's activities per target, standard type and unit.
4. **Write** `chembl_subset.sqlite` and `dataset.json` (the manifest `mcp_server_kit` verifies) to
   `--out-dir`. The output is a function of the archive and this file: rows are written in key
   order and nothing records the time.

The result is an **adaptation of ChEMBL** under CC BY-SA 3.0: a selection, and values computed
from ChEMBL's records. `dataset.json` says so, and so does every answer the server gives
(`engine/attribution.py`). This script is not legal advice and decides no legal question.

Standard library only, so the image stage that runs it needs nothing but Python.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import statistics
import sys
import tarfile
import urllib.request
from collections.abc import Iterable, Iterator
from pathlib import Path

RELEASE = "37"
BASE_URL = f"https://ftp.ebi.ac.uk/pub/databases/chembl/ChEMBLdb/releases/chembl_{RELEASE}/"
ARCHIVE = f"chembl_{RELEASE}_sqlite.tar.gz"
#: SHA-256 of the archive, as published in `checksums.txt` of release 37 and checked again here.
SQLITE_SHA256 = "33c203740555f96067710cdfc1c3c55d890660e5908ec5cbf5817492c290d281"

#: Download attempts, and the seconds one socket read may stall before an attempt counts as failed.
ATTEMPTS = 5
TIMEOUT_SECONDS = 120
HTTP_PARTIAL = 206

#: Bumped when the layout below changes; `engine/store.py` refuses a file of another version.
SCHEMA_VERSION = "1"
DB_FILE = "chembl_subset.sqlite"

#: Activities counted in a summary. `standard_relation = '='` supplies the values; the other
#: relations are only counted (`n_censored`). A flagged duplicate or a value ChEMBL itself marks
#: as suspect is left out of both, because a median should not rest on what ChEMBL doubts.
EXCLUDED_VALIDITY = (
    "Potential author error",
    "Outside typical range",
    "Non standard unit type",
)
CENSORED_RELATIONS = (">", "<", ">=", "<=", ">>", "<<", "~")

#: The full tier's selection, in one place and in words (`describe_selection`).
FULL_MIN_PHASE = 1.0
FULL_MIN_PCHEMBL_ACTIVITIES = 3

#: The fixture tier: well-known compounds whose names and targets a reviewer can check by eye.
#: Salt forms pull in their parents. Every id must exist in the release or the build refuses.
FIXTURE_IDS = (
    "CHEMBL25",  # ASPIRIN: the readiness probe's compound, in every tier
    "CHEMBL112",  # ACETAMINOPHEN (paracetamol)
    "CHEMBL113",  # CAFFEINE
    "CHEMBL521",  # IBUPROFEN
    "CHEMBL941",  # IMATINIB: thousands of activities, many targets
    "CHEMBL1642",  # IMATINIB MESYLATE: a salt form whose parent is CHEMBL941
    "CHEMBL939",  # GEFITINIB
    "CHEMBL192",  # SILDENAFIL
    "CHEMBL1487",  # ATORVASTATIN
    "CHEMBL1431",  # METFORMIN
    "CHEMBL9113",  # TOLUENE: a solvent, with few activities
    "CHEMBL45211",  # ACETONITRILE: a solvent with none
    "CHEMBL1201585",  # TRASTUZUMAB: an antibody, no structure
)

LICENCE_URL = "https://creativecommons.org/licenses/by-sa/3.0/"
LICENCE_TEXT = (
    f"CC-BY-SA-3.0 (Creative Commons Attribution-ShareAlike 3.0 Unported, {LICENCE_URL}). This "
    f"file is an adaptation of ChEMBL release {RELEASE} (subset + aggregation) and is distributed "
    "under the same licence; credit ChEMBL, EMBL-EBI, and keep the ChEMBL ids and the release "
    "number."
)
REFRESH_OWNER = "team:chemclaw3-mcp-maintainers"
REFRESH_CADENCE = "P6M"


def sha256_of(path: Path) -> str:
    """The SHA-256 of `path`, read in blocks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch(url: str, target: Path) -> None:
    """Download `url` to `target` unless it is already there; the caller verifies it.

    A transfer that dies is resumed from the bytes already on disk (`Range`), up to `ATTEMPTS`
    times, because the archive is 5.8 GB and a build that restarts it from zero is a build nobody
    finishes. Nothing is trusted until `verified_archive` has compared digests.
    """
    if target.exists():
        return
    print(f"downloading {url}", file=sys.stderr)
    partial = target.with_suffix(target.suffix + ".part")
    for attempt in range(1, ATTEMPTS + 1):
        have = partial.stat().st_size if partial.exists() else 0
        request = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})  # noqa: S310
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 - https, fixed
                mode = "ab" if have and response.status == HTTP_PARTIAL else "wb"
                with partial.open(mode) as out:
                    shutil.copyfileobj(response, out, 1 << 20)
            partial.rename(target)
            return
        except OSError as error:
            print(f"attempt {attempt}/{ATTEMPTS} failed: {error}", file=sys.stderr)
    raise SystemExit(f"could not download {url} in {ATTEMPTS} attempts")


def published_checksum(checksums: Path, name: str) -> str:
    """The digest EMBL-EBI's `checksums.txt` lists for `name`."""
    for line in checksums.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == name:
            return parts[0].lower()
    raise SystemExit(f"{checksums} lists no digest for {name}")


def verified_archive(workdir: Path) -> Path:
    """The release archive in `workdir`, downloaded and refused unless both digests agree."""
    workdir.mkdir(parents=True, exist_ok=True)
    archive = workdir / ARCHIVE
    checksums = workdir / "checksums.txt"
    fetch(BASE_URL + "checksums.txt", checksums)
    fetch(BASE_URL + ARCHIVE, archive)
    listed = published_checksum(checksums, ARCHIVE)
    found = sha256_of(archive)
    if not (found == listed == SQLITE_SHA256):
        raise SystemExit(
            f"{ARCHIVE} is not the approved file: pinned {SQLITE_SHA256}, checksums.txt {listed}, "
            f"downloaded {found}. Nothing was extracted."
        )
    return archive


def extract_database(archive: Path, workdir: Path) -> Path:
    """Stream the one `.db` member out of the archive; the rest of it is never written."""
    target = workdir / f"chembl_{RELEASE}.db"
    if target.exists():
        return target
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            if member.isfile() and member.name.endswith(".db"):
                source = tar.extractfile(member)
                if source is None:
                    break
                with target.open("wb") as out:
                    shutil.copyfileobj(source, out, 1 << 20)
                return target
    raise SystemExit(f"{archive} holds no .db member")


SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE compound (
    chembl_id TEXT PRIMARY KEY, pref_name TEXT, max_phase REAL, molecule_type TEXT,
    formula TEXT, mol_weight REAL, canonical_smiles TEXT, inchi_key TEXT, skeleton TEXT,
    parent_chembl_id TEXT
) WITHOUT ROWID;
CREATE TABLE name (
    chembl_id TEXT NOT NULL, name TEXT NOT NULL, name_norm TEXT NOT NULL, is_pref INTEGER NOT NULL,
    PRIMARY KEY (chembl_id, name)
) WITHOUT ROWID;
CREATE TABLE target (
    chembl_id TEXT PRIMARY KEY, pref_name TEXT, target_type TEXT, organism TEXT
) WITHOUT ROWID;
CREATE TABLE activity_summary (
    compound_chembl_id TEXT NOT NULL, target_chembl_id TEXT NOT NULL,
    standard_type TEXT NOT NULL, standard_units TEXT NOT NULL,
    n_exact INTEGER NOT NULL, n_censored INTEGER NOT NULL, n_assays INTEGER NOT NULL,
    median REAL, minimum REAL, maximum REAL, median_pchembl REAL,
    assay_confidence_min INTEGER, assay_confidence_max INTEGER,
    PRIMARY KEY (compound_chembl_id, target_chembl_id, standard_type, standard_units)
) WITHOUT ROWID;
CREATE INDEX compound_inchi_key ON compound (inchi_key);
CREATE INDEX compound_skeleton ON compound (skeleton);
CREATE INDEX name_norm ON name (name_norm);
"""


# --- selection --------------------------------------------------------------------------------


def describe_selection(tier: str) -> str:
    """The selection rule in words, recorded in `meta` and `dataset.json`."""
    if tier == "fixture":
        return (
            f"{len(FIXTURE_IDS)} named compounds (scripts/chembl_extract_subset.py FIXTURE_IDS) "
            "and the parent of any salt form among them"
        )
    return (
        f"compounds with ChEMBL max_phase >= {FULL_MIN_PHASE:g}, plus compounds with at least "
        f"{FULL_MIN_PCHEMBL_ACTIVITIES} exact pChEMBL-bearing activity measured in an assay with "
        "confidence score 9 (direct single-protein target), plus the parent of any salt form "
        "selected"
    )


def select_molregnos(source: sqlite3.Connection, tier: str) -> list[int]:
    """The molregnos in the subset, parents included, in key order."""
    if tier == "fixture":
        marks = ",".join("?" for _ in FIXTURE_IDS)
        rows = source.execute(
            f"SELECT chembl_id, molregno FROM molecule_dictionary WHERE chembl_id IN ({marks})",  # noqa: S608
            FIXTURE_IDS,
        ).fetchall()
        missing = sorted(set(FIXTURE_IDS) - {row[0] for row in rows})
        if missing:
            raise SystemExit(f"fixture ids not in release {RELEASE}: {missing}")
        chosen = {row[1] for row in rows}
    else:
        chosen = {
            row[0]
            for row in source.execute(
                "SELECT molregno FROM molecule_dictionary WHERE max_phase >= ?", (FULL_MIN_PHASE,)
            )
        }
        chosen |= {
            row[0]
            for row in source.execute(
                "SELECT a.molregno FROM activities a JOIN assays s ON s.assay_id = a.assay_id "
                "WHERE s.confidence_score = 9 AND a.pchembl_value IS NOT NULL "
                "AND a.standard_relation = '=' GROUP BY a.molregno HAVING COUNT(*) >= ?",
                (FULL_MIN_PCHEMBL_ACTIVITIES,),
            )
        }
    selected = set(chosen)
    for chunk in chunks(sorted(chosen), 500):
        marks = ",".join("?" for _ in chunk)
        for (parent,) in source.execute(
            f"SELECT parent_molregno FROM molecule_hierarchy WHERE molregno IN ({marks})",  # noqa: S608
            chunk,
        ):
            if parent is not None:
                selected.add(parent)
    return sorted(selected)


def chunks(items: list[int], size: int) -> Iterator[list[int]]:
    """`items` in lists of at most `size`."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


# --- aggregation ------------------------------------------------------------------------------


def _rounded(value: float, low: float, high: float) -> float:
    """`value` without binary noise (nine decimals), kept inside [`low`, `high`] it came from."""
    return min(max(round(value, 9), low), high)


def summarise(
    rows: Iterable[tuple[str, float | None, float | None, int, int | None]],
) -> dict[str, object]:
    """One summary row from the (relation, value, pchembl, assay, confidence) tuples of a group."""
    exact: list[float] = []
    pchembl: list[float] = []
    assays: set[int] = set()
    confidences: list[int] = []
    censored = 0
    for relation, value, pchembl_value, assay_id, confidence in rows:
        if relation == "=" and value is not None:
            exact.append(float(value))
            if pchembl_value is not None:
                pchembl.append(float(pchembl_value))
        elif relation in CENSORED_RELATIONS:
            censored += 1
        else:
            continue  # no usable relation: counted nowhere, so no assay or score is credited to it
        assays.add(assay_id)
        if confidence is not None:
            confidences.append(confidence)
    return {
        "n_exact": len(exact),
        "n_censored": censored,
        "n_assays": len(assays),
        "median": _rounded(statistics.median(exact), min(exact), max(exact)) if exact else None,
        "minimum": min(exact) if exact else None,
        "maximum": max(exact) if exact else None,
        "median_pchembl": (
            _rounded(statistics.median(pchembl), min(pchembl), max(pchembl)) if pchembl else None
        ),
        "assay_confidence_min": min(confidences) if confidences else None,
        "assay_confidence_max": max(confidences) if confidences else None,
    }


# --- writing ----------------------------------------------------------------------------------


def fold(name: str) -> str:
    """A name's comparison form; `engine/lookup.py` folds a query the same way."""
    return " ".join(name.casefold().split())


def write_compounds(
    source: sqlite3.Connection, out: sqlite3.Connection, molregnos: list[int]
) -> dict[int, str]:
    """Insert compounds and names; return molregno -> chembl_id for the aggregation pass."""
    ids: dict[int, str] = {}
    for chunk in chunks(molregnos, 500):
        marks = ",".join("?" for _ in chunk)
        ids.update(
            dict(
                source.execute(
                    "SELECT molregno, chembl_id FROM molecule_dictionary "  # noqa: S608
                    f"WHERE molregno IN ({marks})",
                    chunk,
                )
            )
        )
    for chunk in chunks(molregnos, 500):
        marks = ",".join("?" for _ in chunk)
        parents = {
            row[0]: row[1]
            for row in source.execute(
                "SELECT h.molregno, p.chembl_id FROM molecule_hierarchy h "  # noqa: S608
                "JOIN molecule_dictionary p ON p.molregno = h.parent_molregno "
                f"WHERE h.molregno IN ({marks}) AND h.parent_molregno != h.molregno",
                chunk,
            )
        }
        compound_rows = []
        name_rows: set[tuple[str, str, str, int]] = set()
        for row in source.execute(
            "SELECT md.molregno, md.chembl_id, md.pref_name, md.max_phase, md.molecule_type, "  # noqa: S608
            "cp.full_molformula, cp.full_mwt, cs.canonical_smiles, cs.standard_inchi_key "
            "FROM molecule_dictionary md "
            "LEFT JOIN compound_properties cp ON cp.molregno = md.molregno "
            "LEFT JOIN compound_structures cs ON cs.molregno = md.molregno "
            f"WHERE md.molregno IN ({marks})",
            chunk,
        ):
            molregno, chembl_id, pref_name, phase, kind, formula, weight, smiles, key = row
            compound_rows.append(
                (
                    chembl_id,
                    pref_name,
                    phase,
                    kind,
                    formula,
                    weight,
                    smiles,
                    key,
                    key[:14] if key else None,
                    parents.get(molregno),
                )
            )
            if pref_name:
                name_rows.add((chembl_id, pref_name, fold(pref_name), 1))
        for molregno, synonym in source.execute(
            f"SELECT molregno, synonyms FROM molecule_synonyms WHERE molregno IN ({marks})",  # noqa: S608
            chunk,
        ):
            if synonym and synonym.strip():
                name_rows.add((ids[molregno], synonym.strip(), fold(synonym), 0))
        out.executemany("INSERT INTO compound VALUES (?,?,?,?,?,?,?,?,?,?)", sorted(compound_rows))
        # A synonym equal to the preferred name is the preferred name, listed once.
        preferred = {(row[0], row[1]) for row in name_rows if row[3] == 1}
        out.executemany(
            "INSERT INTO name VALUES (?,?,?,?)",
            sorted(row for row in name_rows if row[3] == 1 or (row[0], row[1]) not in preferred),
        )
    return ids


def write_summaries(
    source: sqlite3.Connection, out: sqlite3.Connection, ids: dict[int, str]
) -> None:
    """Aggregate the selected compounds' activities and insert the summaries and their targets."""
    targets: dict[int, str] = {
        tid: chembl_id
        for tid, chembl_id in source.execute("SELECT tid, chembl_id FROM target_dictionary")
    }
    used: set[int] = set()
    validity = ",".join("?" for _ in EXCLUDED_VALIDITY)
    for chunk in chunks(sorted(ids), 200):
        marks = ",".join("?" for _ in chunk)
        groups: dict[
            tuple[int, int, str, str], list[tuple[str, float | None, float | None, int, int | None]]
        ] = {}
        for (
            molregno,
            tid,
            kind,
            units,
            relation,
            value,
            pchembl,
            assay_id,
            confidence,
        ) in source.execute(
            "SELECT a.molregno, s.tid, a.standard_type, a.standard_units, a.standard_relation, "  # noqa: S608
            "a.standard_value, a.pchembl_value, a.assay_id, s.confidence_score "
            "FROM activities a JOIN assays s ON s.assay_id = a.assay_id "
            f"WHERE a.molregno IN ({marks}) AND a.standard_type IS NOT NULL "
            "AND s.tid IS NOT NULL AND a.standard_value IS NOT NULL "
            "AND COALESCE(a.potential_duplicate, 0) = 0 "
            f"AND COALESCE(a.data_validity_comment, '') NOT IN ({validity})",
            (*chunk, *EXCLUDED_VALIDITY),
        ):
            groups.setdefault((molregno, tid, kind, units or ""), []).append(
                (relation or "", value, pchembl, assay_id, confidence)
            )
        batch = []
        for (molregno, tid, kind, units), members in sorted(
            groups.items(),
            key=lambda item: (ids[item[0][0]], targets[item[0][1]], item[0][2], item[0][3]),
        ):
            summary = summarise(members)
            if not summary["n_exact"] and not summary["n_censored"]:
                continue
            used.add(tid)
            batch.append(
                (
                    ids[molregno],
                    targets[tid],
                    kind,
                    units,
                    summary["n_exact"],
                    summary["n_censored"],
                    summary["n_assays"],
                    summary["median"],
                    summary["minimum"],
                    summary["maximum"],
                    summary["median_pchembl"],
                    summary["assay_confidence_min"],
                    summary["assay_confidence_max"],
                )
            )
        out.executemany("INSERT INTO activity_summary VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
    rows = [
        (chembl_id, name, kind, organism)
        for chembl_id, name, kind, organism in source.execute(
            "SELECT chembl_id, pref_name, target_type, organism "
            "FROM target_dictionary ORDER BY chembl_id"
        )
        if chembl_id in {targets[tid] for tid in used}
    ]
    out.executemany("INSERT INTO target VALUES (?,?,?,?)", rows)


# --- the build --------------------------------------------------------------------------------


def build_subset(source_db: Path, out_dir: Path, tier: str, archive_sha256: str) -> dict[str, int]:
    """Write the subset and `dataset.json` for `tier` into `out_dir`; return the table counts."""
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / DB_FILE
    partial = out_dir / (DB_FILE + ".partial")
    partial.unlink(missing_ok=True)
    source = sqlite3.connect(f"{source_db.resolve().as_uri()}?mode=ro", uri=True)
    out = sqlite3.connect(partial)
    out.executescript(
        "PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF; PRAGMA page_size = 4096;"
    )
    out.executescript(SCHEMA)
    molregnos = select_molregnos(source, tier)
    ids = write_compounds(source, out, molregnos)
    write_summaries(source, out, ids)
    counts = {
        table: out.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608 - fixed names
        for table in ("compound", "name", "target", "activity_summary")
    }
    meta = {
        "schema_version": SCHEMA_VERSION,
        "release": RELEASE,
        "tier": tier,
        "source_url": BASE_URL,
        "source_archive": ARCHIVE,
        "source_archive_sha256": archive_sha256,
        "licence": "CC BY-SA 3.0",
        "licence_url": LICENCE_URL,
        "selection": describe_selection(tier),
        "aggregation": (
            "per compound, target, standard type and unit; values over standard_relation '=' "
            "only, censored values counted, values with no recorded relation not used; "
            "activities flagged potential duplicate or with a "
            f"data validity comment in {list(EXCLUDED_VALIDITY)} excluded"
        ),
        **{f"n_{table}": str(count) for table, count in counts.items()},
    }
    out.executemany("INSERT INTO meta VALUES (?, ?)", sorted(meta.items()))
    out.commit()
    out.execute("VACUUM")
    out.close()
    source.close()
    partial.replace(target)
    write_manifest(out_dir, tier, counts, meta["selection"])
    return counts


def write_manifest(out_dir: Path, tier: str, counts: dict[str, int], selection: str) -> None:
    """Write `dataset.json` beside the subset: the eight fields `mcp_server_kit` verifies."""
    manifest = {
        "name": "chembl-subset",
        "version": f"{RELEASE}-{tier}",
        "licence": LICENCE_TEXT,
        "retrieved_from": (
            f"{BASE_URL}{ARCHIVE} (SHA-256 {SQLITE_SHA256}), built by "
            "scripts/chembl_extract_subset.py. Provenance only: nothing here is fetched at runtime."
        ),
        "description": (
            f"A {tier}-tier subset of ChEMBL release {RELEASE} ({counts['compound']} compounds, "
            f"selected as: {selection}), {counts['name']} names and synonyms, {counts['target']} "
            "targets and "
            f"{counts['activity_summary']} activity summaries aggregated per compound, target, "
            "standard type and unit (median, minimum, maximum and counts over exact values; "
            "censored values counted only). A selection and an aggregation of ChEMBL, not ChEMBL: "
            "check assay-level records before relying on a value."
        ),
        "sha256": sha256_of(out_dir / DB_FILE),
        "refresh_owner": REFRESH_OWNER,
        "refresh_cadence": REFRESH_CADENCE,
    }
    (out_dir / "dataset.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    """Download and verify the release, build the requested tier, report what was written."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else None)
    parser.add_argument("--tier", choices=("full", "fixture"), required=True)
    parser.add_argument(
        "--workdir", type=Path, required=True, help="downloads and the extracted .db"
    )
    parser.add_argument(
        "--out-dir", type=Path, required=True, help="where the subset and dataset.json go"
    )
    parser.add_argument(
        "--source-db",
        type=Path,
        help="build from an already extracted ChEMBL .db and skip the download and the digest "
        "check (a test double or a local copy); recorded as unverified",
    )
    parser.add_argument(
        "--keep", action="store_true", help="keep the archive and the extracted .db in --workdir"
    )
    args = parser.parse_args(argv)
    if args.source_db is not None:
        source_db, digest = args.source_db, "unverified (--source-db)"
        owned: list[Path] = []
    else:
        archive = verified_archive(args.workdir)
        source_db, digest = extract_database(archive, args.workdir), SQLITE_SHA256
        # The archive is spent once the database is out of it; dropping it before the build keeps
        # the peak at the database's size rather than the sum (the release is 5.8 GB and 30 GB).
        if not args.keep:
            archive.unlink(missing_ok=True)
        owned = [source_db]
    counts = build_subset(source_db, args.out_dir, args.tier, digest)
    if not args.keep:
        for path in owned:
            path.unlink(missing_ok=True)
    size = (args.out_dir / DB_FILE).stat().st_size
    print(json.dumps({"tier": args.tier, "bytes": size, **counts}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
