"""What this fleet declares and another repository consumes, checked from *this* side.

Two facts leave this tree and are read by `Chemclaw3`:

1. **Three bundle manifests it also ships a copy of** — `chem`, `rxnpredict` and `safety`. Both
   trees declare the tool list, the read-only partition and the bearer variable; whichever
   directory comes first on `CHEMCLAW_CONNECTORS_DIR` wins the name outright, with no merge and no
   warning, so a tool added on one side is simply unreachable in the order the other deploys in.
2. **`servers/calc/tool-surface.json`** — the seam with no manifest in either direction.
   `Chemclaw3` reaches this fleet's `calc` server from inside `science/calc/store.py`, with the
   tool names and argument dicts hardcoded in `connectors/calc/`, and that file is the only record
   they are checked against.

Both are already checked *there*. Here, `assert_manifest_matches` drives each manifest against its
own running server and pins `tool-surface.json` with it — so a manifest that has drifted from its
server fails in this tree. What no check in this tree could see is the drift that matters most for
these three: a rename carried out correctly and completely **here**, server and manifest and
recorded surface together, exactly as `CLAUDE.md` requires in one commit. That commit is green
through `make check` and red only in the consumer's suite, on somebody else's merge. **The gate
that catches a change has to be the gate of the tree the change is made in.**

**So this file runs the consumer's own agreement module rather than reproducing it.** Not because
duplication is untidy — because the copy would be the thing under test. That module parses
`connectors/calc/compose.py` and `remote.py` with an AST walker resolving literals, ternaries and
name bindings; a second implementation here would agree with itself and drift from the call sites
it is meant to read, which is the failure `tests/test_identity_contract.py` records for the header
names — two constants consistent with each other, both wrong about the sender. It also means an
agreement check the consumer adds next month is inherited here with no edit.

**Opt-in, and it can only skip or fail.** A consumer checkout is a fact about a machine, not a
property of a commit. Without one this skips with the reason in the message, and a skip is not a
pass. What it must never do is *quietly* shrink: a checkout that is present while the module it
would run is not is a **failure**, because that is a rename this side has to hear about.

What the arrangement costs, stated rather than implied. Two things:

- The consumer's suite is the authority on what is compared, so a check deleted there is silently
  deleted here, and this side would not know. That is a smaller risk than a divergent copy and it
  is not zero; `docs/BACKLOG.md` names it.
- **It reads that checkout's working tree, not its `HEAD`.** Observed on 2026-09-14: the sibling
  checkout on this machine carried an uncommitted rewrite of the very module this runs, and an
  uncommitted mutation in the code that module reads — somebody else's mutation check, in flight.
  So a failure here can be about a colleague's unstaged edit rather than about this tree, and the
  first thing to do with one is `git -C <checkout> status`. Reading `HEAD` instead was considered
  and is worse: it would check this tree against a revision nobody is running, and miss exactly the
  pre-merge disagreement this file exists to catch.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from mcp_server_kit.testing import (
    CONNECTOR_NAME_PATTERN,
    MAX_MANIFEST_TEXT_CHARS,
    ConnectorManifest,
)
from pydantic import ValidationError

#: The head of every skip message a missing consumer checkout produces, so one reporter can find
#: them all — `conftest.py::pytest_terminal_summary` matches on it.
CONSUMER_SKIP = "[no Chemclaw3 checkout]"

#: The canonical checkout name, in the casing GitHub publishes it and the casing a clone usually
#: lands in. Both, because a single hardcoded default was measured wrong either way in
#: `Chemclaw3/infra/live/siblings.sh`, whose header records exactly that.
CONSUMER_NAMES = ("Chemclaw3", "chemclaw3")

#: Every environment variable that may name the consumer checkout, most specific first.
CONSUMER_ENV_VARS = ("CHEMCLAW3_REPO", "CHEMCLAW_CORE_REPO")

#: The consumer's module holding the agreement, relative to its checkout. A rename there is a
#: failure here rather than a skip: it is the one part of this arrangement that could go quiet.
AGREEMENT_MODULE = "tests/test_sibling_manifest_agreement.py"

ROOT = Path(__file__).resolve().parents[1]


def consumer_repo() -> tuple[Path | None, str]:
    """The `Chemclaw3` checkout, or `None` and the reason there is not one.

    Not delegated to a shell the way the consumer's own sibling lookup is: no script in this
    repository already answers this question, so asking one would mean writing it first. The search
    *mirrors* `Chemclaw3/infra/live/siblings.sh` deliberately — the same two roots, the same two
    casings — because a lookup that resolves differently from the live lanes' is a second answer to
    one question, which is the defect that file exists to have ended.
    """
    for variable in CONSUMER_ENV_VARS:
        named = os.environ.get(variable)
        if named:
            root = Path(named)
            return (root, "") if root.is_dir() else (None, f"{variable}={named} is not a directory")
    roots = (ROOT.parent, ROOT.parent / "8fqycwdt8v-oss")
    for parent in roots:
        for name in CONSUMER_NAMES:
            candidate = parent / name
            if candidate.is_dir():
                return candidate, ""
    searched = ", ".join(str(parent / CONSUMER_NAMES[0]) for parent in roots)
    return None, f"no Chemclaw3 checkout ({searched}; set {' or '.join(CONSUMER_ENV_VARS)})"


def consumer_python() -> tuple[Path | None, str]:
    """The consumer checkout's own interpreter, or `None` and the reason there is not one.

    Its interpreter rather than this one's: its tests import `chemclaw` out of its own environment.
    Separate from `consumer_repo` because the two costs differ — reading that tree needs a shallow
    clone, running its suite needs its whole dependency closure built.
    """
    root, reason = consumer_repo()
    if root is None:
        return None, reason
    interpreter = root / ".venv" / "bin" / "python"
    if not interpreter.exists():
        return None, f"{root} has no .venv — run `make install` there to run its agreement suite"
    return interpreter, ""


# The outcomes pytest reports with a returncode of 0 and which assert nothing about this tree.
# `xfailed`/`xpassed` are the pair the substring check could not see: such a test is collected and
# *runs*, so no skip count mentions it, and its body is allowed to fail.
_INERT_OUTCOMES = ("skipped", "xfailed", "xpassed", "deselected")
_OUTCOME_COUNT = re.compile(r"(\d+) (passed|skipped|xfailed|xpassed|deselected)\b")


def inert_outcome(output: str) -> str | None:
    """Why a green consumer run asserted nothing about this tree, or `None` if it did assert.

    Read off pytest's own counts rather than off two substrings, because a returncode of 0 is what
    pytest reports for a pass, a skip, an xfail, an xpass and an empty selection alike — so
    "returncode 0 and the word `skipped` is absent" accepts three of those five. The rule is the
    positive one: at least one test passed, and nothing inert ran beside it.
    """
    counts: dict[str, int] = {}
    for number, outcome in _OUTCOME_COUNT.findall(output):
        counts[outcome] = counts.get(outcome, 0) + int(number)
    present = [f"{counts[name]} {name}" for name in _INERT_OUTCOMES if counts.get(name)]
    if present:
        return "reported " + ", ".join(present)
    if not counts.get("passed"):
        return "ran no test that passed"
    return None


def test_an_inert_consumer_run_is_not_agreement() -> None:
    """The bite test for `inert_outcome`, on pytest's real summary lines.

    A table rather than a synthetic checkout: what is under test is the reading of an outcome, and
    building a second repository to produce one would test `subprocess` instead.

    **Every row carries a `passed` count, and that is the whole point of the table.** A row with no
    pass in it returns non-`None` whatever `_INERT_OUTCOMES` holds, so it cannot tell a widened
    implementation from the blind one it replaced — which is what a bite test is for. Driven against
    the shipped function, `'1 xfailed in 0.31s'` is answered `'reported 1 xfailed'` by the
    `_INERT_OUTCOMES` arm, which is consulted *first*; gutted back to `("skipped",)`, the same row
    falls through and is answered `'ran no test that passed'` by the pass arm. Both are non-`None`,
    so a table of lone-`xfailed`, lone-`xpassed` and lone-`deselected` rows proves the weaker rule
    four times over and the widening not once — measured: that table stayed green against the gutted
    tuple. The shape that actually occurs has a pass beside the inert outcome, because the consumer
    module being run has more than one test and an xfail is added to one of them.

    **This paragraph shipped saying such a row "never reaches `_INERT_OUTCOMES` at all"**, which is
    false in the direction that makes the reasoning look tighter than it is: the two arms are in the
    other order. The conclusion is unchanged and the route to it is not, which is why the route is
    now written as the measurement rather than as the story.
    """
    assert inert_outcome("2 passed in 0.31s") is None
    assert inert_outcome("1 passed, 1 warning in 0.4s") is None
    for green_but_empty in (
        "2 passed, 1 xfailed in 0.31s",
        "2 passed, 1 xpassed in 0.31s",
        "1 passed, 1 skipped in 0.3s",
        "2 passed, 2 deselected in 0.3s",
        "no tests ran in 0.01s",
        "",
    ):
        assert inert_outcome(green_but_empty) is not None, green_but_empty


def test_the_consumer_still_agrees_with_the_surface_this_tree_declares() -> None:
    """Run `Chemclaw3`'s agreement suite against *this* checkout, and fail on its failure.

    `CHEMCLAW_MCP_REPO` is set to this tree rather than left to that repository's own search, so
    what is under test is this working copy and not whichever fleet checkout happens to sit beside
    it. A **skip** over there is a failure here for the same reason: the checkout was supplied, so
    a skipped run means the module could not read what it was pointed at.

    **A skip was the only inert outcome this refused, and it is not the only one.** Driven against a
    synthetic consumer module at `24b50ec`: a `pytest.skip(...)` fails here correctly and a renamed
    module fails here correctly, but a `@pytest.mark.xfail` test whose body is `assert False`
    **passes** — returncode 0, output `1 xfailed`, and neither of the two substrings this looked
    for. `inert_outcome` reads pytest's own counts instead, and requires at least one `passed` with
    no skip, xfail, xpass or deselection beside it.

    Nothing is written into the consumer's tree — `-p no:cacheprovider` and
    `PYTHONDONTWRITEBYTECODE` between them leave no `.pytest_cache` and no `__pycache__`.
    """
    interpreter, reason = consumer_python()
    if interpreter is None:
        pytest.skip(
            f"{CONSUMER_SKIP} the consumer's agreement suite was NOT run: {reason}. Nothing in "
            "this run is evidence about whether Chemclaw3 still reads the surface this tree "
            "declares."
        )
    checkout = interpreter.parents[2]
    module = checkout / AGREEMENT_MODULE
    assert module.is_file(), (
        f"{module} does not exist. That module is the only check, in either repository, comparing "
        "this fleet's three shared manifests and its recorded calc surface against the repository "
        "that consumes them — so a rename of it retires this check in silence. Point "
        "AGREEMENT_MODULE at its new path in the same pull request."
    )
    completed = subprocess.run(
        [str(interpreter), "-m", "pytest", AGREEMENT_MODULE, "-p", "no:cacheprovider", "-q"],
        cwd=checkout,
        env={**os.environ, "CHEMCLAW_MCP_REPO": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=900,
    )
    output = f"{completed.stdout}\n{completed.stderr}"
    assert completed.returncode == 0, (
        f"Chemclaw3 no longer agrees with the surface this tree declares.\n{output[-4000:]}"
    )
    inert = inert_outcome(output)
    assert inert is None, (
        f"the consumer's agreement suite {inert} against this checkout, although CHEMCLAW_MCP_REPO "
        f"named {ROOT}. A run that asserted nothing is not a pass here — a returncode of 0 is what "
        f"pytest reports for a skip, an xfail and an empty selection alike.\n{output[-4000:]}"
    )


def test_every_place_the_live_lanes_look_for_the_checkout_is_looked_in_here(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The search here must not be a second, different answer to "where is Chemclaw3".

    `Chemclaw3/infra/live/siblings.sh` is that family's one resolution, and the defect its header
    records is a lookup that resolved for one caller and not another *on a machine that had the
    checkout*: one path in one casing against four in two. So all four candidates are **driven**
    here — built under a temporary root, one at a time — rather than asserted as a tuple of names,
    which would pass on a search that consulted the tuple and looked nowhere.

    Driven with no checkout present too, because the reason string is what a reader gets on the
    machine where the check above can only skip: it has to name both roots and both variables, or
    a missing checkout is indistinguishable from a lookup that never looked there.
    """
    for variable in CONSUMER_ENV_VARS:
        monkeypatch.delenv(variable, raising=False)
    fleet = tmp_path / "somewhere" / "Chemclaw3-mcp"
    # `consumer_repo` reads this module's own `ROOT`, so the module globals are the seam — and
    # a string target would need `tests.test_consumer_agreement` to be importable, which depends
    # on how pytest was invoked (see `conftest.py::_consumer_skip_marker`).
    monkeypatch.setitem(globals(), "ROOT", fleet)

    found, reason = consumer_repo()
    assert found is None, found
    for named in (str(fleet.parent), "8fqycwdt8v-oss", *CONSUMER_ENV_VARS):
        assert named in reason, (
            f"the reason a checkout was not found must name {named!r}: {reason!r}"
        )

    for candidate in (
        fleet.parent / "Chemclaw3",
        fleet.parent / "chemclaw3",
        fleet.parent / "8fqycwdt8v-oss" / "Chemclaw3",
        fleet.parent / "8fqycwdt8v-oss" / "chemclaw3",
    ):
        candidate.mkdir(parents=True)
        found, reason = consumer_repo()
        assert found == candidate, (
            f"a checkout at {candidate} was not found (got {found!r}, {reason!r}). The live lanes "
            "resolve it there, so a lane and this gate would disagree about the same machine."
        )
        candidate.rmdir()

    named = tmp_path / "named-explicitly"
    named.mkdir()
    monkeypatch.setenv(CONSUMER_ENV_VARS[0], str(named))
    assert consumer_repo() == (named, ""), "the variable must win over the default roots"


