"""A vulnerability suppression must expire when the thing it excuses changes.

`make deps-audit` passes `--ignore-vuln <id>` for eight advisories, each argued at length in the
`Makefile` against a *specific version* of a specific package. `pip-audit` matches those flags by
id alone, so a dependency that is fixed, replaced or dropped merely stops being reported: the flag
stays, the argument behind it is now about a version nobody ships, and nothing anywhere goes red.
The `Makefile` said so in as many words — "Nothing here goes red when a fix ships" — for as long as
that was the whole mechanism.

So the ids live in `pyproject.toml`'s `[tool.chemclaw.deps-audit]` table with the package and
version each argument was written against, and this file is what makes that pair a control rather
than a note. Three checks, and each catches a different way a suppression rots:

- the lock still resolves the package **to that version** — a bump brings a reader back to the
  argument before the new version ships;
- the lock still **has** the package at all — a suppression for a dependency this fleet has dropped
  is a line nobody would otherwise ever delete;
- every id the `Makefile`'s prose argues about is in the table and every id in the table is argued
  in the `Makefile` — because the flags are derived from the table now, so an id could be retired
  from the gate while its paragraph stayed, and a paragraph reads as a live control.

Deliberately *not* checked: whether the advisory is still open. That needs the network, and it is
the question `make deps-audit` itself asks.
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

    **A set rather than one version, because a resolution forks.** `uv.lock` carries one `package`
    entry per resolved version, and a package whose requirement differs between two environment
    markers or two extras appears twice — measured at `f3f3c9c`, four do: `numpy`,
    `numpy-typing-compat`, `optype` and `scipy-stubs`. Read into a `dict[str, str]`, as this was,
    the later entry silently wins and the check below then compares the suppression's argued
    version against whichever fork `tomllib` happened to yield last. None of the four is suppressed
    today, so this has no live consequence; it is the sort that acquires one on somebody else's
    dependency bump, invisibly, which is the reason to close it while it is still free.

    Held as a set so a fork makes the expiry check *stricter*: the argued version has to be one the
    lock actually resolves, and the failure message can name the others.
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
    """The flags are derived from the table, so the prose is the half that can go stale silently.

    Both directions, because each is a different failure: an argued id missing from the table is a
    control a reader believes is on and is not, and a declared id with no argument is a suppression
    nobody has justified.
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
