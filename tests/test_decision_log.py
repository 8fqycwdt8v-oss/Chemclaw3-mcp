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
import subprocess
import warnings
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_DOCS = ROOT / "docs"
_DECISIONS = _DOCS / "decisions"
_BACKLOG = _DOCS / "BACKLOG.md"
_INDEX = _DECISIONS / "README.md"

# The one id shape: the whole stem, not the date. Two records on one day is normal here, so an id
# that were only the date would name two decisions — the thing this file exists to prevent.
_DATED = r"D-\d{4}-\d{2}-\d{2}-[a-z0-9-]+"
_FILENAME = re.compile(rf"^{_DATED}$")
_HEADING = re.compile(rf"^# ({_DATED}) — ", re.MULTILINE)
_INDEX_ROW = re.compile(rf"^\| \[({_DATED})\]\(([^)]+)\) \| ([^|]*)\|", re.MULTILINE)
_TEST_CITATION = re.compile(r"`(?:[\w./*-]+::)?(test_[a-z0-9_]+)`")
# A commit a record cites. At least one digit, because an all-letter hex word ("defaced") is a real
# English string and an abbreviated hash that happens to be all letters is rare enough to be worth
# the trade: a false positive here fails a run confusingly, a false negative only skips a check.
_COMMIT = re.compile(r"`(?=[0-9a-f]{7,40}`)(?=[a-f]*[0-9])([0-9a-f]{7,40})`")
_KEEPS_IT_TRUE = "## What keeps it true"
# Commits a record names *as unreachable*, which is the one honest reason to write a hash `HEAD`
# does not contain. These four are PR #54's branch commits:
# `D-2026-09-12-a-bypass-that-is-not-in-the-suite-is-not-closed` §5 quotes them because the finding
# *is* that they do not resolve, and `D-2026-09-12-a-ratchet-measures-what-it-parses` cited them as
# evidence until the same pass replaced every one with the merge commit containing them. The
# exemption is checked in both directions below: if one of these ever becomes reachable, the row is
# wrong and has to go.
_QUOTED_AS_UNREACHABLE = frozenset({"68083a4", "39ba4a7", "362e764", "1161473"})


def _records(directory: Path = _DECISIONS) -> list[Path]:
    """Every record in record order. The date leads the stem, so a plain sort is chronology."""
    return sorted(directory.glob("D-*.md"))


def _record_ids(directory: Path = _DECISIONS) -> list[str]:
    """Every id that has a file, in record order."""
    return [path.stem for path in _records(directory)]


def _unfiled_documents(directory: Path = _DECISIONS) -> list[str]:
    """Every `.md` beside the records that is not one — the blind spot of a `D-*` glob.

    `_records` globs `D-*.md`, so a record filed as `2026-09-13-a-decision.md` is not a duplicate,
    not a dangling id and not a missing ledger row: it is *absent*, and every check in this file
    passes while a decision sits unlisted next to them.
    """
    return sorted(
        path.name
        for path in directory.glob("*.md")
        if path.name != "README.md" and not _FILENAME.match(path.stem)
    )


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


def test_two_records_on_one_day_are_distinct_ids(tmp_path: Path) -> None:
    """The property the dated form exists for, now driven over the record machinery.

    **This test was vacuous when it was written and is recorded as such rather than quietly
    rewritten.** It read `first, second = Path("D-…-one-decision.md"),
    Path("D-…-another-entirely.md")`
    and asserted `first.stem != second.stem` — two literals compared against each other, which
    cannot fail for any state of this repository. Measured on 2026-09-12: filing a record in
    `docs/decisions/` under a date-only name left it green while
    `test_every_filename_matches_its_heading` went red, so it contributed nothing that the tree
    could
    break. `c1772fb`'s commit message said one new test came back vacuous and recorded it nowhere;
    this is that record.

    What makes it bite now: the record machinery is run over a directory it is given, so two
    same-day
    records are *built* and read back. A `_records` glob that stopped matching, an `_FILENAME` that
    lost the slug (which is exactly the date-only id this form exists to refuse), or an
    `_unfiled_documents` that stopped seeing a stray file all fail here.
    """
    for stem in ("D-2026-09-12-one-decision", "D-2026-09-12-another-entirely"):
        (tmp_path / f"{stem}.md").write_text(f"# {stem} — A title\n", encoding="utf-8")

    ids = _record_ids(tmp_path)
    assert ids == ["D-2026-09-12-another-entirely", "D-2026-09-12-one-decision"]
    assert len(set(ids)) == len(ids), "same-day records must not share an id"
    for path in _records(tmp_path):
        assert _FILENAME.match(path.stem), f"{path.name} is not a record filename"
        assert _HEADING.findall(path.read_text(encoding="utf-8")) == [path.stem]
        # The date-only form the id deliberately is not: it would name both of these files.
        assert not _FILENAME.match(path.stem[: len("D-2026-09-12")])
    assert _unfiled_documents(tmp_path) == []