# ---------------------------------------------------------------------------------------------
# The manifest model this fleet validates against, held to the one that actually reads a manifest.
#
# `mcp_server_kit.testing.ConnectorManifest` says in its own docstring that it is modelled "the way
# the repository that reads it models it… so the refusal happens here instead" of aborting the
# consumer's startup. Nothing checked it, and at `6c6a0eb` it was wrong in **both** directions at
# once (`D-2026-09-16-a-stand-in-that-refuses-a-real-field-is-not-a-stand-in`). Which is the shape
# this whole file is about: a claim about another repository is checked by reading it.
# ---------------------------------------------------------------------------------------------

#: A well-formed endpoint block, reused by every probe below so each one varies exactly one thing.
#: `read_only` covers the one declared tool, so removing it is the unclassified-tool probe.
_ENDPOINT: dict[str, object] = {
    "transport": "http",
    "url": "http://127.0.0.1:8850/mcp",
    "auth": {"mode": "bearer", "token_env": "CHEMCLAW_PROBE_TOKEN"},
    "tools": ["a_tool"],
    "read_only": ["a_tool"],
    "state_changing": [],
}


def _manifest(**overrides: object) -> dict[str, object]:
    """A minimal valid manifest document with `overrides` applied on top."""
    return {"name": "probe", "description": "a probe manifest", "endpoint": _ENDPOINT, **overrides}


