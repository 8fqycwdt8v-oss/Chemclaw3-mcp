"""Repository layout: every server ships the whole file set, is named once, holds a unique port,
has a run target, a catalogue row and a place in the type gate; the directory maps match the tree.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


SERVERS = ROOT / "servers"


PORT_RANGE = range(8850, 8900)


def server_dirs() -> list[Path]:
    """Every server directory — a subdirectory of `servers/` holding a `connector.yaml`."""
    return sorted(path for path in SERVERS.iterdir() if (path / "connector.yaml").is_file())


def manifest_of(server: Path) -> dict[str, object]:
    """One server's parsed manifest."""
    loaded = yaml.safe_load((server / "connector.yaml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


# The files a server is not deployable or reviewable without. Hoisted out of the test below so the
# checklist a new server is written from can be held against it — `docs/adding-a-server.md` is the
# one declaration of this set in prose, and `test_the_required_file_set_is_declared_once` is what
# keeps the two from drifting. A second copy stood in `CLAUDE.md` for months listing barely half of
# these, which is the failure this constant exists to make impossible to repeat quietly.
REQUIRED_SERVER_FILES = (
    "connector.yaml",
    "pyproject.toml",
    "Containerfile",
    "README.md",
    "deploy/networkpolicy.yaml",
    # The two files that make `/metrics` reachable by a scrape. Listed here rather than left to
    # each server's own `test_deploy.py`, because the failure they prevent is a *new* server
    # shipping without them — which its own tests, if it copied a directory that had none,
    # would never notice. `deploy/` held only the NetworkPolicy on every server in this fleet
    # while every one of those policies admitted the monitoring namespace: the hole was open
    # and nothing was told to go through it.
    "deploy/service.yaml",
    "deploy/servicemonitor.yaml",
    # The workload itself, and the file that carries every pod-hardening field — runAsNonRoot,
    # dropped capabilities, seccomp, resource limits, no service-account token. `deploy/` used
    # to ship the NetworkPolicy/Service/ServiceMonitor but *no* Deployment, so nothing
    # in-cluster set any of those and each defaulted to the cluster's — root, all capabilities,
    # unconfined, unbounded. A new server copying a directory without one would inherit that
    # gap silently, which is why the requirement lives here rather than only in each
    # `test_deploy.py`.
    "deploy/deployment.yaml",
    # The two objects that decide whether a capability survives a rollout and whether it has a
    # capacity lever at all. Same argument as the Deployment above, one round later: every
    # server here shipped `replicas: 1` with neither, so a drain took the capability to zero
    # and a full pod had no second pod to overflow into — and because all seven were identical,
    # no server's own tests could see it. `tests/test_deploy_shape.py` checks what is *in*
    # them; this is what checks they exist for a server that copied a directory predating them.
    "deploy/hpa.yaml",
    "deploy/pdb.yaml",
    "tests/test_no_egress.py",
    "tests/test_server.py",
    # The per-server half of layer 4. The fleet-wide half —
    # `tests/test_deploy_shape.py::test_the_egress_policy_denies_and_selects_the_workload` —
    # is what actually closes the hole this line was missing from: until it existed, a server
    # could ship a NetworkPolicy permitting all outbound traffic and no `test_deploy.py`, and
    # the whole suite stayed green. This entry is the *other* half and is not redundant with
    # it: a server's own file is where its port, its ingress peers and the Service-to-
    # ServiceMonitor port *name* are held, and those are numbers and strings belonging to one
    # server that no fleet-wide reader can derive. Listed here for the same reason
    # `deploy/deployment.yaml` and `deploy/hpa.yaml` are: the failure is a *new* server copying
    # a directory that predates the file, whose own tests then cannot notice what it does not
    # have.
    "tests/test_deploy.py",
)


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_a_server_ships_the_whole_set(server: Path) -> None:
    """A server is not just code: without any one of these it cannot be deployed or reviewed."""
    for required in REQUIRED_SERVER_FILES:
        assert (server / required).exists(), f"{server.name} is missing {required}"


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_the_name_is_one_string_used_four_times(server: Path) -> None:
    """Directory, manifest `name`, package suffix and the key Chemclaw3 dials must all agree."""
    name = manifest_of(server)["name"]
    assert name == server.name, f"{server.name}/connector.yaml calls itself {name!r}"
    package = server / "src" / f"chemclaw_mcp_{server.name.replace('-', '_')}"
    assert package.is_dir(), f"expected the package at {package}"


def test_ports_are_unique_and_inside_this_repository_s_block() -> None:
    """Every served port is unique and inside 8850-8899, which is all this repository can check.

    Why the block *is* 8850-8899 — that everything else in the family was observed below it — is
    recorded in `CLAUDE.md` as a dated reason rather than asserted here. It is a fact about
    repositories this suite cannot see, and the version of it that lived in prose as a boundary was
    wrong for as long as it existed: it published Chemclaw3's connectors as 8810-8815 while `bo` sat
    on 8816.
    """
    seen: dict[int, str] = {}
    for server in server_dirs():
        endpoint = manifest_of(server)["endpoint"]
        assert isinstance(endpoint, dict)
        found = re.search(r":(\d+)/mcp", str(endpoint["url"]))
        assert found, f"{server.name}: cannot read a port out of {endpoint['url']!r}"
        port = int(found.group(1))
        assert port in PORT_RANGE, f"{server.name} claims {port}, outside 8850-8899"
        assert port not in seen, f"{server.name} and {seen[port]} both claim port {port}"
        seen[port] = server.name


def test_every_server_has_a_run_target_on_the_port_its_manifest_publishes() -> None:
    """`CLAUDE.md` publishes `make run-safety  # one per server`, so there is one per server."""
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    for server in server_dirs():
        endpoint = manifest_of(server)["endpoint"]
        assert isinstance(endpoint, dict)
        published = re.search(r":(\d+)/mcp", str(endpoint["url"]))
        assert published, f"{server.name}: cannot read a port out of {endpoint['url']!r}"
        target = re.search(
            rf"^run-{re.escape(server.name)}:.*?(?=^\S|\Z)", makefile, re.MULTILINE | re.DOTALL
        )
        assert target, (
            f"no `run-{server.name}` target in the Makefile; `CLAUDE.md` says there is one per "
            "server, and a reader who believes it goes looking for the port by hand"
        )
        assert f"--port {published.group(1)}" in target.group(0), (
            f"`run-{server.name}` does not start it on {published.group(1)}, which is the port its "
            "own manifest publishes"
        )


def test_the_scripts_map_lists_everything_beside_it() -> None:
    """`scripts/README.md` is a map, and the top-level check only asks that the README exists.

    `CLAUDE.md`'s row said `scripts/` holds "today, the offline check", and `scripts/README.md`
    listed that one file — while `calibrate_rxnpredict_priors.py` has sat beside it since
    2026-08-12. Both documents were edited in the commit that this check follows, and neither was
    read against the directory. This is `test_the_docs_map_lists_everything_beside_it` one folder
    over, for the same reason: a map nobody verifies is read, believed, and wrong.
    """
    scripts = ROOT / "scripts"
    listed = set(re.findall(r"`([^`]+\.py)`", (scripts / "README.md").read_text(encoding="utf-8")))
    present = {path.name for path in scripts.iterdir() if path.suffix == ".py"}
    unlisted = sorted(present - listed)
    assert not unlisted, f"present in scripts/ and not named in scripts/README.md: {unlisted}"
    stale = sorted(name for name in listed if not (scripts / name).exists())
    assert not stale, f"named in scripts/README.md and not present: {stale}"


def test_the_map_and_the_tree_agree() -> None:
    """Every top-level directory has a README and a row in CLAUDE.md, and vice versa.

    Chemclaw3 asks for exactly this and enforces it, having twice found a README asserting a
    structure the tree no longer had. Prose about a directory layout is worth what the check behind
    it is worth.
    """
    guidance = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    for directory in sorted(path for path in ROOT.iterdir() if path.is_dir()):
        # Dot- and dunder-prefixed directories are tooling or build output (`.github`, `.venv`,
        # `__pycache__`) rather than parts of the repository's structure, and they are gitignored.
        if directory.name.startswith((".", "__")):
            continue
        assert (directory / "README.md").is_file(), f"{directory.name}/ has no README.md"
        assert f"`{directory.name}/" in guidance, f"{directory.name}/ has no row in CLAUDE.md"


def test_every_server_is_wired_into_the_type_gate() -> None:
    """`make type` (what CI's `Types` step and `make check` both run) must see every server.

    This recurred once already for `servers/safety/src` — the CI workflow's own comment records it
    — and the fix (centralising on `make type` instead of a hardcoded path list in CI) only moved
    the drift one level down: the Makefile's own `SRC` variable then silently dropped
    `servers/rxnlabel/src` and `servers/rxnpredict/src`, and `rxnlabel` sat with five real
    `mypy --strict` errors CI had never run against it. `docs/adding-a-server.md`'s checklist never
    mentions this step, which is why it keeps not happening — so this is the check, not the prose.
    """
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    src_line = next(line for line in makefile.splitlines() if line.startswith("SRC :="))
    make_src = set(src_line.removeprefix("SRC :=").split())

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    mypy_path_line = next(
        line for line in pyproject.splitlines() if line.strip().startswith("mypy_path")
    )
    mypy_path = set(mypy_path_line.split("=", 1)[1].strip().strip('"').split(":"))

    for server in server_dirs():
        src = server / "src"
        if not src.is_dir():
            continue
        expected = f"servers/{server.name}/src"
        assert expected in make_src, f"Makefile's SRC is missing {expected} — make type skips it"
        assert expected in mypy_path, f"pyproject.toml's mypy_path is missing {expected}"


def _tracked_python_files() -> list[Path]:
    """Every `.py` file this repository ships — tracked, or newly written and not ignored.

    `git ls-files` rather than a filesystem walk with a list of directories to prune. The criterion
    wanted is "what this repository ships", `.gitignore` already states it for `.venv`, the three
    caches and every build artefact, and a prune list written here would be the second declaration
    of that — the hand list the check below exists to stop using, one layer down. `--others
    --exclude-standard` includes a file somebody has just written and not staged, which is exactly
    when the gate most needs to notice it; a tracked file that has been deleted is dropped, because
    a path with nothing behind it is not something mypy can fail to read.
    """
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "*.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [
        (ROOT / name).resolve() for name in listed.split("\0") if name and (ROOT / name).exists()
    ]


def test_the_type_gate_reads_the_test_tree_and_not_only_the_source() -> None:
    """Every `.py` this repository ships sits under some root on the command `make type` runs."""
    printed = subprocess.run(
        ["make", "-n", "type"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    command = next(
        line
        for line in printed.splitlines()
        if " mypy " in line and not line.lstrip().startswith("#")
    )
    arguments = shlex.split(command)

    assert "--explicit-package-bases" in arguments, (
        'without it `make type` dies on `Duplicate module named "test_no_egress"` before it '
        "checks anything, so the flag is part of the gate rather than a preference"
    )

    roots = [
        (ROOT / argument).resolve()
        for argument in arguments
        if not argument.startswith("-") and (ROOT / argument).exists()
    ]
    ungated = sorted(
        str(source.relative_to(ROOT))
        for source in _tracked_python_files()
        if not any(source == root or root in source.parents for root in roots)
    )
    assert not ungated, (
        f"`make type` never reads {ungated}. Every ratchet in this repository lives in a Python "
        "file, and one mypy never reads can stop meaning what it says in silence. Put the "
        "directory on `SRC` or `TESTS` — and if it is deliberately out, that is a decision for a "
        "record and a named exemption here, not a silence."
    )


def test_the_type_gate_narrows_no_check_it_was_argued_out_of() -> None:
    """`[tool.mypy]` does not carry the narrowing that was measured and rejected."""
    import tomllib

    # Nothing outranks the table this function is about to read. mypy's configuration discovery is
    # `mypy.ini`, then `.mypy.ini`, then `pyproject.toml` — documented and fixed upstream, so this
    # is an enumeration of somebody else's constant rather than of the ways to say "check less",
    # which is the distinction that makes it a control and not a list. Driven: a root `mypy.ini`
    # holding `disable_error_code = arg-type` reds nothing else here, because `arg-type` is a code
    # no line of `_TYPE_GATE_CANARY` violates and the execution check below is a floor rather than a
    # proof of strictness. `setup.cfg` ranks *after* `pyproject.toml`, and a driven `[mypy]` section
    # in one changes no answer, so it is not asserted against: a file that cannot win is not a hole.
    for shadowing in ("mypy.ini", ".mypy.ini"):
        assert not (ROOT / shadowing).exists(), (
            f"{shadowing} wins mypy's config discovery over `pyproject.toml`, so every assertion "
            "below is about a table mypy never reads. Put the configuration in `[tool.mypy]`"
        )

    mypy = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["mypy"]
    assert mypy.get("strict") is True, (
        "`[tool.mypy] strict = true` is the gate. Without it `make type` runs a different and "
        "quieter check under the same name"
    )
    narrowed = [
        section
        for section in (mypy, *mypy.get("overrides", []))
        if section.get("disable_error_code") or section.get("warn_unused_ignores") is False
    ]
    assert not narrowed, (
        f"`[tool.mypy]` narrows the gate: {narrowed}. The narrowing was measured and rejected — it "
        "manufactures `unused-ignore` findings in serving code — so re-taking it is a decision for "
        "a record, not a configuration key"
    )

    # The second and last verbatim copy of "print the recipe, find the mypy line", and it stays a
    # copy on purpose: **two callers, and this repository's rule is no abstraction without a third**
    # (`CLAUDE.md`'s Rule of Three, and an abstraction with one caller gets inlined). The reviewer
    # who flagged the duplication measured the same boundary and left it there. It nearly became
    # three: `test_a_planted_error_in_a_gated_file_reds_make_type` was the obvious third caller and
    # is not, because it runs `make type` rather than `make -n type` — the exit code is exactly what
    # a dry run cannot show, and that is the whole point of it. If a genuine third reader of the
    # printed recipe arrives, extract then.
    printed = subprocess.run(
        ["make", "-n", "type"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    command = next(
        line
        for line in printed.splitlines()
        if " mypy " in line and not line.lstrip().startswith("#")
    )
    disabled = [
        argument for argument in shlex.split(command) if argument.startswith("--disable-error-code")
    ]
    assert not disabled, (
        f"the `type:` recipe passes {disabled}, which takes the same narrowing the record rejected "
        "and puts it where nobody reviewing `pyproject.toml` will see it"
    )


# A module that violates four checks at once, planted into the tree and removed again by the test
# below. Each line is here because a *different* relaxation silences it, driven one at a time:
# `--allow-untyped-defs` (or `strict = false`) takes `no-untyped-def`, `--no-strict-optional` takes
# `assignment`, `--no-warn-unused-ignores` (or `strict = false`) takes `unused-ignore`, and
# `--disable-error-code=return-value` takes `return-value`. Four codes rather than one because the
# assertion is that *each* is reported: a canary with a single error proves only that mypy still
# runs, and `make type` would stay red under every flag the deleted deny-list used to name.
_TYPE_GATE_CANARY = '''"""Planted by the type gate's execution check, and removed by it.

If this file is in a checkout, `test_a_planted_error_in_a_gated_file_reds_make_type` died between
planting and its `finally`. Delete it; nothing imports it.
"""


def gate_canary_untyped():
    """No annotations: `--strict`'s `disallow_untyped_defs`."""
    return 1


def gate_canary_return_value() -> int:
    """A `str` where an `int` is declared: mypy's base configuration reports this."""
    return "not an int"


gate_canary_optional: int = None
gate_canary_unused: int = 1  # type: ignore[assignment]
'''


# One gated `src/` root and one gated `tests/` root, named rather than derived. Derivation is what
# `test_the_type_gate_reads_the_test_tree_and_not_only_the_source` does, and it is the wrong tool
# here: a canary planted under whatever the command happens to name is checked by definition, which
# is the "agrees with itself forever" basis that test's own docstring warns about. These two paths
# are checked because they are two real places in this tree, and if either stops being read the
# assertion below says so.
_TYPE_GATE_CANARY_PATHS = (
    Path("packages/mcp_server_kit/src/mcp_server_kit/_type_gate_canary.py"),
    Path("tests/_type_gate_canary.py"),
)


_TYPE_GATE_CANARY_CODES = ("no-untyped-def", "return-value", "assignment", "unused-ignore")


def test_a_planted_error_in_a_gated_file_reds_make_type() -> None:
    """Run the real recipe against a known error and require it to be reported."""
    for path in _TYPE_GATE_CANARY_PATHS:
        (ROOT / path).write_text(_TYPE_GATE_CANARY, encoding="utf-8")
    try:
        run = subprocess.run(
            ["make", "type"], cwd=ROOT, capture_output=True, text=True, check=False
        )
    finally:
        for path in _TYPE_GATE_CANARY_PATHS:
            (ROOT / path).unlink(missing_ok=True)
    reported = run.stdout + run.stderr

    assert run.returncode != 0, (
        "`make type` exited 0 with a deliberately ill-typed module in two gated directories. The "
        f"gate reports nothing about them:\n{reported}"
    )
    for path in _TYPE_GATE_CANARY_PATHS:
        for code in _TYPE_GATE_CANARY_CODES:
            assert any(
                str(path) in line and f"[{code}]" in line for line in reported.splitlines()
            ), (
                f"`make type` did not report [{code}] in {path}. Every line of the planted module "
                "is violated under the configuration this repository ships, so a missing code is a "
                "check that has been turned off somewhere — the configuration, an override, a "
                f"`mypy.ini`, or the recipe.\n{reported}"
            )


def test_every_server_appears_in_the_catalogue() -> None:
    """A server the catalogue has never heard of is one nobody can find or plan around."""
    catalogue = (ROOT / "MODULES.md").read_text(encoding="utf-8")
    for server in server_dirs():
        assert f"`{server.name}`" in catalogue, f"{server.name} is missing from MODULES.md"


def test_the_catalogue_claims_no_port_a_server_contradicts() -> None:
    """The other direction: MODULES.md is the port registry, so it must agree with the manifests."""
    catalogue = (ROOT / "MODULES.md").read_text(encoding="utf-8")
    for server in server_dirs():
        endpoint = manifest_of(server)["endpoint"]
        assert isinstance(endpoint, dict)
        port = re.search(r":(\d+)/mcp", str(endpoint["url"]))
        assert port is not None
        pattern = rf"`{re.escape(server.name)}`[^\n]*{port.group(1)}"
        assert re.search(pattern, catalogue), (
            f"MODULES.md does not record port {port.group(1)} for {server.name}"
        )


def test_the_coverage_basis_is_every_distribution_this_workspace_ships() -> None:
    """A floor that silently narrows is worse than a lower floor."""
    import tomllib

    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    measured = set(config["tool"]["coverage"]["run"]["source_pkgs"])

    shipped = {f"chemclaw_mcp_{server.name}" for server in server_dirs()}
    shipped |= {
        package.name
        for package in (ROOT / "packages").iterdir()
        if (package / "pyproject.toml").is_file()
    }

    missing = sorted(shipped - measured)
    assert not missing, (
        f"{missing} ship in this workspace and are outside the coverage floor, so their "
        "statements are not counted and a package with no tests at all would not move the number. "
        "Add them to `[tool.coverage.run] source_pkgs` and re-measure the floor — the percentage "
        "changes when the basis does, so this is a measurement before it is an edit."
    )
    stale = sorted(measured - shipped)
    assert not stale, (
        f"{stale} are named in the coverage basis and ship nowhere in this workspace. Coverage "
        "over a package that does not exist is silently zero-weighted, which flatters the total."
    )


def test_the_docs_map_lists_everything_beside_it() -> None:
    """`docs/README.md` links every entry beside it and nothing absent; subtrees have a README."""
    docs = ROOT / "docs"
    listed = set(
        re.findall(r"\]\((?!\.\./|https?://)([^)#]+)\)", (docs / "README.md").read_text("utf-8"))
    )
    present = {
        path.name + ("/" if path.is_dir() else "")
        for path in docs.iterdir()
        if path.name != "README.md"
    }
    unlisted = sorted(n for n in present if n not in listed and n.rstrip("/") not in listed)
    assert not unlisted, f"present in docs/ and not linked from docs/README.md: {unlisted}"
    stale = sorted(link for link in listed if not (docs / link).exists())
    assert not stale, f"linked from docs/README.md and not present: {stale}"
    for path in sorted(docs.iterdir()):
        if path.is_dir():
            assert (path / "README.md").exists(), f"docs/{path.name}/ has no README.md"
