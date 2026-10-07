"""A vulnerability suppression must expire when the thing it excuses changes.

`make deps-audit` passes `--ignore-vuln` for each advisory declared in `pyproject.toml`'s
`[tool.chemclaw.deps-audit]` table, argued in the `Makefile` against a specific package version.
`pip-audit` matches an id or any of its aliases, so aliases are part of each row. A fixed, bumped
or dropped dependency merely stops being reported, so three checks: the lock still resolves the
package at the argued version; the lock still has the package; and the `Makefile`'s argued ids
equal the table's. Whether the advisory is still open needs the network and is
`make deps-audit`'s question.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Every advisory id shape this repository's suppressions use. Matched against the Makefile's prose,
# which is where the arguments live.
_ADVISORY = re.compile(
    r"\b(?:PYSEC-\d{4}-\d+|GHSA-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}|CVE-\d{4}-\d+)\b"
)


def _suppressions() -> list[dict[str, Any]]:
    """The declared suppressions: the one place the ids exist."""
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    rows = config["tool"]["chemclaw"]["deps-audit"]["suppressions"]
    assert isinstance(rows, list) and rows, "no suppressions declared; has the table moved?"
    return [dict(row) for row in rows]


def _locked() -> dict[str, set[str]]:
    """Every distribution `uv.lock` resolves, name to the set of versions it resolves it at.

    A set because a resolution forks: a package can appear once per environment marker or extra,
    and a single value would keep whichever entry came last. The argued version must be one of them.
    """
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    versions: dict[str, set[str]] = {}
    for entry in lock["package"]:
        versions.setdefault(str(entry["name"]), set()).add(str(entry["version"]))
    return versions


@pytest.mark.parametrize("row", _suppressions(), ids=lambda row: str(row["id"]))
def test_a_suppression_names_a_package_the_lock_still_resolves(row: dict[str, Any]) -> None:
    """A suppression for a dependency that has left the closure is dead text with a live effect."""
    assert row["package"] in _locked(), (
        f"{row['id']} suppresses an advisory about {row['package']!r}, which uv.lock no longer "
        "resolves. Delete the row and its paragraph in the Makefile"
    )


@pytest.mark.parametrize("row", _suppressions(), ids=lambda row: str(row["id"]))
def test_a_suppression_expires_when_its_package_moves(row: dict[str, Any]) -> None:
    """The whole point: an argument written against one version is not evidence about another.

    `--ignore-vuln` matches by id, so without this a bump that fixed the advisory — or one that
    did not — would ship under a suppression argued against the version it replaced.
    """
    actual = _locked()[row["package"]]
    assert actual == {row["version"]}, (
        f"{row['id']} is argued in the Makefile against {row['package']}=={row['version']} and "
        f"uv.lock now resolves {sorted(actual)}. Re-derive the argument against the version(s) "
        "that will ship, then update this row — or delete both if the advisory no longer applies. "
        "More than one version means the resolution forked, and a suppression argued against one "
        "of them says nothing about the other"
    )


def test_the_makefile_argues_exactly_the_suppressions_that_are_declared() -> None:
    """The `Makefile` argues exactly the suppressions that are declared.

    An argued id missing from the table reads as a live control that is not, and a declared id with
    no argument is an unjustified suppression.
    """
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    argued = set(_ADVISORY.findall(makefile))
    declared = {str(row["id"]) for row in _suppressions()}
    # An advisory carried in two databases is one suppression under two names, and the Makefile
    # quotes both. `aliases` is what separates that from a ninth suppression nobody declared.
    declared |= {str(alias) for row in _suppressions() for alias in row.get("aliases", ())}
    assert argued == declared, (
        f"the Makefile argues {sorted(argued - declared)} that pyproject.toml does not suppress, "
        f"and pyproject.toml suppresses {sorted(declared - argued)} that it does not argue"
    )