#: `(label, document, verdict here, verdict over there)`. Where the two columns differ, the reason
#: is one of the deliberate differences `ConnectorManifest`'s docstring lists and nothing else; the
#: rows that used to differ *by accident* are the reason this table exists. The verdicts are written
#: per side rather than as "agree"/"disagree" precisely so a difference has to be typed out.
_MANIFEST_PROBES: tuple[tuple[str, dict[str, object], bool, bool], ...] = (
    ("a plain manifest", _manifest(), True, True),
    # The false accepts, measured at 6c6a0eb: both of these validated here and abort startup there.
    ("a name that is not a slug", _manifest(name="Calc_Server!"), False, False),
    ("a name with an underscore", _manifest(name="calc_server"), False, False),
    ("a description past the cap", _manifest(description="x" * 20_000), False, False),
    # The false refuses: real fields of the consumer's model that this one did not declare.
    (
        "endpoint.knowledge_read",
        _manifest(endpoint={**_ENDPOINT, "knowledge_read": ["a_tool"]}),
        True,
        True,
    ),
    ("top-level skills", _manifest(skills=["a-skill"]), True, True),
    ("top-level profiles", _manifest(profiles=["a-profile"]), True, True),
    ("top-level note_types", _manifest(note_types=["job-result"]), True, True),
    ("top-level relations", _manifest(relations=["computed-from"]), True, True),
    # Still refused on both sides, which is what makes the row above a widening rather than a hole.
    ("an invented key", _manifest(nonsense=["x"]), False, False),
    # The deliberate differences. `endpoint` is the fourth the docstring names and does not show as
    # a difference here: this probe drops the endpoint *and* declares no jobs, so the consumer
    # refuses it too, for its own reason (`_contributes_capability`).
    ("an unclassified tool", _manifest(endpoint={**_ENDPOINT, "read_only": []}), True, False),
    ("mount: backend", _manifest(mount="backend"), True, False),
    (
        "auth: mode none",
        _manifest(endpoint={**_ENDPOINT, "auth": {"mode": "none"}}),
        False,
        True,
    ),
    ("no endpoint at all", {"name": "probe", "description": "d", "jobs": []}, False, False),
)