def test_a_record_filed_under_another_name_is_not_invisible(tmp_path: Path) -> None:
    """A `D-*` glob is a check that cannot see what it does not glob, which is the hole.

    Every check in this file starts from `_records()`, so a decision filed as
    `2026-09-13-unfiled.md` is not a duplicate, not a dangling id and not an unlisted row — it is
    simply absent, with nothing anywhere saying so. Measured on 2026-09-12: such a file in
    `docs/decisions/` passed the whole module. The missing direction is the one every other map
    check in this repository already has: *everything beside the records is a record*.
    """
    assert _unfiled_documents() == [], (
        "docs/decisions/ holds a document that is neither README.md nor a `D-YYYY-MM-DD-slug.md` "
        "record; rename it, because no check in this file can see it where it is"
    )

    (tmp_path / "2026-09-13-unfiled.md").write_text("# unfiled\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# ledger\n", encoding="utf-8")
    (tmp_path / "D-2026-09-13-filed.md").write_text("# D-2026-09-13-filed — T\n", encoding="utf-8")
    assert _records(tmp_path) == [tmp_path / "D-2026-09-13-filed.md"]
    assert _unfiled_documents(tmp_path) == ["2026-09-13-unfiled.md"]


def test_every_commit_the_registers_cite_is_reachable_from_head() -> None:
    """A record's commit citations are its evidence, and a squash merge can retire all of them.

    `docs/decisions/README.md` rests the whole numbering convention on one sentence — "a number
    written here is a dated measurement of a named commit, never a claim about `HEAD`" — and the
    naming half broke on the first record written under it.
    `D-2026-09-12-a-ratchet-measures-what-it-parses` cited four branch commits and told the reader
    to
    run `git show 39ba4a7:tests/test_fleet.py`; PR #54 was **squash**-merged, so none of the four is
    an ancestor of `main` and the command fails for anyone who clones it. Measured on 2026-09-12
    with
    full history (this checkout was unshallowed first): all four objects still exist *here*, because
    this is the branch they were written on, and `git merge-base --is-ancestor` reports none of them
    reachable from `HEAD`. A citation that resolves only in the session that wrote it is the deleted
    port table with a hash in it.

    So the check is ancestry, not existence — existence is exactly what misleads. On a shallow clone
    ancestry cannot be decided at all, and the honest answer is to say what was not looked at rather
    than to pass: the warning is the same shape `test_every_anchor_a_row_names_exists` uses for a
    row it cannot open.

    **One exemption, and it is the shape this file's own sibling check describes**: a record may
    name a hash `HEAD` cannot reach when the *point* is that it cannot, which is what §5 of
    `D-2026-09-12-a-bypass-that-is-not-in-the-suite-is-not-closed` does. `_QUOTED_AS_UNREACHABLE`
    carries those four with the reason, and the exemption is asserted in both directions: a hash
    listed there that becomes reachable fails too, so the allowlist cannot outlive its argument.

    `docs/BACKLOG.md` is read here too, and for one reason rather than two: it cited the same
    unreachable `362e764` in a row about the static scan, and a second copy of this regex in
    `tests/test_backlog_register.py` would be the second declaration this repository keeps deleting.
    """
    shallow = subprocess.run(
        ["git", "rev-parse", "--is-shallow-repository"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    cited = {
        commit: path.name
        for path in [*_records(), _BACKLOG]
        for commit in _COMMIT.findall(path.read_text(encoding="utf-8"))
    }
    if shallow.returncode != 0 or shallow.stdout.strip() != "false":
        warnings.warn(
            f"the commit citations in docs/ ({sorted(cited)}) were not checked: this is "
            "a shallow clone, where `git merge-base --is-ancestor` cannot decide reachability. "
            "Re-run after `git fetch --unshallow`.",
            stacklevel=1,
        )
        return

    def reachable(commit: str) -> bool:
        return (
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
                cwd=ROOT,
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )

    stale_exemption = sorted(
        commit for commit in _QUOTED_AS_UNREACHABLE & set(cited) if reachable(commit)
    )
    assert not stale_exemption, (
        f"{stale_exemption} is exempted as a hash `HEAD` cannot reach and `HEAD` reaches it; the "
        "row in `_QUOTED_AS_UNREACHABLE` is no longer true and the record can cite it plainly"
    )
    unreachable = sorted(
        f"{record} cites {commit}"
        for commit, record in cited.items()
        if commit not in _QUOTED_AS_UNREACHABLE and not reachable(commit)
    )
    assert not unreachable, (
        f"commit(s) cited in docs/ that `HEAD` does not contain: {unreachable}. A branch "
        "commit does not survive a squash merge; cite the merge commit and the pull request, which "
        "are what a reader of `main` can open."
    )


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
