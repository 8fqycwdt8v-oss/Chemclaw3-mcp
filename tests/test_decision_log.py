"""A decision record must identify one decision, and the ledger must match the files beside it.

Ported from `Chemclaw3`'s `tests/test_decision_log.py`, which this repository had no equivalent of:
every argument in `CLAUDE.md` was unanchored prose with no record behind it. Three things went on
the way over, and each is a decision rather than a simplification:

- **The numbered sequence is gone.** That repository carries a frozen `D-NNN` range and the two
  orderings, the stem slice and the sort test that keep it readable. This one has no such range and
  must never acquire one — allocating a number means reading "highest on `origin/main`, plus one",
  which is stale the instant another session pushes. So record order is `sorted(glob("D-*.md"))`,
  which is chronological for free because the date leads the stem.
- **Reservations are gone.** They exist there because sessions had numbers in flight when the dated
  form landed. A dated id cannot be claimed by anyone else, so there is nothing to reserve.
- **The "By topic" index is not here yet**, and neither is its ratchet. `docs/decisions/README.md`
  says when to add both, together.

What came over unchanged is the check most in this repository's idiom:
`test_every_test_a_record_names_still_exists`. `CLAUDE.md` already says "anything this file claims
about a server should be checked by a test in that server", and a rename retires such a citation in
silence — the citation still reads as authoritative while pointing at nothing. The test corpus here
is `tests/`, `packages/*/tests` and `servers/*/tests`, because that is where this fleet's tests are.

One check has no counterpart there: `tests/test_fleet.py::test_the_map_and_the_tree_agree` gives
every **top-level** directory a row-and-README guarantee, and `docs/` lists its own contents by
hand. A new subtree under `docs/` is therefore exactly that failure one level down, which is what
`test_the_docs_map_lists_everything_beside_it` refuses.

Deliberately about *identity and reachability*, not prose: whether a record argues well is a review
matter.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_DOCS = ROOT / "docs"
_DECISIONS = _DOCS / "decisions"
_INDEX = _DECISIONS / "README.md"

# The one id shape: the whole stem, not the date. Two records on one day is normal here, so an id
# that were only the date would name two decisions — the thing this file exists to prevent.
_DATED = r"D-\d{4}-\d{2}-\d{2}-[a-z0-9-]+"
_FILENAME = re.compile(rf"^{_DATED}$")
_HEADING = re.compile(rf"^# ({_DATED}) — ", re.MULTILINE)
_INDEX_ROW = re.compile(rf"^\| \[({_DATED})\]\(([^)]+)\) \| ([^|]*)\|", re.MULTILINE)
_TEST_CITATION = re.compile(r"`(?:[\w./*-]+::)?(test_[a-z0-9_]+)`")
_KEEPS_IT_TRUE = "## What keeps it true"


def _records() -> list[Path]:
    """Every record in record order. The date leads the stem, so a plain sort is chronology."""
    return sorted(_DECISIONS.glob("D-*.md"))


def _record_ids() -> list[str]:
    """Every id that has a file, in record order."""
    return [path.stem for path in _records()]


def _index_rows() -> list[tuple[str, str, str]]:
    """Every `(id, link, title)` in the ledger, in file order."""
    return [
        (adr, link, title.strip())
        for adr, link, title in _INDEX_ROW.findall(_INDEX.read_text(encoding="utf-8"))
    ]


def test_the_record_has_files_to_check() -> None:
    """Guard the guard: a parse matching nothing would pass every assertion below it."""
    assert _records(), "no `D-*.md` files parsed from docs/decisions/; has the naming moved?"
    assert _index_rows(), "no rows parsed from docs/decisions/README.md; has the table shape moved?"


def test_every_record_id_is_unique() -> None:
    """No id names two decisions — a citation that resolves to two is worse than a dangling one.

    The filesystem enforces the common case, since two files cannot share a name. This catches the
    rest: a heading copied into a second file, which is how a record gets written twice.
    """
    duplicates = sorted(adr for adr, count in Counter(_record_ids()).items() if count > 1)
    assert not duplicates, f"docs/decisions/ reuses ids: {duplicates}"


def test_every_filename_matches_its_heading() -> None:
    """The id in the filename is the id in the document, or a citation reaches the wrong record.

    The filename is what the ledger links to and what a `git grep` for a decision finds; the heading
    is what a reader sees. A file renamed without its heading is a mismatch nothing else here sees.
    """
    for path in _records():
        assert _FILENAME.match(path.stem), (
            f"{path.name}: expected `D-YYYY-MM-DD-lowercase-slug.md`; the ledger's links and every "
            "`git grep` for a decision rely on that shape"
        )
        headings = _HEADING.findall(path.read_text(encoding="utf-8"))
        assert headings, f"{path.name} has no `# D-YYYY-MM-DD-slug — Title` heading"
        assert len(headings) == 1, f"{path.name} carries more than one heading: {headings}"
        assert headings[0] == path.stem, (
            f"{path.name} is titled {headings[0]}; filename and heading must name one decision"
        )


def test_the_ledger_lists_exactly_the_records_on_disk() -> None:
    """`docs/decisions/README.md` and the files beside it name the same records, in the same order.

    A ledger that has silently drifted is worse than no ledger: it is consulted and believed. Order
    is asserted too, because it is the record's chronology — a reader scanning for "what was decided
    most recently about this" reads the bottom of that table.
    """
    on_disk = _record_ids()
    listed = [adr for adr, _, _ in _index_rows()]
    missing = [adr for adr in on_disk if adr not in set(listed)]
    extra = [adr for adr in listed if adr not in set(on_disk)]
    assert not missing, f"in docs/decisions/ but not listed in its README.md: {missing}"
    assert not extra, f"listed in docs/decisions/README.md but no such file: {extra}"
    assert on_disk == listed, (
        "docs/decisions/README.md lists the same ids as the files beside it but in a different "
        "order; the table is the record's chronology"
    )


def test_every_row_links_to_its_file() -> None:
    """A row is only useful if it reaches the record, and only true if the link resolves."""
    broken = [(adr, link) for adr, link, _ in _index_rows() if not (_DECISIONS / link).exists()]
    assert not broken, f"docs/decisions/README.md rows whose link resolves to nothing: {broken}"
    mislinked = [(adr, link) for adr, link, _ in _index_rows() if link != f"{adr}.md"]
    assert not mislinked, (
        f"docs/decisions/README.md rows linking somewhere other than their own file: {mislinked}"
    )


def test_no_record_carries_an_unresolved_conflict_marker() -> None:
    """A `<<<<<<<` left behind is invisible to every other check here, and once was in the sibling.

    The checks above parse filenames, headings and `| D-… |` rows, so both sides of a conflict can
    be kept, every id can be fine, and nothing looks at the lines between them.
    """
    for path in [_INDEX, *_records()]:
        offenders = [
            f"{path.name}:{number}"
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
            if line.startswith(("<<<<<<< ", ">>>>>>> ")) or line == "======="
        ]
        assert not offenders, f"unresolved merge conflict markers: {offenders}"


def test_two_records_on_one_day_are_distinct_ids() -> None:
    """The property the dated form exists for, asserted rather than described.

    Two sessions writing a record on the same day is routine across this family. If the id were the
    *date*, the form would reproduce the collision it replaces; because the id is the whole stem,
    same-day records are distinct and only an identical slug collides — as an add/add conflict on a
    filename, which git reports loudly.
    """
    first, second = Path("D-2026-09-12-one-decision.md"), Path("D-2026-09-12-another-entirely.md")
    assert first.stem != second.stem
    assert _FILENAME.match(first.stem) and _FILENAME.match(second.stem)


def test_a_malformed_id_is_still_rejected() -> None:
    """The filename check is a real gate, not a shape that accepts anything beginning with `D-`.

    Including the numbered form, which is the one this repository must never acquire: a `D-NNN`
    landing here would mean somebody had to read `origin/main` to allocate it.
    """
    for bad in ("D-2026-9-12-slug", "D-2026-09-12", "D-2026-09-12-Slug", "D-167-numbered", "D-999"):
        assert not _FILENAME.match(bad), f"{bad} should not be a valid record filename"


def _defined_test_names() -> set[str]:
    """Every `test_*` function this fleet defines, plus every `test_*.py` module stem.

    Both are legitimate things for a record to cite: a sentence naming `tests/test_fleet.py` as
    `test_fleet` would otherwise read as a dangling function. Three test roots, because a server
    tests itself and the kit tests itself — the fleet-level `tests/` is only a third of the suite.
    """
    roots = [
        ROOT / "tests",
        *sorted(ROOT.glob("packages/*/tests")),
        *sorted(ROOT.glob("servers/*/tests")),
    ]
    assert len(roots) > 3, f"only {len(roots)} test roots found; has the layout changed?"
    names: set[str] = set()
    for root in roots:
        for path in sorted(root.rglob("test_*.py")):
            names.add(path.stem)
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
                if isinstance(
                    node, ast.FunctionDef | ast.AsyncFunctionDef
                ) and node.name.startswith("test_"):
                    names.add(node.name)
    return names


def test_every_record_says_what_keeps_it_true() -> None:
    """A record names the guards that hold it, or the check below has nothing to resolve.

    This is what makes the citation check load-bearing rather than decorative: without the section,
    a record can be entirely persuasive and entirely unenforced, and nothing would say so. A
    decision with no guard to name is worth recording as exactly that, in words.
    """
    for path in _records():
        text = path.read_text(encoding="utf-8")
        assert _KEEPS_IT_TRUE in text, (
            f"{path.name} has no `{_KEEPS_IT_TRUE}` section; that section is what a later session "
            "reads to find out whether the decision is still enforced"
        )
        cited = _TEST_CITATION.findall(text[text.index(_KEEPS_IT_TRUE) :])
        assert cited, (
            f"{path.name}'s `{_KEEPS_IT_TRUE}` section names no `test_*`. If nothing enforces the "
            "decision, say so there in a sentence — an empty heading reads as if something does."
        )


def test_every_test_a_record_names_still_exists() -> None:
    """A guard a record cites by name resolves against the suite.

    A citation that resolves to nothing looks identical to one that resolves: it reads as
    authoritative while pointing at nothing, which is `CLAUDE.md`'s deleted port table one level in.
    A merged record is never edited, so when a rename genuinely retires a citation the fix is to
    rename the test back — or, if it is really gone, to add the allowlist this file deliberately
    does not carry yet, with the line saying what replaced it. There is nothing to exempt today.
    """
    defined = _defined_test_names()
    dangling = sorted(
        {
            name
            for path in _records()
            for name in _TEST_CITATION.findall(path.read_text(encoding="utf-8"))
            if name not in defined
        }
    )
    assert not dangling, (
        f"test name(s) cited in docs/decisions/ that resolve to nothing: {dangling}. Rename the "
        "test back, or correct the citation before the record is merged."
    )


def test_the_docs_map_lists_everything_beside_it() -> None:
    """`docs/README.md` is a map, and `tests/test_fleet.py` only checks the top-level one.

    `test_the_map_and_the_tree_agree` gives every top-level directory a row in `CLAUDE.md` and a
    `README.md` of its own, in both directions, "because a map nobody verifies is read, believed,
    and wrong". `docs/` lists its own contents by hand and nothing checked that list, so a new
    subtree here — this record directory is the first — is precisely that failure one level down.

    Both directions, and a `README.md` for every subtree, for the same reason GitHub renders one the
    moment a reader clicks the folder.
    """
    listed = set(
        re.findall(r"\]\((?!\.\./|https?://)([^)#]+)\)", (_DOCS / "README.md").read_text("utf-8"))
    )
    present = {
        path.name + ("/" if path.is_dir() else "")
        for path in _DOCS.iterdir()
        if path.name != "README.md"
    }
    unlisted = sorted(
        name for name in present if name not in listed and name.rstrip("/") not in listed
    )
    assert not unlisted, f"present in docs/ and not linked from docs/README.md: {unlisted}"
    stale = sorted(link for link in listed if not (_DOCS / link).exists())
    assert not stale, f"linked from docs/README.md and not present: {stale}"
    for path in sorted(_DOCS.iterdir()):
        if path.is_dir():
            assert (path / "README.md").exists(), (
                f"docs/{path.name}/ has no README.md; GitHub renders one the moment a reader "
                "clicks the folder, and that is the whole reason for the rule"
            )
