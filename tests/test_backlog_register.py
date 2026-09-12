"""This repository's two registers must obey the rules they open with, and their anchors resolve.

Ported from `Chemclaw3`'s `tests/test_backlog_register.py`, minus the half of it that exists for one
quoted sentence: that file carries `_HISTORICAL`/`_RETROSPECTIVE` so a *past-tense* measurement of
what the register once was may state a count. No such sentence exists here, and machinery for a case
the tree does not contain is the shape this repository keeps deleting. Add it when a retrospective
sentence is actually written, not before.

What is new here has no counterpart there, and it is the check that repository states in prose and
cannot run: **open every anchor**. Its own 2026-08-17 pass did that by hand and found seventeen rows
unworkable as written, four of them describing code a merged decision had already deleted.
This file is small enough that a backticked path in a row can simply be resolved, so it is.

**The count rule is enforced here for both registers, not only for `docs/BACKLOG.md`.** `CLAUDE.md`
and `docs/decisions/README.md` state the same rule — a number nobody re-derives is a claim about its
author's afternoon — and the ledger is the likelier of the two to accumulate one, because nobody
re-counts records while adding one. One implementation, driven over both files: the alternative is a
second copy of the pattern in `tests/test_decision_log.py`, and this suite runs with
`--import-mode=importlib`, under which a sibling test module cannot be imported (measured).

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
_LEDGER = ROOT / "docs" / "decisions" / "README.md"
# An open row: a checkbox item, up to the next one or the next section. **Not "a checkbox item with
# a bolded title"**, which is what this matched until 2026-09-12 while the register's own header
# published `grep -c '^- [ ]'` as the authority on what a row is. The two disagreed, so a row
# written without bold was a row by the header's definition and invisible to every check in this
# file — including the duplicate check and the anchor check.
# `test_the_row_parse_agrees_with_the_headers_own_command` is what keeps them one definition.
_ROW = re.compile(r"^- \[ \] (.*?)(?=^- \[ \]|^## |\Z)", re.MULTILINE | re.DOTALL)
# The bolded title, where a row has one. It is a row's identity when it exists; the first line is
# the fallback, so an unbolded row still has a name to report and to compare.
_BOLD_TITLE = re.compile(r"^\*\*(.+?)\*\*", re.DOTALL)
# The marker that puts a row's subject outside this checkout. Rule 4 of the register's header.
_OTHER_REPO = "**Other repository:**"
_BACKTICKED = re.compile(r"`([^`]+)`")

# The nouns each register is counted in. Narrow deliberately, so a genuine measurement of something
# else ("40+ ML engines", "eight `src/` roots", "4,717 lines") is untouched: those name a corpus,
# not the queue.
_BACKLOG_NOUNS = r"rows?|findings?|items?|tasks?|entry|entries"
_LEDGER_NOUNS = r"records?|rows?|entry|entries"
# Spelled-out numbers are in the pattern because a mutation walked past it without them: the
# sibling's version matches `\d` only, and "Eight rows are open today." passed while "8 rows"
# failed. The word list stops at twenty — past that nobody writes the number out — plus `dozen`,
# which is the one idiom above it that reads as a count rather than as a round figure.
_WORDS = (
    "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen"
    "|sixteen|seventeen|eighteen|nineteen|twenty|dozen"
)
_COUNT = rf"(?:\d[\d,]*|{_WORDS})"
# What may stand between a count and the noun it counts, and nothing else. Proximity alone is too
# crude to use here: this register's own rule 2 opens "**2 · Every row names an anchor**", which any
# within-N-characters rule reads as a stated count of rows.
_QUALIFIER = r"(?:of its|of them|open|still open|remaining|outstanding|live|more|such)"


def _stated_counts(text: str, nouns: str) -> list[str]:
    """Every phrase in `text` that states a count of the register's own contents.

    Two orders, because a stale count arrives in both and only the first was matched: `8 open rows`,
    and `Rows open: 8` — the second being also how a markdown table cell reads.

    **Backticks are stripped first.** A careful author writes a number in a code span, and
    `` There are `9` open rows. `` walked past a pattern requiring whitespace between the digit and
    the noun. Measured on 2026-09-12, each of these was inserted into the real header and the suite
    stayed green: a code span, the singular (`1 row is open.`), `Only one row remains open.`,
    `a dozen`, `Rows open: 8`, a table cell, and `8 tasks`.

    Args:
        text: the register's own text.
        nouns: an alternation of the nouns *this* register is counted in. A register's nouns are
            its own: `records` counts the ledger and is an ordinary word in the backlog.

    Returns:
        The offending phrases, in file order, as they read with backticks removed.
    """
    plain = text.replace("`", "")
    before = re.compile(rf"\b{_COUNT}\s+(?:{_QUALIFIER}\s+)*(?:{nouns})\b", re.IGNORECASE)
    after = re.compile(rf"\b(?:{nouns})\b\s*(?:\w+\s*)?[:|=]\s*{_COUNT}\b", re.IGNORECASE)
    return [match.group(0) for pattern in (before, after) for match in pattern.finditer(plain)]


def _rows() -> list[tuple[str, str]]:
    """Every open row as `(title, body)`, in file order, the title unwrapped to one line.

    Unwrapped because a title is a row's identity: whether it happens to break across two lines is a
    formatting accident, and comparing the wrapped forms would let one row be re-added under a
    different line break and read as a second item.
    """
    rows: list[tuple[str, str]] = []
    for block in _ROW.findall(_BACKLOG.read_text(encoding="utf-8")):
        bold = _BOLD_TITLE.match(block)
        title = bold.group(1) if bold else block.split("\n", 1)[0]
        rows.append((" ".join(title.split()), block))
    return rows


def test_the_register_has_rows_to_check() -> None:
    """Guard the guard: a parse matching nothing would pass every assertion below it."""
    assert len(_rows()) > 3, "no open rows parsed from docs/BACKLOG.md; the row shape moved"


def test_the_row_parse_agrees_with_the_headers_own_command() -> None:
    """What this file calls a row and what the header tells a reader to count are one definition.

    They were two. `_ROW` required a bolded title while the header publishes `grep -c '^- [ ]'` as
    the authority, so a row written without bold was a row to every reader and invisible to every
    check here — not a duplicate, not missing an anchor, simply absent. A register whose own checks
    disagree with its own instructions has a hole the width of the disagreement.
    """
    text = _BACKLOG.read_text(encoding="utf-8")
    counted = len(re.findall(r"^- \[ \]", text, re.MULTILINE))
    assert len(_rows()) == counted, (
        f"the header's `grep -c` counts {counted} rows and this file parses {len(_rows())}; the "
        "two definitions of a row have drifted apart"
    )


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
    stated = _stated_counts(_BACKLOG.read_text(encoding="utf-8"), _BACKLOG_NOUNS)
    assert not stated, (
        f"docs/BACKLOG.md states a live count of its own rows: {stated}. Cite the `grep -c` and "
        "let it answer; a number here is stale the next time a row lands or closes."
    )


def test_the_decision_ledger_states_no_count_of_its_own_records() -> None:
    """The same rule, over the file that states it — and that nothing was checking.

    `docs/decisions/README.md` says "a number written here is a dated measurement of a named
    commit, never a claim about `HEAD`", and `CLAUDE.md` names both registers when it refuses a
    count in prose. Only the backlog was checked. The ledger is the likelier of the two to grow
    one, because a session adding a record reads the table and nobody re-counts it afterwards.
    """
    stated = _stated_counts(_LEDGER.read_text(encoding="utf-8"), _LEDGER_NOUNS)
    assert not stated, (
        f"docs/decisions/README.md states a count of its own records: {stated}. The table below "
        "it is the count, and it is the only copy that cannot go stale."
    )


def test_the_count_guard_catches_the_shapes_that_walked_past_it() -> None:
    """Every evasion measured on 2026-09-12, as this guard's own data.

    A guard is worth what its worst case is worth, and each of these went into the real register and
    left the suite green. The code span is the one worth naming twice: a backtick is exactly how a
    careful author writes a number in a document like this, so the guard was weakest against the
    person most likely to be believed.

    The clean arms are the other half. This register measures plenty of things that are not itself —
    a corpus, a line count, a number of servers — and a guard that flagged those would be deleted
    within a week, which is the failure mode of every over-eager check.
    """
    for evasion in (
        "There are `9` open rows.",
        "1 row is open.",
        "Only one row remains open.",
        "About a dozen rows are open.",
        "half a dozen items",
        "Rows open: 8",
        "| Open rows | 8 |",
        "8 tasks",
        "8 entries",
        "Eight rows are open today.",
        "12 findings",
        "four of its open rows",
    ):
        assert _stated_counts(evasion, _BACKLOG_NOUNS), f"{evasion!r} walks past the count guard"

    for innocent in (
        "**2 · Every row names an anchor in the tree**",
        "it reached 4,717 lines in twenty-one days",
        "`retro` carries 40+ ML engines across as many containers",
        "eight `src/` roots and no test directory",
        "six things are true of it",
        "grep -c '^- \\[ \\]' docs/BACKLOG.md",
    ):
        assert not _stated_counts(innocent, _BACKLOG_NOUNS), f"{innocent!r} is not a row count"

    # A register's nouns are its own: the ledger is counted in records, the backlog is not.
    assert _stated_counts("The ledger holds 12 records.", _LEDGER_NOUNS)
    assert not _stated_counts("The ledger holds 12 records.", _BACKLOG_NOUNS)


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
    them begins with an entry that exists here. A `::symbol` suffix is cut: the path is what makes
    the row findable, and resolving a symbol means parsing every language in the tree. The limit of
    that trick is stated where it is shared, in `tests/test_fleet.py`: a *single-segment* anchor is
    self-rooting, so a row naming a root-level file that has been deleted stops being read as a path
    at the same moment it stops resolving.

    **Resolution is tracked per row, which is the half that was missing.** The count was global and
    the assertion was `assert checked` once at the end, so a row every one of whose backticked
    tokens is *shorthand* — `mcp_server_kit/egress.py` is the shape, and `CLAUDE.md` writes it
    thirteen times — contributed nothing, was reported by nothing, and the assertion passed on other
    rows' hits. Rule 2 of the register's header says every row names an anchor; this is now the
    check that says so rather than a check that says *some* row does.

    Rows marked as another repository's are skipped, and the skip is **reported** — a check that
    quietly shrinks tells you nothing about what it did not look at.
    """
    top_level = {path.name for path in ROOT.iterdir()}
    missing: list[str] = []
    elsewhere: list[str] = []
    unanchored: list[str] = []
    for title, body in _rows():
        if _OTHER_REPO in body:
            elsewhere.append(title)
            continue
        resolved = 0
        for token in _BACKTICKED.findall(body):
            path = token.split("::", 1)[0]
            if path.split("/")[0] not in top_level:
                continue
            found = (
                sorted(ROOT.glob(path)) if "*" in path else [ROOT / path] * (ROOT / path).exists()
            )
            if found:
                resolved += 1
            else:
                missing.append(f"{title!r} names {path}")
        if not resolved:
            unanchored.append(title)
    assert not missing, (
        f"docs/BACKLOG.md rows naming anchors that do not exist: {missing}. A row whose anchor "
        "cannot be opened is not workable; correct it or delete it."
    )
    assert not unanchored, (
        f"docs/BACKLOG.md rows that resolve no anchor in this tree: {unanchored}. Rule 2 says "
        "every row names one — a path, not a shorthand — so any `grep` finds what it is about."
    )
    if elsewhere:
        warnings.warn(
            f"{len(elsewhere)} BACKLOG row(s) name another repository's anchor and were not "
            f"checked here: {elsewhere}. Nothing in this suite can open those; the row itself "
            "names what to verify and where.",
            stacklevel=1,
        )