#: Read each document on the consumer's own interpreter and report accept/refuse, nothing else.
#: Deliberately not importing anything of this repository's: what is wanted is that model's verdict.
_CONSUMER_VERDICTS = """
import json, sys
from chemclaw.connectors.manifest import ConnectorManifest
from chemclaw.core.manifest_io import MAX_MANIFEST_TEXT_CHARS
verdicts = []
for document in json.load(sys.stdin):
    try:
        ConnectorManifest.model_validate(document)
        verdicts.append(True)
    except Exception:
        verdicts.append(False)
json.dump(
    {
        "verdicts": verdicts,
        "max_text_chars": MAX_MANIFEST_TEXT_CHARS,
        "name_pattern": ConnectorManifest.model_fields["name"].metadata[-1].pattern,
    },
    sys.stdout,
)
"""


def _accepts_here(document: dict[str, object]) -> bool:
    """Whether `mcp_server_kit.testing`'s model validates `document`."""
    try:
        ConnectorManifest.model_validate(document)
    except ValidationError:
        return False
    return True


def test_the_stand_in_manifest_model_refuses_what_it_says_it_refuses() -> None:
    """This half runs everywhere, because a table nobody can read is worth nothing on a laptop.

    The verdicts *over there* need a checkout; the verdicts *here* need only this model, and they
    are what fails the moment somebody loosens it. Both halves read one table, so the two cannot
    describe different probes.
    """
    for label, document, here, _ in _MANIFEST_PROBES:
        assert _accepts_here(document) is here, (
            f"{label}: mcp_server_kit.testing.ConnectorManifest "
            f"{'refused' if here else 'accepted'} it, and the table says the opposite"
        )


