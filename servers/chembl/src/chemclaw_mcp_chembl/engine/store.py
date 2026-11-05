"""The vendored ChEMBL subset: where it is, that it is the approved file, and how it is opened.

The subset is one SQLite file written by `scripts/chembl_extract_subset.py` beside its
`dataset.json`. `dataset()` verifies the checksum once per process; `connect()` opens the file
read-only and immutable, so a query cannot write and the file is never locked or journalled.

There is no setting that moves the file: a swapped-in subset would be a different set of claims
under the same attribution. The image build writes the full subset over the fixture the wheel
carries, and a test points `data_dir` elsewhere.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from functools import lru_cache
from pathlib import Path

from mcp_server_kit import Dataset, load_dataset

__all__ = ["DB_FILE", "SCHEMA_VERSION", "StoreError", "connect", "data_dir", "dataset", "meta"]

DB_FILE = "chembl_subset.sqlite"

#: The layout `scripts/chembl_extract_subset.py` writes; a file of another version is refused.
SCHEMA_VERSION = "1"

#: What `attribution` reads from `meta`; a file without one cannot answer under its licence.
REQUIRED_META = ("release", "tier", "source_url", "licence_url")

_DEFAULT_DIR = Path(__file__).resolve().parent.parent / "data"


class StoreError(RuntimeError):
    """The subset file is not the schema this server reads; `/healthz` answers 503 with this."""


def data_dir() -> Path:
    """The directory holding the subset and its `dataset.json`."""
    return _DEFAULT_DIR


@lru_cache(maxsize=4)
def _verified(directory: Path) -> Dataset:
    """Verify `directory`'s subset against its manifest once, and its schema version."""
    found = load_dataset(directory, records_file=DB_FILE)
    with closing(_open(found.records_path)) as connection:
        recorded = _read_meta(connection)
    if recorded.get("schema_version") != SCHEMA_VERSION:
        raise StoreError(
            f"{found.records_path} has schema_version {recorded.get('schema_version')!r}; this "
            f"server reads {SCHEMA_VERSION!r}. Rebuild it with scripts/chembl_extract_subset.py."
        )
    if missing := [key for key in REQUIRED_META if not recorded.get(key)]:
        raise StoreError(
            f"{found.records_path} has no {', '.join(missing)} in its meta table, so an answer "
            "from it could not carry its attribution"
        )
    return found


def dataset() -> Dataset:
    """The verified subset's provenance record."""
    return _verified(data_dir())


def _open(path: Path) -> sqlite3.Connection:
    """A read-only, immutable connection: no write is possible and no journal is looked for."""
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """A connection to the verified subset, closed on exit; one per call, as it is one thread's."""
    with closing(_open(dataset().records_path)) as connection:
        yield connection


def _read_meta(connection: sqlite3.Connection) -> dict[str, str]:
    return {str(row["key"]): str(row["value"]) for row in connection.execute("SELECT * FROM meta")}


def meta() -> dict[str, str]:
    """The `meta` table: release, tier, source checksum, selection rule and counts."""
    with connect() as connection:
        return _read_meta(connection)
