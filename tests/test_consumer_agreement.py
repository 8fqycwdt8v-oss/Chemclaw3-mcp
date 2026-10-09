"""What this fleet declares and `Chemclaw3` consumes, checked from *this* side.

Two facts leave this tree: the manifests `packages/chemclaw_contracts` owns (Chemclaw3 installs
them as the pinned package and keeps no copy), and `servers/calc/tool-surface.json`, the record
Chemclaw3's hardcoded `calc` calls are checked against. A rename done completely here passes
`make check` and fails only in the consumer, so this runs the consumer's own agreement module
rather than a copy that would agree with itself.

Opt-in: without a consumer checkout it skips with the reason; a present checkout missing the
module fails. `.github/workflows/agreement.yml` runs it with `CHEMCLAW3_AGREEMENT_REQUIRED` set,
where a skip fails. It reads the checkout's working tree, not `HEAD`, so a failure may come from
an uncommitted edit there; check `git -C <checkout> status` first. A check deleted in the
consumer is silently deleted here too.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import NoReturn

import chemclaw_contracts as contracts
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

#: The canonical checkout name, in GitHub's casing and the casing a clone usually lands in; both,
#: as `Chemclaw3/infra/live/siblings.sh` searches.
CONSUMER_NAMES = ("Chemclaw3", "chemclaw3")

#: Every environment variable that may name the consumer checkout, most specific first.
CONSUMER_ENV_VARS = ("CHEMCLAW3_REPO", "CHEMCLAW_CORE_REPO")

#: The consumer's module holding the agreement, relative to its checkout. A rename there is a
#: failure here rather than a skip: it is the one part of this arrangement that could go quiet.
AGREEMENT_MODULE = "tests/test_sibling_manifest_agreement.py"

ROOT = Path(__file__).resolve().parents[1]

#: Set by `.github/workflows/agreement.yml`, the lane that clones the consumer and builds it. There
#: the checkout is the point of the job, so a missing one is a broken lane rather than a machine
#: without a sibling — and a skip would report green on exactly the run meant to be the evidence.
REQUIRED_ENV = "CHEMCLAW3_AGREEMENT_REQUIRED"

#: What a failure tells the author to change, in the consumer. Paths in *that* repository, named
#: here because the author of a pull request in this one is the reader who has to open them.
CONSUMER_FILES_TO_UPDATE = (
    "pyproject.toml — the `chemclaw-contracts` pin, moved to the commit of this fleet that carries "
    "the change (the connector manifests reach the consumer only through that package)",
    "tests/test_sibling_manifest_agreement.py — `_ARGUED_DIVERGENCES`, and the per-seam declined "
    "tables for the `calc` and `rxnlabel` backends (`servers/*/tool-surface.json` here)",
)


def skip_unless_required(message: str) -> NoReturn:
    """Skip with `message` — or fail with it, where the lane exists to run this check."""
    if os.environ.get(REQUIRED_ENV):
        pytest.fail(f"{REQUIRED_ENV} is set, so a skip is a failure: {message}")
    pytest.skip(message)


def consumer_repo() -> tuple[Path | None, str]:
    """The `Chemclaw3` checkout, or `None` and the reason there is not one.

    Mirrors `Chemclaw3/infra/live/siblings.sh` (same two roots, same two casings), so this lookup
    cannot resolve differently from the live lanes'.
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

    Its tests import `chemclaw` from its own environment. Separate from `consumer_repo` because
    reading the tree needs only a clone, while running its suite needs its dependencies built.
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

    Read off pytest's counts: returncode 0 covers pass, skip, xfail, xpass and an empty selection
    alike. The rule is positive: at least one pass and nothing inert beside it.
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
    """`inert_outcome` on pytest's real summary lines.

    Every row carries a `passed` count: a row without one is rejected by the pass arm whatever
    `_INERT_OUTCOMES` holds, so it could not tell the widened rule from a blind one. The shape that
    occurs is a pass beside an xfail, skip, xpass or deselection.
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


def test_a_missing_consumer_fails_where_the_lane_requires_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both directions of `skip_unless_required`.

    With `CHEMCLAW3_AGREEMENT_REQUIRED` set (only by the lane that clones and builds the consumer) a
    missing checkout fails, or the job would pass having compared nothing; without it, it skips.
    """
    monkeypatch.setenv(REQUIRED_ENV, "1")
    with pytest.raises(pytest.fail.Exception, match=REQUIRED_ENV):
        skip_unless_required("no checkout")
    monkeypatch.delenv(REQUIRED_ENV)
    with pytest.raises(pytest.skip.Exception, match="no checkout"):
        skip_unless_required("no checkout")