def test_the_stand_in_manifest_model_agrees_with_the_model_that_reads_a_manifest() -> None:
    """Every probe, and every manifest this fleet ships, through the consumer's own model.

    **Why the shipped manifests are in here as well as the probes.** The stand-in accepts `jobs`,
    `skills`, `profiles`, `note_types` and `relations` without validating their contents, because
    a second copy of `JobSpec` and its three cross-field validators would be a second answer to one
    question. That is a real gap in the stand-in and this is what closes it: the files that are
    actually published go through the model that will actually read them, so a fleet manifest the
    consumer would refuse fails here regardless of how loosely the stand-in is typed.

    `manifests-internal/` is expected to be **refused** over there, and that refusal is the whole
    mechanism behind `manifests-internal/`: `mount:` is a key the consumer's `extra="forbid"` model
    will not take, so pointing `CHEMCLAW_CONNECTORS_DIR` at that directory is a startup error
    naming the file rather than a backend winning a name collision.

    Skips with the reason when there is no checkout, and a skip is not a pass — `conftest.py`
    counts it and says what the run is therefore not evidence about.
    """
    interpreter, reason = consumer_python()
    if interpreter is None:
        pytest.skip(
            f"{CONSUMER_SKIP} the stand-in manifest model was NOT checked against the model that "
            f"reads a manifest: {reason}. Nothing in this run is evidence about whether they agree."
        )

    published = sorted((ROOT / "manifests").glob("*/connector.yaml"))
    internal = sorted((ROOT / "manifests-internal").glob("*/connector.yaml"))
    assert published and internal, "no shipped manifests found; has the layout changed?"
    shipped: list[tuple[str, dict[str, object], bool, bool]] = [
        (str(path.relative_to(ROOT)), yaml.safe_load(path.read_text(encoding="utf-8")), True, there)
        for paths, there in ((published, True), (internal, False))
        for path in paths
    ]
    rows = [*_MANIFEST_PROBES, *shipped]

    completed = subprocess.run(
        [str(interpreter), "-c", _CONSUMER_VERDICTS],
        cwd=interpreter.parents[2],
        input=json.dumps([document for _, document, _, _ in rows]),
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0, (
        "the consumer's manifest model could not be asked for a verdict:\n"
        f"{completed.stderr[-3000:]}"
    )
    answer = json.loads(completed.stdout)

    assert answer["max_text_chars"] == MAX_MANIFEST_TEXT_CHARS, (
        f"the consumer caps manifest text at {answer['max_text_chars']} characters and this "
        f"repository's copy says {MAX_MANIFEST_TEXT_CHARS}"
    )
    assert answer["name_pattern"] == CONNECTOR_NAME_PATTERN, (
        f"the consumer requires {answer['name_pattern']!r} of a connector name and this "
        f"repository's copy says {CONNECTOR_NAME_PATTERN!r}"
    )
    for (label, document, here, there), verdict in zip(rows, answer["verdicts"], strict=True):
        assert verdict is there, (
            f"{label}: the consumer's ConnectorManifest "
            f"{'accepted' if verdict else 'refused'} it, and this table says the opposite. Either "
            "that model moved, or this table was wrong about it"
        )
        assert _accepts_here(document) is here, (
            f"{label}: the stand-in model disagrees with its own table, which the shipped "
            "manifests reach through this loop and the probe table reaches through the test above"
        )
