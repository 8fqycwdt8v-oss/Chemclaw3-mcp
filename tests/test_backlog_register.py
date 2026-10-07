"""This repository's two registers obey the rules they open with, and their anchors resolve.

Checked: every backticked path in an open row resolves (symbol anchors are resolved to their file
only); neither `docs/BACKLOG.md` nor the decision ledger states a count of its own contents, one
implementation driven over both. A row about another repository is skipped, and the skips are
counted and reported. Whether a row is still *true* is not checkable; an anchor that resolves
only proves the row can be looked up.
"""

from __future__ import annotations

import re
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_BACKLOG = ROOT / "docs" / "BACKLOG.md"
_LEDGER = ROOT / "docs" / "decisions" / "README.md"
# An open row: a checkbox item, up to the next one or the next section. This must match the
# header's own `grep -c '^- [ ]'` definition (bold title or not);
# `test_the_row_parse_agrees_with_the_headers_own_command` keeps them one definition.
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
# Spelled-out numbers up to twenty, plus `dozen`, so "Eight rows are open" is caught as well as
# "8 rows".
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

    Both orders are matched (`8 open rows` and `Rows open: 8`, as a table cell reads), and backticks
    are stripped first so a number in a code span is not missed.

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

    The title is a row's identity, so a different line break must not make it a second item.
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

    Otherwise a row outside the parser's definition would be invisible to every check here.
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

    A count printed beside the command that disproves it is worse than none.
    """
    stated = _stated_counts(_BACKLOG.read_text(encoding="utf-8"), _BACKLOG_NOUNS)
    assert not stated, (
        f"docs/BACKLOG.md states a live count of its own rows: {stated}. Cite the `grep -c` and "
        "let it answer; a number here is stale the next time a row lands or closes."
    )


def test_the_decision_ledger_states_no_count_of_its_own_records() -> None:
    """The decision ledger states no count of its own records.

    The same rule as the backlog; the ledger is likelier to grow one, since nobody re-counts it
    after adding a record.
    """
    stated = _stated_counts(_LEDGER.read_text(encoding="utf-8"), _LEDGER_NOUNS)
    assert not stated, (
        f"docs/decisions/README.md states a count of its own records: {stated}. The table below "
        "it is the count, and it is the only copy that cannot go stale."
    )


def test_the_count_guard_catches_the_shapes_that_walked_past_it() -> None:
    """The count guard catches each known evasion and passes legitimate measurements.

    Evasions include code spans, the singular, spelled-out numbers and table cells. The clean arms
    matter equally: the register measures plenty that is not itself, and a guard that flagged those
    would be deleted.
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
    """Every row resolves at least one path anchor, and every path it names exists.

    Only backticked tokens rooted at a real top-level entry are paths; a `::symbol` suffix is cut.
    Rows marked as another repository's are skipped, and the skip is reported.
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