def test_the_consumer_still_agrees_with_the_surface_this_tree_declares() -> None:
    """Run `Chemclaw3`'s agreement suite against *this* checkout, and fail on its failure.

    `CHEMCLAW_MCP_REPO` points at this tree, so this working copy is what is checked. Any inert
    outcome (skip, xfail, xpass, deselection, no pass) fails, since the checkout was supplied.
    `-p no:cacheprovider` and `PYTHONDONTWRITEBYTECODE` leave nothing in the consumer's tree.
    """
    interpreter, reason = consumer_python()
    if interpreter is None:
        skip_unless_required(
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
    update = "".join(f"\n  - {path}" for path in CONSUMER_FILES_TO_UPDATE)
    assert completed.returncode == 0, (
        f"Chemclaw3 no longer agrees with the surface this tree declares.\n{output[-4000:]}\n\n"
        "If this change is meant — a tool added, renamed or reclassified here — the consumer has "
        f"to follow, in Chemclaw3:{update}\n"
        "Open that pull request, and label this one `agreement:core-follows` so the agreement "
        "lane reports without blocking; the consumer's own CI stays red on this fleet's `main` "
        "until it lands (D-2026-09-27-the-fleet-runs-the-consumer-s-agreement-before-merge)."
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
    """The checkout search here looks everywhere the live lanes look.

    All four candidates (two roots, two casings) are driven under a temporary root, rather than
    asserted as names a search might never consult. With no checkout present, the reason must name
    both roots and both variables.
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

    explicit = tmp_path / "named-explicitly"
    explicit.mkdir()
    monkeypatch.setenv(CONSUMER_ENV_VARS[0], str(explicit))
    assert consumer_repo() == (explicit, ""), "the variable must win over the default roots"


# ---------------------------------------------------------------------------------------------
# `mcp_server_kit.testing.ConnectorManifest` is a stand-in for the consumer's manifest model, so
# the refusal happens in this tree; it is held here to the model that actually reads a manifest.
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


def _queued(**queued: object) -> dict[str, object]:
    """A minimal manifest whose endpoint carries `queued` (inline wait 45 s unless overridden)."""
    return _manifest(endpoint={**_ENDPOINT, "queued": {"inline_wait_seconds": 45, **queued}})


#: `(label, document, verdict here, verdict over there)`. Where the columns differ the reason must
#: be one of the deliberate differences `ConnectorManifest`'s docstring lists; verdicts are written
#: per side so a difference has to be typed out.
_MANIFEST_PROBES: tuple[tuple[str, dict[str, object], bool, bool], ...] = (
    ("a plain manifest", _manifest(), True, True),
    # False accepts: both validate in the stand-in and would abort startup in the consumer.
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
    # The consumer's switch for a declared-but-unbound bundle (`pyexec` sets it); refusing it here
    # would be a false refusal.
    ("default_enabled: false", _manifest(default_enabled=False), True, True),
    ("a contract_version", _manifest(contract_version="1.0.0"), True, True),
    ("a contract_version that is not semver", _manifest(contract_version="1.0"), False, False),
    # Still refused on both sides, which is what makes the row above a widening rather than a hole.
    ("an invented key", _manifest(nonsense=["x"]), False, False),
    # Shapes the stand-in once coerced and the consumer refuses: a discriminated endpoint with no
    # tag (`union_tag_not_found`), a bare YAML key (`None`, a `list_type` error), and an endpoint
    # serving nothing (the consumer's classification validator).
    (
        "an endpoint with no transport",
        _manifest(endpoint={k: v for k, v in _ENDPOINT.items() if k != "transport"}),
        False,
        False,
    ),
    (
        "a bare state_changing key",
        _manifest(endpoint={**_ENDPOINT, "state_changing": None}),
        False,
        False,
    ),
    ("a bare top-level skills key", _manifest(skills=None), False, False),
    (
        "an empty tools list",
        _manifest(endpoint={**_ENDPOINT, "tools": [], "read_only": []}),
        False,
        False,
    ),
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
    # `endpoint.queued`: the consumer routes these tools through its interactive queue, and every
    # refusal it makes of the block has to be one this stand-in makes too.
    ("a queued block", _queued(tools=["a_tool"]), True, True),
    ("a queued name twice", _queued(tools=["a_tool", "a_tool"]), False, False),
    ("a queued name not served", _queued(tools=["other_tool"]), False, False),
    ("an empty queued list", _queued(tools=[]), False, False),
    ("a zero inline wait", _queued(tools=["a_tool"], inline_wait_seconds=0), False, False),
    ("an invented queued key", _queued(tools=["a_tool"], nonsense=1), False, False),
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
    """The stand-in model's own verdicts on every probe, which need no checkout.

    Both halves read one table, so they cannot describe different probes.
    """
    for label, document, here, _ in _MANIFEST_PROBES:
        assert _accepts_here(document) is here, (
            f"{label}: mcp_server_kit.testing.ConnectorManifest "
            f"{'refused' if here else 'accepted'} it, and the table says the opposite"
        )


def test_the_stand_in_manifest_model_agrees_with_the_model_that_reads_a_manifest() -> None:
    """Every probe, and every manifest this fleet ships, through the consumer's own model.

    The stand-in does not validate `jobs`, `skills`, `profiles`, `note_types` or `relations` (a
    second `JobSpec` would be a second answer), so shipped manifests go through the real model.
    `manifests-internal/` must be refused there: its `mount:` key is what makes mounting that
    directory a startup error. Skips with the reason when there is no checkout.
    """
    interpreter, reason = consumer_python()
    if interpreter is None:
        skip_unless_required(
            f"{CONSUMER_SKIP} the stand-in manifest model was NOT checked against the model that "
            f"reads a manifest: {reason}. Nothing in this run is evidence about whether they agree."
        )

    published = sorted(contracts.manifests_dir().glob("*/connector.yaml"))
    internal = sorted(contracts.internal_manifests_dir().glob("*/connector.yaml"))
    assert published and internal, "no shipped manifests found; has the layout changed?"
    shipped: list[tuple[str, dict[str, object], bool, bool]] = [
        (
            str(path.relative_to(path.parents[2])),
            yaml.safe_load(path.read_text(encoding="utf-8")),
            True,
            there,
        )
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
