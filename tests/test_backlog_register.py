"""`docs/BACKLOG.md` must obey the rules it opens with, and its anchors must resolve.

Ported from `Chemclaw3`'s `tests/test_backlog_register.py`, minus the half of it that exists for one
quoted sentence: that file carries `_HISTORICAL`/`_RETROSPECTIVE` so a *past-tense* measurement of
what the register once was may state a count. No such sentence exists here, and machinery for a case
the tree does not contain is the shape this repository keeps deleting. Add it when a retrospective
sentence is actually written, not before.

What is new here has no counterpart there, and it is the check that repository states in prose and
cannot run: **open every anchor**. Its own 2026-08-17 pass did that by hand and found seventeen rows
unworkable as written, four of them describing code a merged decision had already deleted.
This file is small enough that a backticked path in a row can simply be resolved, so it is.

Two limits, stated rather than discovered later:

- **A row about another repository is skipped, and the skip is counted and reported.** No test here
  can open `chemclaw2_retrosynthesis`. A check that quietly shrinks is worse than one that says what
  it did not look at, so the skipped rows are named in a warning the run prints.
- **A symbol anchor is not resolved, only its path.** `file.py::_helper` checks `file.py`. Resolving
  the symbol means parsing every language in the tree, and the path is what makes a row findable.

**Whether a row is still *true* is not checkable by any test**, here or there. An anchor that
resolves proves the row can be looked up, not that what it says about the code still holds — which
is why the register's own header says to check a row against `HEAD` before working it.
"""

from __future__ import annotations

import re
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_BACKLOG = ROOT / "docs" / "BACKLOG.md"
# An open row: a checkbox item with a bolded title, which is the shape the whole file uses.
_ROW = re.compile(r"^- \[ \] \*\*(.+?)\*\*(.*?)(?=^- \[ \]|^## |\Z)", re.MULTILINE | re.DOTALL)
# The marker that puts a row's subject outside this checkout. Rule 4 of the register's header.
_OTHER_REPO = "**Other repository:**"
# The nouns this register is counted in. Narrow deliberately, so a genuine measurement of something
# else ("40+ ML engines", "eight `src/` roots") is untouched: those name a corpus, not the queue.
#
# **Spelled-out numbers are in the pattern because a mutation walked past it without them.** The
# sibling's version matches `\d` only; "Eight rows are open today." in this file's header passed it
# while "8 rows" failed, which is a guard that catches the careless phrasing and misses the careful
# one. The word list stops at twenty: past that nobody writes the number out.
_WORDS = (
    "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen"
    "|sixteen|seventeen|eighteen|nineteen|twenty"
)
_COUNT = rf"(?:\d[\d,]*|{_WORDS})"
_STATED_COUNT = re.compile(
    rf"\b{_COUNT}\s+(?:of its\s+)?(?:open\s+)?(?:rows|findings|items)\b", re.IGNORECASE
)
_BACKTICKED = re.compile(r"`([^`]+)`")


def _rows() -> list[tuple[str, str]]:
    """Every open row as `(title, body)`, in file order, the title unwrapped to one line.

    Unwrapped because a title is a row's identity: whether it happens to break across two lines is a
    formatting accident, and comparing the wrapped forms would let one row be re-added under a
    different line break and read as a second item.
    """
    return [
        (" ".join(title.split()), body)
        for title, body in _ROW.findall(_BACKLOG.read_text(encoding="utf-8"))
    ]


def test_the_register_has_rows_to_check() -> None:
    """Guard the guard: a parse matching nothing would pass every assertion below it."""
    assert len(_rows()) > 3, "no open rows parsed from docs/BACKLOG.md; the row shape moved"


def test_no_row_appears_twice() -> None:
    """One item, one row. A second copy reads as a second item, and the two then drift apart."""
    titles = [title for title, _ in _rows()]
    repeated = sorted({title for title in titles if titles.count(title) > 1})
    assert not repeated, (
        f"these rows appear more than once in docs/BACKLOG.md: {repeated}. Keep the copy carrying "
        "the most measurement and delete the other."
    )


def test_nowhere_in_the_file_states_a_live_row_count() -> None:
    """The register carries the `grep`; it must not also carry the answer, in any section.

    A number nobody re-derives is a claim about its author's afternoon, and one printed beside the
    command that disproves it is worse than none. This is the same rule `CLAUDE.md` applies to its
    own `make` targets and to the port table it deleted.
    """
    stated = _STATED_COUNT.findall(_BACKLOG.read_text(encoding="utf-8"))
    assert not stated, (
        f"docs/BACKLOG.md states a live count of its own rows: {stated}. Cite the `grep -c` and "
        "let it answer; a number here is stale the next time a row lands or closes."
    )


def test_the_header_still_shows_how_to_derive_the_count() -> None:
    """Removing the number must not remove the way to get it — that is the other failure."""
    header = _BACKLOG.read_text(encoding="utf-8").split("\n---", 1)[0]
    assert "grep -c '^- \\[ \\]'" in header, (
        "the header no longer shows the command that counts the rows"
    )


def test_every_anchor_a_row_names_exists() -> None:
    """Every path a row names resolves, so the row can be looked up rather than argued about.

    Only backticked tokens rooted at a real top-level entry are treated as paths, which needs no
    allowlist — the same trick `tests/test_fleet.py` uses on `CLAUDE.md`. A row's prose names many
    things that are not paths (`MCP_EGRESS_ALLOW`, `dict[str, float]`, `make check`), and none of
    them begins with a directory that exists here. A `::symbol` suffix is cut: the path is what
    makes the row findable, and resolving a symbol means parsing every language in the tree.

    Rows marked as another repository's are skipped, and the skip is **reported** — a check that
    quietly shrinks tells you nothing about what it did not look at.
    """
    top_level = {path.name for path in ROOT.iterdir()}
    missing: list[str] = []
    elsewhere: list[str] = []
    checked = 0
    for title, body in _rows():
        if _OTHER_REPO in body:
            elsewhere.append(title)
            continue
        for token in _BACKTICKED.findall(body):
            path = token.split("::", 1)[0]
            if path.split("/")[0] not in top_level:
                continue
            checked += 1
            found = (
                sorted(ROOT.glob(path)) if "*" in path else [ROOT / path] * (ROOT / path).exists()
            )
            if not found:
                missing.append(f"{title!r} names {path}")
    assert checked, "no path anchors resolved from docs/BACKLOG.md; rule 2 says every row has one"
    assert not missing, (
        f"docs/BACKLOG.md rows naming anchors that do not exist: {missing}. A row whose anchor "
        "cannot be opened is not workable; correct it or delete it."
    )
    if elsewhere:
        warnings.warn(
            f"{len(elsewhere)} BACKLOG row(s) name another repository's anchor and were not "
            f"checked here: {elsewhere}. Nothing in this suite can open those; the row itself "
            "names what to verify and where.",
            stacklevel=1,
        )
