"""A decision record identifies one decision, and the ledger matches the files beside it.

About identity and reachability, not prose: whether a record argues well is a review matter. The
checks are unique ids, filename equal to heading, the ledger listing exactly the records on disk,
every `## What keeps it true` test name resolving against the suite, and an `## Options` section on
every record dated on or after the cursor below, and no unresolved merge-conflict marker.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_DECISIONS = ROOT / "docs" / "decisions"
_INDEX = _DECISIONS / "README.md"

# The id is the whole stem, not the date: two records on one day is normal here.
_DATED = r"D-\d{4}-\d{2}-\d{2}-[a-z0-9-]+"
_FILENAME = re.compile(rf"^{_DATED}$")
_HEADING = re.compile(rf"^# ({_DATED}) — ", re.MULTILINE)
_INDEX_ROW = re.compile(rf"^\| \[({_DATED})\]\(([^)]+)\) \| ([^|]*)\|", re.MULTILINE)
_TEST_CITATION = re.compile(r"`(?:[\w./*-]+::)?(test_[a-z0-9_]+)`")
_KEEPS_IT_TRUE = "## What keeps it true"
# Records dated on or after this carry an `## Options` section (`TEMPLATE.md`).
_OPTIONS_CURSOR = "D-2026-10-07"

# Tests a merged record cites that no longer exist, with what replaced them. A merged record is
# never edited, so a retired citation is listed here rather than corrected in place.
_PROSE_TESTS = (
    "retired with the other prose-policing tests (D-2026-10-07-the-record-gets-lean, adopting "
    "Chemclaw3's D-2026-10-07-the-architecture-programme decision 5)"
)
_RETIRED_CITATIONS: dict[str, str] = {
    "test_only_the_depiction_is_gated_and_it_is_gated": (
        "servers/chem/tests/test_admission.py::test_the_band_is_gated_and_nothing_else_is"
    ),
    "test_a_bare_tools_key_is_an_empty_list_rather_than_a_type_error": (
        "packages/mcp_server_kit/tests/test_manifest_model.py::"
        "test_an_endpoint_the_consumer_cannot_load_is_refused_here"
    ),
    "test_a_dynamic_import_of_a_computed_name_is_deliberately_not_flagged": (
        "packages/mcp_server_kit/tests/test_no_egress.py::"
        "test_a_dynamic_import_of_a_computed_name_must_be_justified_at_its_site"
    ),
    "test_every_record_says_what_keeps_it_true": (
        "tests/test_decision_log.py::test_every_test_a_record_names_still_exists"
    ),
    "test_the_required_file_set_is_declared_once": (
        "tests/test_fleet_layout.py::test_the_checklist_lists_every_required_file"
    ),
    "test_claude_md_holds_no_second_port_registry": _PROSE_TESTS,
    "test_no_prose_here_counts_this_fleet_s_servers_without_naming_them": _PROSE_TESTS,
    "test_claude_md_and_the_guard_name_the_same_channels_as_outside_it": _PROSE_TESTS,
    "test_claude_md_claims_no_interception_the_guard_does_not_make": _PROSE_TESTS,
    "test_every_path_claude_md_cites_under_a_real_directory_resolves": _PROSE_TESTS,
    "test_every_path_first_party_source_cites_resolves": _PROSE_TESTS,
    "test_every_other_repository_citation_is_still_cited_and_still_not_here": _PROSE_TESTS,
    "test_every_commit_the_registers_cite_is_reachable_from_head": _PROSE_TESTS,
    "test_a_record_names_the_file_its_test_lives_in": _PROSE_TESTS,
    "test_every_retired_citation_names_a_live_replacement": _PROSE_TESTS,
    "test_two_records_on_one_day_are_distinct_ids": _PROSE_TESTS,
    "test_a_record_filed_under_another_name_is_not_invisible": _PROSE_TESTS,
}


def _records() -> list[Path]:
    """Every record in record order. The date leads the stem, so a plain sort is chronology."""
    return sorted(_DECISIONS.glob("D-*.md"))


def _index_rows() -> list[tuple[str, str, str]]:
    """Every `(id, link, title)` in the ledger, in file order."""
    return [
        (adr, link, title.strip())
        for adr, link, title in _INDEX_ROW.findall(_INDEX.read_text(encoding="utf-8"))
    ]


def _test_definitions() -> set[str]:
    """Every `test_*` function name and `test_*.py` module stem this fleet defines."""
    roots = [ROOT / "tests", *ROOT.glob("packages/*/tests"), *ROOT.glob("servers/*/tests")]
    names: set[str] = set()
    for root in roots:
        for path in root.rglob("test_*.py"):
            names.add(path.stem)
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and (
                    node.name.startswith("test_")
                ):
                    names.add(node.name)
    return names


def test_the_record_has_files_to_check() -> None:
    """Guard the guard: a parse matching nothing would pass every assertion below it."""
    assert _records(), "no `D-*.md` files parsed from docs/decisions/; has the naming moved?"
    assert _index_rows(), "no rows parsed from docs/decisions/README.md; has the table shape moved?"


def test_every_record_id_is_unique() -> None:
    """No id names two decisions, including a heading copied into a second file."""
    headings = [
        h for path in _records() for h in _HEADING.findall(path.read_text(encoding="utf-8"))
    ]
    duplicates = sorted(adr for adr, count in Counter(headings).items() if count > 1)
    assert not duplicates, f"docs/decisions/ reuses ids: {duplicates}"


def test_every_filename_matches_its_heading() -> None:
    """The filename id is the id in the one heading; numbered and date-only ids are refused."""
    for bad in ("D-2026-9-12-slug", "D-2026-09-12", "D-2026-09-12-Slug", "D-167-numbered"):
        assert not _FILENAME.match(bad), f"{bad} should not be a valid record filename"
    for path in _records():
        assert _FILENAME.match(path.stem), f"{path.name}: expected `D-YYYY-MM-DD-lowercase-slug.md`"
        headings = _HEADING.findall(path.read_text(encoding="utf-8"))
        assert headings == [path.stem], (
            f"{path.name} carries headings {headings}; expected exactly `# {path.stem} — Title`"
        )


def test_the_ledger_lists_exactly_the_records_on_disk() -> None:
    """The ledger names exactly the records on disk, in order, each row linking its own file."""
    on_disk = [path.stem for path in _records()]
    rows = _index_rows()
    listed = [adr for adr, _, _ in rows]
    assert sorted(set(on_disk) - set(listed)) == [], "records missing from docs/decisions/README.md"
    assert sorted(set(listed) - set(on_disk)) == [], "ledger rows with no record file"
    assert on_disk == listed, "docs/decisions/README.md lists the records out of date order"
    mislinked = [(adr, link) for adr, link, _ in rows if link != f"{adr}.md"]
    assert not mislinked, f"ledger rows linking somewhere other than their own file: {mislinked}"


def test_every_test_a_record_names_still_exists() -> None:
    """Each record says what keeps it true, and every test name it cites resolves or is retired."""
    defined = _test_definitions()
    dangling: set[str] = set()
    for path in _records():
        text = path.read_text(encoding="utf-8")
        assert _KEEPS_IT_TRUE in text, f"{path.name} has no `{_KEEPS_IT_TRUE}` section"
        names = set(_TEST_CITATION.findall(text))
        dangling |= {name for name in names if name not in defined}
    unresolved = sorted(dangling - set(_RETIRED_CITATIONS))
    assert not unresolved, (
        f"test name(s) cited in docs/decisions/ that resolve to nothing: {unresolved}. "
        "Rename the test back, or list it as retired."
    )
    revived = sorted(name for name in _RETIRED_CITATIONS if name in defined)
    assert not revived, f"listed as retired but defined again: {revived}"


def test_a_record_from_the_cursor_on_weighs_its_options() -> None:
    """A record dated on or after the cursor carries `## Options`: a decision is a choice."""
    missing = [
        path.name
        for path in _records()
        if path.stem >= _OPTIONS_CURSOR and "\n## Options" not in path.read_text(encoding="utf-8")
    ]
    assert not missing, f"records with no `## Options` section (see TEMPLATE.md): {missing}"


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
