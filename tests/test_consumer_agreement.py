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

import os
import subprocess
from pathlib import Path

import pytest

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


def test_the_consumer_still_agrees_with_the_surface_this_tree_declares() -> None:
    """Run `Chemclaw3`'s agreement suite against *this* checkout, and fail on its failure.

    `CHEMCLAW_MCP_REPO` is set to this tree rather than left to that repository's own search, so
    what is under test is this working copy and not whichever fleet checkout happens to sit beside
    it. A **skip** over there is a failure here for the same reason: the checkout was supplied, so
    a skipped run means the module could not read what it was pointed at.

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
    assert " skipped" not in output and "no tests ran" not in output, (
        "the consumer's agreement suite made no assertion against this checkout, although "
        f"CHEMCLAW_MCP_REPO named {ROOT}. A skip there is not a pass here — it means that module "
        f"could not read what it was pointed at.\n{output[-4000:]}"
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
