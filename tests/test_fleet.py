"""Fleet-level invariants — the ones no single server can see about itself.

Each server's own tests check that server. Nothing there can notice that two servers claimed the
same port, that a manifest was copied into `manifests/` instead of symlinked and has since drifted,
or that a server exists which `MODULES.md` has never heard of. Those are exactly the failures that
appear once, in a deployment, months later.

The shape is Chemclaw3's `tests/test_repo_map.py`: check the declarations against the directories
on disk, **in both directions**, because a one-way check passes happily while the tree grows things
the documentation does not know about.

One check here is about *data* rather than structure, and it belongs here for the same reason: two
servers carrying a density for THF is a fact neither of them can see, and a chemist who is told
0.889 by one and 0.886 by the other has been given two answers to one question — which is what this
repository's central rule forbids, whether the second answer comes from a second implementation or
from a second table.
"""

from __future__ import annotations

import ast
import json
import re
import shlex
from pathlib import Path
from typing import NamedTuple

import pytest
import yaml
from mcp_server_kit.egress import GUARD_DISABLED_VALUES

ROOT = Path(__file__).resolve().parents[1]
SERVERS = ROOT / "servers"
MANIFESTS = ROOT / "manifests"
INTERNAL_MANIFESTS = ROOT / "manifests-internal"
PORT_RANGE = range(8850, 8900)
# What a server declares itself to be, and where its symlink therefore belongs. `connector` is the
# default and is written nowhere: Chemclaw3's `ConnectorManifest` is `extra="forbid"`, so a `mount:`
# key on a manifest it reads would abort its startup. That asymmetry is the design — see
# `test_a_backend_declares_itself_in_a_key_chemclaw3_refuses`.
MOUNTS = {"connector": MANIFESTS, "backend": INTERNAL_MANIFESTS}


def server_dirs() -> list[Path]:
    """Every server directory — a subdirectory of `servers/` holding a `connector.yaml`."""
    return sorted(path for path in SERVERS.iterdir() if (path / "connector.yaml").is_file())


def manifest_of(server: Path) -> dict[str, object]:
    """One server's parsed manifest."""
    loaded = yaml.safe_load((server / "connector.yaml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_a_server_ships_the_whole_set(server: Path) -> None:
    """A server is not just code: without any one of these it cannot be deployed or reviewed."""
    for required in (
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
    ):
        assert (server / required).exists(), f"{server.name} is missing {required}"


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_the_name_is_one_string_used_four_times(server: Path) -> None:
    """Directory, manifest `name`, package suffix and the key Chemclaw3 dials must all agree."""
    name = manifest_of(server)["name"]
    assert name == server.name, f"{server.name}/connector.yaml calls itself {name!r}"
    package = server / "src" / f"chemclaw_mcp_{server.name.replace('-', '_')}"
    assert package.is_dir(), f"expected the package at {package}"


def declared_mount(server: Path) -> str:
    """What this server says it is: a connector Chemclaw3 dials, or a backend it calls."""
    mount = manifest_of(server).get("mount", "connector")
    assert mount in MOUNTS, f"{server.name} declares mount: {mount!r}; expected one of {[*MOUNTS]}"
    return str(mount)


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_the_manifest_is_registered_by_symlink_in_the_bucket_it_declares(server: Path) -> None:
    """Two directories, because two of these servers must never be mounted as connectors.

    `manifests/` is the directory every published `export CHEMCLAW_CONNECTORS_DIR=...` line in this
    repository names, and Chemclaw3 discovers a bundle as any subdirectory of it holding a
    `connector.yaml`. Discovery is enablement unless `connectors_enabled` narrows it, so anything
    registered there is a capability the agent gets.

    Two servers here are not that. `calc` is a **backend** Chemclaw3 calls from inside
    `science/calc/store.py::cached_compute` on a cache miss, and `rxnlabel` is reached by plain
    configuration from a background drain. Both carry a Chemclaw3 bundle's name or an internal
    primitive's tools, and both said so only in prose — in block capitals, in the same files that
    supplied the copy-pasteable `export` that mounts them. Measured with Chemclaw3's own
    `_bundle_dirs()` and that exact line: `calc -> Chemclaw3-mcp/manifests/calc` won the collision,
    which removes `report_measurement`, `find_calculations`, `list_artifacts`, `fetch_artifact`,
    `calculator_trust`, `calculator_outliers`, `compute_thermochemistry` and all twelve durable calc
    jobs from the agent's surface, with no error at any point.

    So the distinction is a directory rather than a paragraph: a copy in the wrong bucket, or in
    both, fails here. This repository's own rule — a README is not a gate — applied to itself.
    """
    bucket = MOUNTS[declared_mount(server)]
    registered = bucket / server.name / "connector.yaml"
    assert registered.is_symlink(), (
        f"{registered} must be a symlink to the server's own manifest, not a copy — two copies of "
        "one declaration is how a manifest outlives the surface it describes"
    )
    assert registered.resolve() == (server / "connector.yaml").resolve()
    for other in MOUNTS.values():
        if other != bucket:
            assert not (other / server.name).exists(), (
                f"{server.name} is registered in both {bucket.name}/ and {other.name}/; exactly "
                "one of them is what a deployment mounts"
            )


def test_the_directory_the_export_line_names_holds_only_connectors() -> None:
    """Chemclaw3's discovery, replicated over `manifests/`: everything it finds must be dialable.

    The other direction of the test above, and the one that catches a directory appearing in
    `manifests/` that no `servers/` entry claims. `_bundle_dirs` is non-recursive and reads any
    subdirectory holding a `connector.yaml`, so that is exactly what is enumerated here.
    """
    found = sorted(path.name for path in MANIFESTS.iterdir() if (path / "connector.yaml").is_file())
    connectors = sorted(s.name for s in server_dirs() if declared_mount(s) == "connector")
    assert found == connectors, (
        f"mounting manifests/ would give Chemclaw3 {found}; the connectors are {connectors}"
    )


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_a_backend_declares_itself_in_a_key_chemclaw3_refuses(server: Path) -> None:
    """The second layer: a backend's manifest cannot be loaded as a connector even if it is found.

    A directory split is only as good as the path an operator types, so the declaration is also the
    thing that makes the mistake loud. Chemclaw3's `ConnectorManifest` is `extra="forbid"`, so
    `mount: backend` is refused by name — measured against Chemclaw3 itself:

        ConnectorError: .../calc/connector.yaml: invalid manifest: 1 validation error for
        ConnectorManifest / mount / Extra inputs are not permitted

    and `registry.discovered()` loads every manifest it finds, so that is a startup error naming
    the file rather than a capability quietly swapped. The same property is why a **connector's**
    manifest must not carry the key at all: it would abort the startup of the deployments that are
    supposed to mount it.
    """
    declared = manifest_of(server)
    if declared_mount(server) == "connector":
        assert "mount" not in declared, (
            f"{server.name} is mounted as a connector, so its manifest must carry no `mount:` key "
            "— Chemclaw3's ConnectorManifest forbids extra keys and would refuse to start"
        )
    else:
        assert declared["mount"] == "backend", (
            f"{server.name} is not a connector, so its manifest must say so in a key Chemclaw3 "
            "refuses, and not only in a comment"
        )


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_every_tool_is_classified_exactly_once(server: Path) -> None:
    """The rule Chemclaw3's HttpEndpoint enforces (D-167). Omission fails *open* at the gate."""
    endpoint = manifest_of(server)["endpoint"]
    assert isinstance(endpoint, dict)
    tools = set(endpoint.get("tools", []))
    read_only = set(endpoint.get("read_only", []))
    state_changing = set(endpoint.get("state_changing", []))
    assert tools, f"{server.name} declares no tools"
    assert not (tools - read_only - state_changing), f"{server.name}: unclassified tools"
    assert not (read_only & state_changing), f"{server.name}: tools classified twice"
    assert not ((read_only | state_changing) - tools), f"{server.name}: classified an unserved tool"


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_a_networked_manifest_carries_a_credential(server: Path) -> None:
    """Bearer even on the loopback dev URL — an auth mode that changes with the address gets
    forgotten on the serving side the day the address changes."""
    endpoint = manifest_of(server)["endpoint"]
    assert isinstance(endpoint, dict)
    auth = endpoint.get("auth", {})
    assert auth.get("mode") == "bearer", f"{server.name} must declare bearer auth"
    assert auth.get("token_env"), f"{server.name} declares bearer with no token_env"


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
    """`CLAUDE.md` publishes `make run-safety  # one per server`, so there is one per server.

    Measured on 2026-09-12 that sentence was false in the direction that wastes a reader's time:
    five `run-*` targets for seven servers, with `rxnlabel` and `rxnpredict` — the two whose local
    dev address is hardest to guess, because neither appears in `manifests/` beside the others —
    having none. The claim is now the cheaper half to make true, and this is what keeps it true.

    The port is checked against the manifest rather than transcribed, for the reason `MODULES.md`
    is the only port registry: a target that starts a server on a port nothing addresses it by is a
    second declaration, and this repository has already published one of those.
    """
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


def test_no_server_registers_a_resource_or_a_prompt() -> None:
    """`connector_app`'s caller-rebind and error-sanitize only cover `server._tool_manager`.

    `_bind_caller_per_tool_call`/`_sanitize_tool_errors` (`mcp_server_kit/app.py`) both patch
    `manager.call_tool` — the one FastMCP dispatch that a live attribute lookup inside the SDK's own
    registered handler reads afresh on every call. `FastMCP.read_resource`/`get_prompt` are *not*
    that shape: the lowlevel server captures the bound method object at `_setup_handlers()`, inside
    `FastMCP.__init__`, before `connector_app()` ever sees the instance — so the same "monkeypatch
    the attribute afterward" trick that works for tools would silently do nothing for a resource or
    a prompt, and `read_resource`'s own exception handler
    (`except Exception as e: raise ResourceError(str(e))`, no `ValueError` exemption) would reach
    the calling model verbatim, and `current_caller()` inside a resource/prompt body would read the
    *handshake's* identity rather than the request being served.

    So: nobody may add one until the kit actually covers it. This is the guard rail, not a
    permanent decision — extending `_bind_caller_per_tool_call`/`_sanitize_tool_errors` (which needs
    a real registration to develop and verify against, not a guess at SDK internals with nothing
    live to test it on) is what turns this red into a real fix.
    """
    for server in server_dirs():
        src = server / "src"
        if not src.is_dir():
            continue
        for path in src.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    assert node.func.attr not in {"resource", "prompt"}, (
                        f"{path.relative_to(ROOT)}:{node.lineno} calls .{node.func.attr}(...) — "
                        "connector_app's caller-rebind and error-sanitize do not cover resources "
                        "or prompts yet (see this test's docstring)"
                    )


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


def test_claude_md_holds_no_second_port_registry() -> None:
    """`MODULES.md` is the registry. A second table that no test reads goes stale and is believed.

    One did: `CLAUDE.md` listed five servers when seven were built and advertised two ranges as
    free over ports `rxnlabel` (8865) and `pyexec` (8899) already held. `test_the_catalogue_...`
    above enforces `MODULES.md` against the manifests and looks at nothing else, so the stale table
    was invisible to the suite and first to a reader.

    Asserted as "no server name paired with a port", not "no port literal", because the *rule* —
    the 8850-8899 block, and why it starts there — is prose worth keeping there.
    """
    guidance = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    for server in server_dirs():
        endpoint = manifest_of(server)["endpoint"]
        assert isinstance(endpoint, dict)
        port = re.search(r":(\d+)/mcp", str(endpoint["url"]))
        assert port is not None
        name, number = re.escape(server.name), port.group(1)
        # Either order on one line: the table this replaced put the port first.
        pattern = rf"(`{name}`[^\n]*\b{number}\b)|(\b{number}\b[^\n]*`{name}`)"
        assert not re.search(pattern, guidance), (
            f"CLAUDE.md pairs {server.name} with port {port.group(1)}; MODULES.md is the registry "
            "and the only file tests/test_fleet.py checks, so a second copy here goes stale"
        )


def test_the_two_tables_that_both_hold_densities_agree() -> None:
    """`props` and `chem` both record ambient densities. They must not disagree about a solvent.

    The overlap is real and neither table is wrong to have it: `props` answers "what is this
    solvent like", `chem` needs a number to turn 10 volumes into a mass. What is forbidden is the
    two drifting, because the failure is silent in the worst possible way — a charge table computed
    from one density beside a solvent sheet quoting the other, with nothing on either saying they
    came from different files.

    Matched on canonical SMILES rather than on a name, so `2-MeTHF` and `2-methyltetrahydrofuran`
    are compared rather than skipped. The tolerance is 1%: these are handbook values at "20-25 °C",
    and demanding equality would fail on the temperature the compiler happened to quote.
    """
    from chemclaw_mcp_chem.engine.chem import require_canonical_smiles
    from chemclaw_mcp_chem.engine.reagents import dataset as chem_dataset
    from chemclaw_mcp_props.engine import records
    from mcp_server_kit import read_records

    props_densities = {
        require_canonical_smiles(solvent.smiles): (solvent.name, solvent.density_20c)
        for solvent in records.all_solvents()
    }
    compared = 0
    for row in read_records(chem_dataset()):
        raw = row["density_g_per_ml"].strip()
        if not raw:
            continue
        found = props_densities.get(require_canonical_smiles(row["smiles"]))
        if found is None:
            continue
        name, density = found
        compared += 1
        assert abs(float(raw) - density) / density < 0.01, (
            f"chem says {raw} g/mL for {row['name']} and props says {density} for {name}; "
            "one solvent, two answers"
        )
    assert compared >= 20, f"only {compared} solvents overlap — did a table lose its densities?"


def test_the_reagent_table_two_servers_carry_is_one_file() -> None:
    """`chem` and `safety` both ship the bench-reagent corpus. It must be the *same* corpus.

    This is the density check's sibling and it exists for a stronger version of the same reason. One
    server never imports another, so a table two servers need is carried by both — `chem` resolves
    the name a chemist wrote into a structure, `safety` needs the same resolution to get `THF`,
    `2-MeTHF` and `C1CCOC1` to an ICH row. Neither server can see the other's copy, so neither can
    notice the day they stop agreeing, and the failure is the one this repository's central rule
    forbids: two answers to one question, with nothing on either saying they came from different
    files.

    Byte-identity rather than a tolerance, because unlike the densities these are not independently
    compiled numbers — one was copied from the other, and anything less than equality is drift. If
    the two ever have to diverge, this test is where the argument for it gets written down.
    """
    copies = [
        SERVERS / "chem" / "src" / "chemclaw_mcp_chem" / "data",
        SERVERS / "safety" / "src" / "chemclaw_mcp_safety" / "data" / "reagents",
    ]
    records = {path: (path / "records.csv").read_bytes() for path in copies}
    manifests = {path: (path / "dataset.json").read_bytes() for path in copies}
    assert len(set(records.values())) == 1, f"the reagent tables differ: {list(records)}"
    assert len(set(manifests.values())) == 1, f"the reagent manifests differ: {list(manifests)}"


def test_every_server_builds_a_wheel_that_carries_its_data() -> None:
    """A server that cannot be packaged cannot be deployed, and nothing else here would notice.

    **Four of the five servers could not build a wheel at all**, and the fleet was green throughout:
    `make check`, `offline-run` and every per-server suite run from the source tree, where a
    `data/` directory is simply a directory. Only building a distribution reveals that
    `[tool.hatch.build.targets.wheel] packages = ["src/chemclaw_mcp_<name>"]` already includes
    everything beneath it, so the `force-include` of `.../data` added each corpus a **second** time
    and hatchling refused:

        ValueError: A second file is being added to the wheel archive at the same path:
        `chemclaw_mcp_safety/data/genotox/dataset.json`.

    `calc` was the only one that built, because it is the only server with no vendored data and
    therefore never had the redundant entry. So the gap was invisible in exactly the servers whose
    whole point is the corpus baked into their image — including the two Chemclaw3 dials in
    production.

    **Both halves are asserted, and the second is the dangerous one.** Removing a `force-include`
    to make a build pass would be a silent catastrophe if the data then stopped shipping: a hazard
    screen with no `rules.yaml` answers "no rule matched" for every molecule, which reads as *safe*.
    So this counts the corpus files inside the built wheel rather than trusting that the packaging
    change was equivalent.
    """
    import subprocess
    import tempfile
    import zipfile

    for server in sorted(p for p in SERVERS.iterdir() if (p / "pyproject.toml").is_file()):
        data_dir = next(iter((server / "src").glob("*/data")), None)
        with tempfile.TemporaryDirectory() as out:
            built = subprocess.run(
                ["uv", "build", "--wheel", "--out-dir", out, str(server)],
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            assert built.returncode == 0, (
                f"{server.name} cannot be packaged, so it cannot be deployed:\n{built.stderr}"
            )
            wheels = list(Path(out).glob("*.whl"))
            assert len(wheels) == 1, f"{server.name} built {len(wheels)} wheels"
            names = zipfile.ZipFile(wheels[0]).namelist()

        if data_dir is None:
            continue
        on_disk = sum(1 for p in data_dir.rglob("*") if p.is_file())
        in_wheel = sum(1 for n in names if "/data/" in n)
        assert in_wheel == on_disk, (
            f"{server.name} ships {in_wheel} of its {on_disk} data files; a server whose corpus "
            "is missing answers 'nothing matched' for every input, which reads as a clean result"
        )


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_the_image_can_name_the_build_it_is(server: Path) -> None:
    """Every Containerfile threads a build argument into `MCP_SERVER_REVISION`.

    `mcp_server_kit.app.server_revision` reads that variable and stamps it onto the `initialize()`
    handshake's `serverInfo.version` and onto `/healthz`, which is how Chemclaw3's audit row learns
    *which build of which server* computed a number — the provenance hole that opened the moment
    the chemistry left that repository and started shipping on its own cadence.

    **This asserts the supply, not the read**, and that distinction is the whole reason the test
    exists. Chemclaw3 shipped this exact field once (D-057) and it read `"unknown"` for eight
    months: the function, the column and the test were all present and correct, and no build ever
    set the variable. A default that is a plausible answer cannot fail loudly, so the only place
    the omission is visible is here, in the file that would have had to supply it.
    """
    text = (server / "Containerfile").read_text(encoding="utf-8")
    assert "ARG CHEMCLAW_REVISION" in text, (
        f"{server.name}/Containerfile declares no CHEMCLAW_REVISION build argument, so every "
        "image it builds reports its revision as 'unknown'"
    )
    assert "ENV MCP_SERVER_REVISION=${CHEMCLAW_REVISION}" in text, (
        f"{server.name}/Containerfile takes a revision argument and never puts it in the "
        "environment, which is the only place the server reads it"
    )


def test_the_revision_reaches_the_handshake_and_the_probe() -> None:
    """The other half: the value an image supplies is the one a client and a probe see.

    Kept in the fleet file rather than in one server's, because it is a claim about `connector_app`
    — every server's front door — and asserting it once per server would be five copies of one
    fact. It reaches through `FastMCP._mcp_server`, a private attribute, deliberately: `FastMCP`
    takes no `version`, so that coupling is real and an upstream rename must turn this red rather
    than silently reverting the whole fleet to reporting the MCP SDK's own release number.
    """
    from fastapi.testclient import TestClient
    from mcp.server.fastmcp import FastMCP
    from mcp_server_kit.app import connector_app

    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("MCP_SERVER_REVISION", "abc1234")
        served = FastMCP("probe")
        app = connector_app(served, name="probe")
        options = served._mcp_server.create_initialization_options()
        with TestClient(app) as client:
            probed = client.get("/healthz").json()

    assert options.server_version == "abc1234", (
        "the initialize() handshake reports "
        f"{options.server_version!r}; a client cannot tell which build answered"
    )
    assert probed == {"status": "ok", "server": "probe", "revision": "abc1234"}

    with pytest.MonkeyPatch.context() as patch:
        patch.delenv("MCP_SERVER_REVISION", raising=False)
        bare = FastMCP("probe")
        connector_app(bare, name="probe")
        assert bare._mcp_server.create_initialization_options().server_version == "unknown", (
            "an unstamped build must say so rather than report the MCP SDK's version, which is a "
            "true fact about the wrong thing"
        )


# A version specifier written as a literal inside a Containerfile's `pip install`, e.g.
# `"rxnmapper==0.4.3"`. Quoted because that is how a shell-safe specifier is written; the operator
# is captured so a floating one can be named in the failure rather than merely rejected.
_PIP_SPECIFIER = re.compile(r'"([A-Za-z0-9][A-Za-z0-9._-]*)(==|>=|<=|~=|>|<)([0-9][^"]*)"')


def _locked_versions() -> dict[str, str]:
    """Every distribution `uv.lock` resolves, name to version — the set `make deps-audit` reads."""
    import tomllib

    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    packages = lock["package"]
    assert isinstance(packages, list)
    return {str(entry["name"]): str(entry["version"]) for entry in packages}


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_an_image_that_installs_from_the_index_pins_what_the_audit_read(server: Path) -> None:
    """An image's direct install of a locked package is pinned to the version the audit read.

    **`make deps-audit` audits `uv.lock`, and no image consumes it** — none copies it, none passes
    `--constraint`, none runs `uv sync`. Every image re-resolves with pip at build time, so the
    audited closure and the shipped closure are two resolutions of the same declarations taken on
    different days. That gap cannot be closed from here: closing it means the images installing
    from the lock, which is a delivery change.

    What *can* be held here is the sharpest instance of it, and it was open: `rxnlabel`'s
    Containerfile installed `"rxnmapper>=0.4" "rxn-insight>=0.1.2"` straight from PyPI, so the one
    install that names its packages explicitly agreed with the audited version only by luck — and
    the audit's argued vulnerability suppressions, each written against a specific version, were
    being applied to versions nobody had checked. A specifier written into an image is pinned, and
    pinned to the lock, so `uv lock` moving it is what proposes the bump in a pull request.
    """
    text = (server / "Containerfile").read_text(encoding="utf-8")
    locked = _locked_versions()
    for name, operator, version in _PIP_SPECIFIER.findall(text):
        if name.lower().replace("_", "-") not in locked:
            continue  # not something `uv.lock` resolves, so this audit has nothing to say about it
        expected = locked[name.lower().replace("_", "-")]
        assert operator == "==", (
            f"{server.name}/Containerfile installs {name}{operator}{version} from the index: an "
            "open bound re-resolves on every build, so what ships is not what `make deps-audit` "
            f"read. Pin it to =={expected}, the version in uv.lock"
        )
        assert version == expected, (
            f"{server.name}/Containerfile pins {name}=={version} while uv.lock resolves "
            f"{expected}: the audited version and the shipped version have drifted apart"
        )


def test_every_published_dev_token_default_is_in_the_redaction_exemption() -> None:
    """`_PUBLISHED_VALUES` is a literal, so something has to hold it against what is published.

    `mcp_server_kit.logging` refuses to redact the credentials this repository commits, because a
    value anybody can read is not a secret and scrubbing it only corrupts logs. The set that says
    which ones those are is written out by hand; the `Makefile` is where they are actually
    published. A default added there and not here is silently redacted out of every `make run-*`
    log, and a value left here after the Makefile stops using it is a real credential this fleet
    has quietly exempted — so both directions are the same check, run against the file rather than
    against a memory of it.
    """
    from mcp_server_kit.logging import _PUBLISHED_VALUES

    makefile = (Path(__file__).resolve().parents[1] / "Makefile").read_text()
    defaults = set(re.findall(r"\$\$\{CHEMCLAW_[A-Z_]+_TOKEN:-([^}]+)\}", makefile))
    assert defaults, "no `CHEMCLAW_*_TOKEN` default found in the Makefile; has the pattern changed?"
    unexempted = defaults - _PUBLISHED_VALUES
    assert not unexempted, (
        f"the Makefile publishes {sorted(unexempted)!r} as a token default and "
        "mcp_server_kit.logging does not exempt it, so every local log line mentioning it is "
        "rewritten to ***"
    )
    stale = _PUBLISHED_VALUES - defaults
    assert not stale, (
        f"{sorted(stale)!r} is exempted from redaction and is no longer a "
        "published default; an exemption that outlives its reason is a credential this fleet has "
        "decided not to hide"
    )


# A variable a shipped file sets, and the value it sets it to — or `None` where the file *names* the
# variable and does not hold the value: a `valueFrom:` reference into a ConfigMap or Secret, or a
# Containerfile `ARG` the build supplies. Both ratchets below read `None` as unprovable rather than
# as absent, which is the only safe reading of a file that cannot answer the question.
EnvSetting = tuple[str, str | None]

# A `NAME=value` assignment at the head of a command line, which is how a value is set *past*
# every `env:` block and every `ENV` instruction:
# `command: ["sh", "-c", "MCP_EGRESS_GUARD=off exec …"]`.
_SHELL_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)


def _inline_assignments(command: object) -> list[EnvSetting]:
    """Every `NAME=value` a command line sets, whether it is a JSON array or one shell string.

    A container that sets a variable in its own `command:` sets it for the server process exactly
    as an `env:` entry would, and a parser reading only `env:` reports clean. Both shapes reduce to
    the same thing: split every string with `shlex` and keep the tokens that are assignments —
    over-inclusive on purpose, since a flag (`--port=8850`) cannot match and a real assignment must.
    """
    parts = command if isinstance(command, list) else [command]
    found: list[EnvSetting] = []
    for part in parts:
        if not isinstance(part, str):
            continue
        try:
            tokens = shlex.split(part)
        except ValueError:
            tokens = part.split()
        for token in tokens:
            match = _SHELL_ASSIGNMENT.match(token)
            if match:
                found.append((match.group(1), match.group(2)))
    return found


def _env_pairs(node: object) -> list[EnvSetting]:
    """Every environment variable a parsed manifest sets, however nested and however spelled.

    Recursive rather than pathed, because the shape differs between a Deployment's container and
    anything a server may add later — and a check that only looks where the variable is *expected*
    finds it exactly where it is not a problem.

    **Three shapes, because requiring `value` read two of them as setting nothing.** An entry whose
    value comes from a `valueFrom:` reference is reported with `None`: the variable is set, and this
    repository does not hold what to. A `command:`/`args:` assignment is reported with its value.
    Measured before the fix, a `valueFrom` pulling `MCP_EGRESS_ALLOW` out of a ConfigMap and a
    `command: ["sh", "-c", "MCP_EGRESS_GUARD=off exec uvicorn …"]` both returned no offences at all.
    """
    found: list[EnvSetting] = []
    if isinstance(node, dict):
        name = node.get("name")
        if isinstance(name, str):
            if "value" in node:
                found.append((name, str(node["value"])))
            elif "valueFrom" in node:
                found.append((name, None))
        for key in ("command", "args"):
            if key in node:
                found.extend(_inline_assignments(node[key]))
        for value in node.values():
            found.extend(_env_pairs(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_env_pairs(item))
    return found


def _instructions(text: str) -> list[str]:
    r"""A Containerfile's instructions, one per entry, with backslash continuations joined.

    Whole-line comments are dropped *before* the join, which is the order Docker's own parser uses
    and the only one that is safe here: every `ENV` in this fleet sits under a paragraph of prose,
    and joining first would let a comment line ending in a backslash swallow the instruction below
    it. A comment *inside* a continued instruction is removed without ending it, which is also
    Docker's behaviour.

    **Each line is stripped on both sides, because Docker accepts leading whitespace and the
    instruction match below is anchored at column 0.** Measured before the fix: a one-line
    Containerfile reading `   ENV MCP_EGRESS_GUARD=off` produced no offences from either ratchet,
    while a real `docker build` of it put `off` in the image.
    """
    joined: list[str] = []
    buffer = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            buffer += stripped[:-1] + " "
            continue
        joined.append(buffer + stripped)
        buffer = ""
    if buffer:
        joined.append(buffer)
    return joined


def _containerfile_env(label: str, text: str) -> list[EnvSetting]:
    r"""Every variable a Containerfile's `ENV` instructions set, however the instruction is spelled.

    **A regex over physical lines reads two of this fleet's seven Containerfiles as setting
    nothing.** `servers/rxnlabel` and `servers/rxnpredict` set `MCP_EGRESS_GUARD=on` as a
    backslash-continuation of a multi-line `ENV`, so an `^ENV\s+MCP_EGRESS_...` match never saw it
    — and would not have seen an `off` written the same way either. Measured before the fix: a
    widened continuation form returned no offences at all, which is a ratchet reporting clean on
    the one shape the tree actually uses.

    So the parse is Docker's: continuations joined (`_instructions`), then `ENV k=v k2=v2` split
    into its pairs with `shlex` so a quoted value survives, and the legacy `ENV name value` form —
    still valid, still one variable — read as one. `label` only names the file in a message.

    **`CMD` and `ENTRYPOINT` are read too**, for the reason a Deployment's `command:` is: a variable
    assigned at the head of the command line is set for the server process, and a parser that knows
    only `ENV` reports clean on it.
    """
    found: list[EnvSetting] = []
    for instruction in _instructions(text):
        words = re.split(r"\s+", instruction, maxsplit=1)
        verb, remainder = words[0], words[1] if len(words) > 1 else ""
        if verb.upper() in {"CMD", "ENTRYPOINT"}:
            found.extend(_inline_assignments(_command_words(remainder)))
            continue
        if verb.upper() != "ENV":
            continue
        try:
            tokens = shlex.split(remainder)
        except ValueError as exc:  # pragma: no cover - a malformed Containerfile
            raise AssertionError(f"{label}: cannot parse {instruction!r}: {exc}") from exc
        if not tokens:
            continue
        if "=" not in tokens[0]:
            found.append((tokens[0], " ".join(tokens[1:])))
            continue
        for token in tokens:
            name, _, value = token.partition("=")
            if name:
                found.append((name, value))
    return found


def _command_words(remainder: str) -> list[str] | str:
    """A `CMD`/`ENTRYPOINT` argument as its words — the JSON exec form, or the shell form verbatim.

    Docker's two forms differ in quoting, not in effect: `["sh", "-c", "X=1 exec …"]` and
    `sh -c "X=1 exec …"` both run the same process with the same environment.
    """
    if remainder.lstrip().startswith("["):
        try:
            parsed = json.loads(remainder)
        except ValueError:
            return remainder
        if not isinstance(parsed, list):
            return []
        return [word for word in parsed if isinstance(word, str)]
    return remainder


def _env_settings(label: str, text: str) -> list[EnvSetting]:
    """Every environment variable one shipped file sets, whichever kind of file it is.

    `label` decides how the text is read — a deployment manifest is YAML with `env:` entries, a
    Containerfile is `ENV` instructions. Shared by both ratchets below, so the two cannot disagree
    about what a file sets.
    """
    if label.endswith(".yaml"):
        return _env_pairs(list(yaml.safe_load_all(text)))
    return _containerfile_env(label, text)


def _shown(value: str | None) -> str:
    """A value as an offence reads it — `None` is a file naming the variable but not its value."""
    return repr(value) if value is not None else "a value this file does not hold"


def _provably_arms(value: str | None) -> bool:
    """Whether this value, as written in the file, *provably* leaves the egress guard armed.

    **The test is "provably on", not "not one of the words for off", and the inversion is the whole
    point.** `arm_from_env` arms unless the value is in `GUARD_DISABLED_VALUES`, so the old rule —
    flag the members of that set — read a value it could not resolve as clean. Measured before the
    fix, this Containerfile produced no offences here, and a reviewer's `docker build` of it put
    `MCP_EGRESS_GUARD=off` in the image:

        ARG GUARD=off
        ENV MCP_EGRESS_GUARD=${GUARD}

    That idiom already ships for a legitimate reason — `servers/rxnpredict/Containerfile` stamps its
    build revision with it — so it is the shape a developer reaches for, not a contrivance. Any `$`
    means the file does not hold the value the image will carry, and a `valueFrom:` reference
    (`None`) does not either. **An empty value arms**, because
    `os.environ.get("MCP_EGRESS_GUARD", "on")` returns `""` and `""` is in no disable set — so a
    bare `ENV MCP_EGRESS_GUARD=` is clean here, and `tests/test_egress.py` drives that against the
    real `arm_from_env` rather than leaving it to this docstring.
    """
    if value is None or "$" in value:
        return False
    return value.strip().lower() not in GUARD_DISABLED_VALUES


def _egress_offences(label: str, text: str) -> list[str]:
    """Every way one shipped file departs from the posture: guard on, allowlist empty.

    Split out from the test so the ratchet can be shown to bite on a widened manifest without one
    existing in the tree.
    """
    offences: list[str] = []
    if label.endswith(".yaml") and "envFrom" in text:
        offences.append(f"{label}: uses envFrom, which can carry MCP_EGRESS_* unseen")
    for name, value in _env_settings(label, text):
        if name == "MCP_EGRESS_ALLOW":
            offences.append(f"{label}: sets MCP_EGRESS_ALLOW={_shown(value)}")
        if name == "MCP_EGRESS_GUARD" and not _provably_arms(value):
            if value is not None and value.strip().lower() in GUARD_DISABLED_VALUES:
                offences.append(f"{label}: disables the egress guard ({value!r})")
            else:
                offences.append(
                    f"{label}: sets MCP_EGRESS_GUARD to {_shown(value)}, so this file cannot show "
                    "the guard is armed in the image it builds"
                )
    return offences


def test_no_shipped_deployment_widens_the_egress_allowlist() -> None:
    """`MCP_EGRESS_ALLOW` is empty in every shipped deployment — asserted, not asserted *about*.

    `CLAUDE.md` states that in the same breath as `chemclaw_mcp_egress_guard_armed`, which is the
    gauge that made "the guard is installed" a fact a scrape can check. The allowlist was the half
    nothing checked at either end: the gauge said `1` with `MCP_EGRESS_ALLOW=evil.example.com` set
    (fixed — `chemclaw_mcp_egress_allowed_hosts` publishes the count now), and no test anywhere
    read what this repository actually ships. Both halves are needed, because they fail
    differently: the gauge catches a *running* pod somebody widened, and this catches the widening
    arriving in a pull request, which is when it is cheap to argue about.

    `MCP_EGRESS_GUARD` is checked in the same pass for the same reason — a shipped `off` would
    disable the guard for every request, and the only in-repo record of that today is the ENV line
    in each Containerfile setting it explicitly `on`.

    **What this cannot see, stated rather than implied:** an `envFrom` block, which pulls values
    from a ConfigMap or Secret this repository does not hold, and a pod `env:` a cluster operator
    adds outside these files. The first is flagged where it appears; the second is what
    `chemclaw_mcp_egress_allowed_hosts` exists to make visible from a scrape.
    """
    shipped = sorted(SERVERS.glob("*/deploy/*.yaml")) + sorted(SERVERS.glob("*/Containerfile"))
    assert shipped, "no deployment manifests found; has the layout changed?"
    offences = [
        offence
        for manifest in shipped
        for offence in _egress_offences(
            str(manifest.relative_to(ROOT)), manifest.read_text(encoding="utf-8")
        )
    ]
    assert not offences, "the shipped posture is no egress and no allowlist:\n  " + "\n  ".join(
        offences
    )


def test_the_allowlist_check_bites() -> None:
    """A ratchet that passes on everything is not a ratchet, so it is shown failing on purpose.

    The tree it guards is clean today — which is exactly why the check above cannot demonstrate
    that it works. These inputs are the shapes a widening would arrive in.

    **The continuation arm is here because its absence was the hole.** This test drove only the
    own-line `ENV MCP_EGRESS_GUARD=off` form — which is the one shape `servers/rxnlabel` and
    `servers/rxnpredict` do *not* use. Both set the guard inside a multi-line `ENV`, and the
    physical-line regex this check used to run could not see it, so for two of seven servers a
    deployment could have written `off` in situ with the ratchet still green. Certifying the arm the
    tree does not exercise is the failure mode, not the regex.
    """
    widened = _egress_offences(
        "servers/x/deploy/deployment.yaml",
        "spec:\n  containers:\n    - name: server\n      env:\n"
        "        - name: MCP_EGRESS_ALLOW\n          value: weights.example.org\n",
    )
    assert widened == [
        "servers/x/deploy/deployment.yaml: sets MCP_EGRESS_ALLOW='weights.example.org'"
    ]
    assert _egress_offences("servers/x/Containerfile", "ENV MCP_EGRESS_GUARD=off\n") == [
        "servers/x/Containerfile: disables the egress guard ('off')"
    ]
    assert _egress_offences("servers/x/Containerfile", "ENV MCP_EGRESS_GUARD=on\n") == []

    # The shape the two ML servers actually ship: one `ENV` spanning lines, the guard not first and
    # not last, a comment paragraph above it and a comment line inside the continuation.
    continued = (
        "# The three switches that keep inference local.\n"
        "ENV HF_HOME=/opt/models/hf \\\n"
        "    HF_HUB_OFFLINE=1 \\\n"
        "# a comment inside a continuation does not end it\n"
        "    MCP_EGRESS_GUARD=off \\\n"
        '    MCP_EGRESS_ALLOW="evil.example.com" \\\n'
        "    CHEMCLAW_RXNPREDICT_MODEL_DIR=/opt/models\n"
    )
    assert _egress_offences("servers/x/Containerfile", continued) == [
        "servers/x/Containerfile: disables the egress guard ('off')",
        "servers/x/Containerfile: sets MCP_EGRESS_ALLOW='evil.example.com'",
    ]
    assert _egress_offences(
        "servers/x/Containerfile", continued.replace("GUARD=off", "GUARD=on")
    ) == ["servers/x/Containerfile: sets MCP_EGRESS_ALLOW='evil.example.com'"]

    # The guard set on as a continuation is what every shipped file that uses the form does, and it
    # must read as clean — a parser that flagged it would be noticed, a parser that cannot see it
    # at all is what shipped.
    assert _containerfile_env(
        "servers/x/Containerfile", continued.replace("GUARD=off", "GUARD=on")
    ) == [
        ("HF_HOME", "/opt/models/hf"),
        ("HF_HUB_OFFLINE", "1"),
        ("MCP_EGRESS_GUARD", "on"),
        ("MCP_EGRESS_ALLOW", "evil.example.com"),
        ("CHEMCLAW_RXNPREDICT_MODEL_DIR", "/opt/models"),
    ]

    # A comment line ending in a backslash must not swallow the instruction under it, which is why
    # comments are dropped before the join rather than after.
    assert _containerfile_env(
        "servers/x/Containerfile", "# a trailing backslash in prose \\\nENV MCP_EGRESS_GUARD=off\n"
    ) == [("MCP_EGRESS_GUARD", "off")]

    # Docker's legacy space-separated form sets one variable to the rest of the line.
    assert _containerfile_env(
        "servers/x/Containerfile", "ENV MCP_EGRESS_ALLOW evil.example.com"
    ) == [("MCP_EGRESS_ALLOW", "evil.example.com")]


def test_no_shape_that_hides_a_value_from_this_ratchet_reads_as_clean() -> None:
    """The four shapes three fresh-context reviewers walked the ratchet past, as the ratchet's own
    data.

    Each was measured returning `[]` before the fix, and a bypass that is not in the suite is not
    closed — it is rediscovered. In order of how likely a developer is to write it by accident:

    1. **`ARG` → `ENV` indirection.** `ARG GUARD=off` / `ENV MCP_EGRESS_GUARD=${GUARD}` builds an
       image with the guard off, which a reviewer confirmed against a real `docker build`. The old
       rule compared the value against the disable-set, and `${GUARD}` is in no set;
       `_provably_arms` inverts that.
       **This idiom already ships** — `servers/rxnpredict/Containerfile` stamps its revision with it
       — so it is the shape a reader of this tree would reach for.
    2. **A leading space.** Docker accepts `   ENV …` and the match was anchored at column 0; a
       reviewer confirmed the variable reaches the built image.
    3. **`valueFrom:`.** `_env_pairs` required a `value` key, so an entry pulling `MCP_EGRESS_ALLOW`
       out of a ConfigMap named the variable and reported nothing. It cannot be resolved here, so it
       is an offence: `envFrom` was already refused for exactly this reason and this is the same
       file hiding the same thing one key deeper.
    4. **A variable assigned in `command:`** (or in a Containerfile `CMD`/`ENTRYPOINT`), which sets
       it for the server process without an `env:` block anywhere.

    The two clean arms matter as much: an empty value **arms** the guard, and so does `on`. A
    ratchet that flagged `ENV MCP_EGRESS_GUARD=` would be refusing the posture it exists to protect.
    """
    indirected = _egress_offences(
        "servers/x/Containerfile", "FROM x\nARG GUARD=off\nENV MCP_EGRESS_GUARD=${GUARD}\n"
    )
    assert indirected == [
        "servers/x/Containerfile: sets MCP_EGRESS_GUARD to '${GUARD}', so this file cannot show "
        "the guard is armed in the image it builds"
    ]
    assert _egress_offences("servers/x/Containerfile", "ENV MCP_EGRESS_GUARD=\n") == []
    assert _egress_offences("servers/x/Containerfile", "ENV MCP_EGRESS_GUARD=on\n") == []

    assert _egress_offences("servers/x/Containerfile", "   ENV MCP_EGRESS_GUARD=off\n") == [
        "servers/x/Containerfile: disables the egress guard ('off')"
    ]

    assert _egress_offences(
        "servers/x/deploy/deployment.yaml",
        "spec:\n  containers:\n    - name: server\n      env:\n"
        "        - name: MCP_EGRESS_ALLOW\n          valueFrom:\n"
        "            configMapKeyRef: {name: egress, key: hosts}\n",
    ) == ["servers/x/deploy/deployment.yaml: sets MCP_EGRESS_ALLOW=a value this file does not hold"]

    assert _egress_offences(
        "servers/x/deploy/deployment.yaml",
        "spec:\n  containers:\n    - name: server\n"
        '      command: ["sh", "-c", "MCP_EGRESS_GUARD=off exec uvicorn app"]\n',
    ) == ["servers/x/deploy/deployment.yaml: disables the egress guard ('off')"]
    assert _egress_offences(
        "servers/x/Containerfile", 'CMD ["sh", "-c", "MCP_EGRESS_GUARD=off exec uvicorn app"]\n'
    ) == ["servers/x/Containerfile: disables the egress guard ('off')"]
    assert _egress_offences(
        "servers/x/Containerfile", "ENTRYPOINT MCP_EGRESS_ALLOW=evil.example.com uvicorn app\n"
    ) == ["servers/x/Containerfile: sets MCP_EGRESS_ALLOW='evil.example.com'"]


# Every environment variable a first-party module turns into a number is something a deployment can
# move, and `_BOUND_ANCHORS` is the floor under the derivation below: a scan that silently stopped
# finding variables — a renamed `env_prefix`, a read through a helper — would agree with an empty
# tree forever. These five are the ones whose loss would matter most, one per
# mechanism and one per server that owns an admission ceiling, which `CLAUDE.md` calls the bound a
# slow tool owes the fleet. They are named here rather than in prose for the reason this repository
# keeps relearning: a count or a list in a document goes stale on somebody else's merge.
_BOUND_ANCHORS = frozenset(
    {
        "MCP_MAX_SMILES_CHARS",
        "MCP_MAX_MOLECULE_ATOMS",
        "CHEMCLAW_CHEM_MAX_CONCURRENT_RENDERS",
        "CHEMCLAW_PYEXEC_MAX_CONCURRENT_RUNS",
        "CHEMCLAW_CALC_MAX_CONCURRENT_REQUESTS",
    }
)

# A shipped deployment file that sets a derived numeric setting, and the argument for it. One row,
# and it is the one this check found on its first run over the real tree — which is also the
# counter-example to the widening check this one replaced. `crest_threads` defaults to `0`, meaning
# "let CREST's OpenMP size itself from `/proc/cpuinfo`", which is the *node's* core count and not
# something a container CPU limit changes; `servers/calc/Containerfile` sets `4`, and a comparison
# against the default would have read that narrowing as `4 > 0` and called it a widening.
#
# Everything else derived below is either a resource bound (`MCP_MAX_*`, the admission ceilings, the
# thread pool) or — in `servers/calc` — a *scientific* constant that enters `calc_version`, the
# primary key of Chemclaw3's calibration ledger; that server's own config docstring says changing
# one "is a scientific decision, not a deployment tweak". The two classes fail differently and need
# the same gate: one lets a pod be exhausted, the other writes rows nothing reconciles against.
_ARGUED_DEPLOYMENT_SETTINGS: frozenset[tuple[str, str]] = frozenset(
    {
        # CREST is the one thing in this image that should use more than one core, and the scrubbed
        # child environment means it has to be told so here rather than inherit `OMP_NUM_THREADS`.
        ("servers/calc/Containerfile", "CHEMCLAW_CREST_THREADS"),
    }
)

_NUMERIC_CASTS = frozenset({"int", "float"})


def _is_environ(node: ast.AST) -> bool:
    """Whether `node` is `os.environ` (or a bare `environ` imported from it)."""
    if isinstance(node, ast.Attribute):
        return node.attr == "environ"
    return isinstance(node, ast.Name) and node.id == "environ"


def _called_name(node: ast.Call) -> str:
    """The bare name a call names, whatever it hangs off — `os.getenv` and `getenv` read the
    same."""
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else ""


def _env_read(node: ast.AST) -> str | None:
    """The variable name `node` reads from the environment, when it is a literal.

    Three spellings: `os.environ["X"]`, `os.environ.get("X", …)` and `os.getenv("X", …)`. The third
    was missed until 2026-09-12 — nothing in `src/` uses it today, so the gap was latent, and a
    derivation that silently omits the most ordinary spelling of an environment read is the failure
    this whole ratchet is about.
    """
    if isinstance(node, ast.Call):
        func = node.func
        called = _called_name(node)
        if (
            isinstance(func, ast.Attribute)
            and called in {"get", "setdefault"}
            and _is_environ(func.value)
            and node.args
        ) or (called == "getenv" and node.args):
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                return first.value
    if isinstance(node, ast.Subscript) and _is_environ(node.value):
        key = node.slice
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            return key.value
    return None


def _env_read_within(node: ast.AST) -> str | None:
    """The first literal environment read anywhere inside `node`.

    A subtree rather than the node itself, because the read is rarely the bare argument: the fleet
    writes `int(os.environ.get("X", "4"))` and `os.environ.get("X", "").strip()`.
    """
    for child in ast.walk(node):
        found = _env_read(child)
        if found is not None:
            return found
    return None


def _numeric_environ_reads(tree: ast.Module) -> dict[str, int]:
    """Every environment variable this module turns into a number, and the line it happens on.

    Two shapes, because the fleet uses both: the read wrapped directly in `int`/`float`, and the
    read bound to a local that is converted further down (`raw = os.environ.get(...).strip()` then
    `float(raw)` — `executor.py` and `sessions.py` are written that way, and a scan that only
    matched the direct form would report those three variables absent).
    """
    from_var: dict[str, tuple[str, int]] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            read = _env_read_within(node.value)
            if read is not None:
                from_var[node.targets[0].id] = (read, node.lineno)
    found: dict[str, int] = {}
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in _NUMERIC_CASTS
            and node.args
        ):
            continue
        argument = node.args[0]
        read, line = _env_read_within(argument), node.lineno
        if read is None and isinstance(argument, ast.Name) and argument.id in from_var:
            read, line = from_var[argument.id]
        if read is not None:
            found.setdefault(read, line)
    return found


def _numeric_settings_fields(tree: ast.Module) -> dict[str, int]:
    """Every numeric `pydantic-settings` field this module declares, as its environment name.

    `servers/calc` and `servers/rxnpredict` configure themselves through `BaseSettings` with an
    `env_prefix`, so their bounds never appear in an `os.environ` call at all — the variable name is
    the prefix plus the field name, and `case_sensitive` is pydantic's default of `False`. A scan
    that knew only `os.environ` finds most of `servers/calc`'s numbers absent, including its
    admission ceiling — see `test_the_bound_scan_sees_both_configuration_mechanisms`, which holds
    the figure rather than this sentence.

    Scalar `int`/`float` annotations only, `X | None` included. A number inside a container
    annotation (`dict[str, float]`) is not a bound anything here could compare, and is left out
    rather than half-covered.
    """
    found: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        bases = {
            base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
            for base in node.bases
        }
        if "BaseSettings" not in bases:
            continue
        prefix = ""
        for statement in node.body:
            if not (
                isinstance(statement, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == "model_config"
                    for target in statement.targets
                )
                and isinstance(statement.value, ast.Call)
            ):
                continue
            for keyword in statement.value.keywords:
                if keyword.arg == "env_prefix" and isinstance(keyword.value, ast.Constant):
                    prefix = str(keyword.value.value)
        for statement in node.body:
            if (
                isinstance(statement, ast.AnnAssign)
                and isinstance(statement.target, ast.Name)
                and _annotation_is_numeric(statement.annotation)
            ):
                found[(prefix + statement.target.id).upper()] = statement.lineno
    return found


def _annotation_is_numeric(annotation: ast.expr) -> bool:
    """Whether `annotation` is `int`, `float`, one of those unioned with `None`, or wrapped.

    `Annotated[int, Field(ge=1)]` is the spelling a field grows the moment somebody wants a
    constraint on it, and it was invisible here until 2026-09-12 — so a bound could have left the
    ratchet's set by acquiring a validator. No field in the tree is written that way today;
    `Optional[int]` is covered as the `|` form only, which is the form this repository writes.
    """
    if isinstance(annotation, ast.Name):
        return annotation.id in _NUMERIC_CASTS
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return _annotation_is_numeric(annotation.left) or _annotation_is_numeric(annotation.right)
    if isinstance(annotation, ast.Subscript) and _annotation_name(annotation.value) == "Annotated":
        inner = annotation.slice
        first = inner.elts[0] if isinstance(inner, ast.Tuple) and inner.elts else inner
        return _annotation_is_numeric(first)
    return False


def _annotation_name(node: ast.expr) -> str:
    """The bare name of an annotation's head — `Annotated` and `typing.Annotated` read the same."""
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else ""


class Bound(NamedTuple):
    """A number a deployment can move: where the code reads it, and how its name is matched.

    `case_sensitive` is not decoration. `os.environ["MCP_MAX_SMILES_CHARS"]` reads that exact name
    and nothing else, while `pydantic-settings` leaves `case_sensitive=False`, so *every* spelling
    of a settings field's name is honoured. Measured on 2026-09-12 with the real classes:
    `chemclaw_calc_max_concurrent_requests=99` gives `CalcSettings().calc_max_concurrent_requests ==
    99`, and `mcp_max_smiles_chars=7` leaves `limits.MAX_SMILES_CHARS` at its default of 4000. So
    the matching has to differ per mechanism: a uniform case-sensitive rule misses the lowercase
    spelling of an admission ceiling, and a uniform case-insensitive one flags an `ENV` that does
    nothing at all.
    """

    where: str
    case_sensitive: bool


def numeric_env_bounds() -> dict[str, Bound]:
    """Every environment variable first-party code turns into a number, and where it is read.

    Derived rather than listed, because a hand-written list of names is the drift hazard this
    repository keeps finding — the `Ports` section of `CLAUDE.md` is the worked example, a second
    table that published two taken ports as free. `Bound.where` is `path:line`, which is what makes
    an offence actionable without a second lookup.

    **What the derivation does not see, stated rather than implied** — each measured on 2026-09-12
    against a synthetic module, and none of these shapes exists in `src/` today:

    - a read through a helper (`_env_int("X", 4)`), which needs the helper's body followed;
    - a settings class inheriting from a `BaseSettings` *subclass*, where `env_prefix` is on the
      parent;
    - a nested `BaseModel` reached through `env_nested_delimiter`;
    - `Field(4, validation_alias="REAL_NAME")`, which is found under the *prefixed field name*
      rather than under the alias the environment actually uses — the one shape that is worse than
      absent, because it reports a name nothing reads.

    A row in `docs/BACKLOG.md` carries the decision about whether to follow them; `_BOUND_ANCHORS`
    is the floor that keeps the derivation from quietly returning less than it did.
    """
    found: dict[str, Bound] = {}
    roots = sorted(ROOT.glob("packages/*/src")) + sorted(ROOT.glob("servers/*/src"))
    assert roots, "no first-party source roots found; has the layout changed?"
    for root in roots:
        for source in sorted(root.rglob("*.py")):
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            where = source.relative_to(ROOT)
            for name, line in _numeric_environ_reads(tree).items():
                found[name] = Bound(f"{where}:{line}", case_sensitive=True)
            for name, line in _numeric_settings_fields(tree).items():
                found[name] = Bound(f"{where}:{line}", case_sensitive=False)
    return found


def _matching_bound(name: str, bounds: dict[str, Bound]) -> tuple[str, Bound] | None:
    """The bound a shipped spelling of `name` moves, under that bound's own matching rule.

    A lowercase `ENV chemclaw_calc_max_concurrent_requests=99` moved `servers/calc`'s admission
    ceiling to 99 with both ratchets silent, because the derivation uppercases a settings field's
    name and matching was `name in bounds`.
    """
    exact = bounds.get(name)
    if exact is not None:
        return name, exact
    canonical = name.upper()
    insensitive = bounds.get(canonical)
    if insensitive is not None and not insensitive.case_sensitive:
        return canonical, insensitive
    return None


def _bound_offences(label: str, text: str, bounds: dict[str, Bound]) -> list[str]:
    """Every numeric setting one shipped file moves without an argued row.

    **The shape, and why it is this one rather than a widening check.** A ratchet asserting "a
    shipped value does not *widen* the default" is stronger where the default is discoverable and
    the direction is known, and that is true of neither half here: `MCP_THREAD_POOL_SIZE` and
    `MCP_SESSION_IDLE_TIMEOUT_SECONDS` are read with an empty-string default and get their real one
    from a constant or from a cgroup read at runtime, and for `servers/calc`'s fitted pKa
    calibration constants "wider" means nothing at all. A direction the check had to guess would be
    a check that passes on the cases it cannot classify — which is how the egress ratchet above came
    to certify the one shape the tree does not use.

    So the rule is uniform and needs no direction: a shipped file sets none of these unless the pair
    is in `_ARGUED_DEPLOYMENT_SETTINGS`. It catches a narrowing too, deliberately — narrowing
    `MCP_MAX_SMILES_CHARS` in one server's image is a behaviour change a caller discovers as a
    refusal, and it is the same review either way. The one argued row in the tree is a narrowing,
    and a default comparison would have scored it as a widening; see that constant.
    """
    offences: list[str] = []
    if label.endswith(".yaml") and "envFrom" in text:
        offences.append(f"{label}: uses envFrom, which can carry a resource bound unseen")
    for name, value in _env_settings(label, text):
        matched = _matching_bound(name, bounds)
        if matched is None:
            continue
        canonical, bound = matched
        if (label, canonical) in _ARGUED_DEPLOYMENT_SETTINGS:
            continue
        spelling = (
            ""
            if name == canonical
            else f" (written as {name}, which a case-insensitive settings field honours)"
        )
        offences.append(
            f"{label}: sets {canonical}={_shown(value)}{spelling}, which {bound.where} reads as a "
            "number"
        )
    return offences


def test_no_shipped_deployment_moves_a_bound_the_code_reads_from_the_environment() -> None:
    """Nothing in `deploy/` or a Containerfile moves a number first-party code reads as a bound.

    `CLAUDE.md` makes two of these non-negotiable — "a ceiling on how many of it may run at once" is
    what a slow tool owes the fleet — and nothing checked any of them at either end. The sibling
    ratchet above reads the same files for `MCP_EGRESS_*` and throws every other pair away, so a
    `deploy/deployment.yaml` `env:` entry or a Containerfile `ENV` could have doubled an admission
    ceiling, widened the SMILES bound that stops a SIGSEGV, or retuned the pKa calibration that keys
    Chemclaw3's ledger, with nothing going red.

    The set is derived from the code that reads it (`numeric_env_bounds`), not listed here, so a
    bound added next year in one of the shapes that derivation parses is covered the day it is
    written — which is the honest form of a sentence that used to promise *any* new bound.
    `numeric_env_bounds` names the four shapes it does not follow; a row in `docs/BACKLOG.md`
    carries the decision.

    What this cannot see is stated rather than implied: a pod `env:` a cluster operator adds outside
    these files, and an `envFrom` whose values live in a ConfigMap this repository does not hold —
    the second is flagged where it appears, and so now are a `valueFrom:` reference and a variable
    assigned in a `command:`, both of which used to read as setting nothing.
    """
    bounds = numeric_env_bounds()
    assert set(bounds) >= _BOUND_ANCHORS, (
        f"the bound scan lost {sorted(_BOUND_ANCHORS - set(bounds))!r}; a derivation that stops "
        "finding variables agrees with an empty tree forever"
    )
    shipped = sorted(SERVERS.glob("*/deploy/*.yaml")) + sorted(SERVERS.glob("*/Containerfile"))
    assert shipped, "no deployment manifests found; has the layout changed?"
    offences = [
        offence
        for manifest in shipped
        for offence in _bound_offences(
            str(manifest.relative_to(ROOT)), manifest.read_text(encoding="utf-8"), bounds
        )
    ]
    assert not offences, (
        "a bound is moved in a shipped file with no argued row in "
        "`_ARGUED_DEPLOYMENT_SETTINGS`:\n  " + "\n  ".join(offences)
    )


def test_the_bound_check_bites() -> None:
    """The tree is clean, so the check is shown failing on purpose — in both file shapes.

    Including the continuation `ENV`, because that is the shape the sibling egress ratchet was blind
    to for two of seven servers, and this check reads the same files through the same parser.
    """
    bounds = {
        "CHEMCLAW_CHEM_MAX_CONCURRENT_RENDERS": Bound("servers/chem/x.py:1", case_sensitive=False)
    }
    assert _bound_offences(
        "servers/chem/deploy/deployment.yaml",
        "spec:\n  containers:\n    - name: server\n      env:\n"
        "        - name: CHEMCLAW_CHEM_MAX_CONCURRENT_RENDERS\n          value: '64'\n",
        bounds,
    ) == [
        "servers/chem/deploy/deployment.yaml: sets CHEMCLAW_CHEM_MAX_CONCURRENT_RENDERS='64', "
        "which servers/chem/x.py:1 reads as a number"
    ]
    assert _bound_offences(
        "servers/chem/Containerfile",
        "ENV PYTHONUNBUFFERED=1 \\\n    CHEMCLAW_CHEM_MAX_CONCURRENT_RENDERS=64\n",
        bounds,
    ) == [
        "servers/chem/Containerfile: sets CHEMCLAW_CHEM_MAX_CONCURRENT_RENDERS='64', "
        "which servers/chem/x.py:1 reads as a number"
    ]
    # A setting nothing reads as a number is not this check's business.
    assert _bound_offences("servers/chem/Containerfile", "ENV HF_HUB_OFFLINE=1\n", bounds) == []
    # An argued row is the one way through, and it is a *pair*: the real row exempts
    # `CHEMCLAW_CREST_THREADS` in calc's Containerfile and nowhere else, so the same variable set
    # from another file is still an offence.
    threads = {
        "CHEMCLAW_CREST_THREADS": Bound("servers/calc/.../config.py:215", case_sensitive=False)
    }
    assert (
        _bound_offences("servers/calc/Containerfile", "ENV CHEMCLAW_CREST_THREADS=4\n", threads)
        == []
    )
    assert _bound_offences(
        "servers/chem/Containerfile", "ENV CHEMCLAW_CREST_THREADS=4\n", threads
    ) == [
        "servers/chem/Containerfile: sets CHEMCLAW_CREST_THREADS='4', which "
        "servers/calc/.../config.py:215 reads as a number"
    ]
    # A ConfigMap this repository does not hold can carry any of them.
    assert _bound_offences(
        "servers/chem/deploy/deployment.yaml",
        "spec:\n  containers:\n    - name: server\n      envFrom:\n"
        "        - configMapRef:\n            name: tuning\n",
        bounds,
    ) == [
        "servers/chem/deploy/deployment.yaml: uses envFrom, which can carry a resource bound unseen"
    ]


def test_no_spelling_that_moved_a_bound_past_this_ratchet_reads_as_clean() -> None:
    """The lowercase spelling, and the two hiding places, as this ratchet's own data.

    **A lowercase environment name moves the ceiling of the server whose calls take minutes to
    hours.** `pydantic-settings` leaves `case_sensitive=False`, so
    `chemclaw_calc_max_concurrent_requests=99` gives `CalcSettings().calc_max_concurrent_requests ==
    99` (measured 2026-09-12 against the real class), while the derivation uppercases what it finds
    and matching was `name in bounds` — so the uppercase spelling was caught and the lowercase one
    was invisible. Both are the same change to the pod.

    The **other direction is asserted too**, because it is what stops this fix becoming a nuisance:
    `os.environ` is case-sensitive, `mcp_max_smiles_chars=7` leaves `limits.MAX_SMILES_CHARS` at
    4000 (measured the same way), and a ratchet that flagged it would be reporting an `ENV` that
    changes nothing. That is why a bound carries how its name is matched rather than a flag on the
    check.
    """
    ceiling = {
        "CHEMCLAW_CALC_MAX_CONCURRENT_REQUESTS": Bound(
            "servers/calc/.../config.py:194", case_sensitive=False
        ),
        "MCP_MAX_SMILES_CHARS": Bound("packages/.../limits.py:40", case_sensitive=True),
    }
    assert _bound_offences(
        "servers/calc/Containerfile", "ENV chemclaw_calc_max_concurrent_requests=99\n", ceiling
    ) == [
        "servers/calc/Containerfile: sets CHEMCLAW_CALC_MAX_CONCURRENT_REQUESTS='99' (written as "
        "chemclaw_calc_max_concurrent_requests, which a case-insensitive settings field honours), "
        "which servers/calc/.../config.py:194 reads as a number"
    ]
    assert (
        _bound_offences("servers/chem/Containerfile", "ENV mcp_max_smiles_chars=7\n", ceiling) == []
    )
    assert _bound_offences(
        "servers/chem/Containerfile", "ENV MCP_MAX_SMILES_CHARS=7\n", ceiling
    ) == [
        "servers/chem/Containerfile: sets MCP_MAX_SMILES_CHARS='7', which "
        "packages/.../limits.py:40 reads as a number"
    ]

    # The *derived* set carries the same distinction, which the fixtures above cannot show: a
    # mutation flipping every bound to case-insensitive left this test green until these two lines
    # existed, because a hand-built fixture asserts the matching rule and not the derivation.
    derived = numeric_env_bounds()
    assert derived["MCP_MAX_SMILES_CHARS"].case_sensitive, (
        "an `os.environ` read is case-sensitive; marking it otherwise makes the ratchet flag an "
        "`ENV` that changes nothing"
    )
    assert not derived["CHEMCLAW_CALC_MAX_CONCURRENT_REQUESTS"].case_sensitive, (
        "a `pydantic-settings` field is honoured in any case; marking it sensitive puts the "
        "lowercase spelling of an admission ceiling back outside the ratchet"
    )

    # The same two hiding places the egress ratchet had, over the same parser: a bound pulled from a
    # ConfigMap, and a bound assigned in the container's own command line.
    assert _bound_offences(
        "servers/calc/deploy/deployment.yaml",
        "spec:\n  containers:\n    - name: server\n      env:\n"
        "        - name: CHEMCLAW_CALC_MAX_CONCURRENT_REQUESTS\n          valueFrom:\n"
        "            configMapKeyRef: {name: tuning, key: ceiling}\n",
        ceiling,
    ) == [
        "servers/calc/deploy/deployment.yaml: sets CHEMCLAW_CALC_MAX_CONCURRENT_REQUESTS=a value "
        "this file does not hold, which servers/calc/.../config.py:194 reads as a number"
    ]
    assert _bound_offences(
        "servers/calc/deploy/deployment.yaml",
        "spec:\n  containers:\n    - name: server\n"
        '      command: ["sh", "-c", "MCP_MAX_SMILES_CHARS=9 exec uvicorn app"]\n',
        ceiling,
    ) == [
        "servers/calc/deploy/deployment.yaml: sets MCP_MAX_SMILES_CHARS='9', which "
        "packages/.../limits.py:40 reads as a number"
    ]


def test_the_derivation_reads_the_two_spellings_it_used_to_miss() -> None:
    """`os.getenv` and `Annotated[int, …]`, neither of which exists in `src/` today.

    That is the point: a derivation is a claim about shapes rather than about this tree, and both of
    these would have entered it as an ordinary line of code with the ratchet silent. Measured on
    2026-09-12 before the fix, each of these modules contributed **nothing** to the bound set.

    The shapes still outside it are named in `numeric_env_bounds`' docstring and queued with an
    anchor, rather than left for the next reviewer to discover by writing one.
    """
    getenv = ast.parse('import os\n\nLIMIT = int(os.getenv("MCP_MAX_THINGS", "4"))\n')
    assert set(_numeric_environ_reads(getenv)) == {"MCP_MAX_THINGS"}
    bare = ast.parse('from os import getenv\n\nLIMIT = float(getenv("MCP_MAX_SECONDS", "1.5"))\n')
    assert set(_numeric_environ_reads(bare)) == {"MCP_MAX_SECONDS"}

    annotated = ast.parse(
        "from typing import Annotated\n\n"
        "class S(BaseSettings):\n"
        '    model_config = SettingsConfigDict(env_prefix="CHEMCLAW_")\n'
        "    max_runs: Annotated[int, Field(ge=1)] = 4\n"
    )
    assert set(_numeric_settings_fields(annotated)) == {"CHEMCLAW_MAX_RUNS"}

    # And the boundary, asserted so the docstring naming it cannot quietly become false: a read
    # through a helper is not followed, and that is a decision rather than an oversight.
    helper = ast.parse('LIMIT = _env_int("MCP_MAX_THINGS", 4)\n')
    assert _numeric_environ_reads(helper) == {}


def test_the_bound_scan_sees_both_configuration_mechanisms() -> None:
    """A scan that knew only `os.environ` would find `servers/calc`'s whole config absent.

    This is the half of the inventory that is easy to get wrong in the reassuring direction. `calc`
    reads none of its numbers through `os.environ` — its bounds look like constants in
    `engine/config.py`, and they are `pydantic-settings` fields under `env_prefix="CHEMCLAW_"`, so
    every one of them is an environment variable. `servers/calc/tests/test_admission.py` measures
    the consequence on the ceiling itself; this asserts the *scan* can see it, which is what makes
    the ratchet above cover the heaviest server in the fleet rather than silently skip it.
    """
    bounds = numeric_env_bounds()
    environ_read = {
        name
        for name, bound in bounds.items()
        if bound.where.startswith(("packages/", "servers/chem/", "servers/pyexec/"))
    }
    assert "MCP_MAX_SMILES_CHARS" in environ_read
    calc = {name for name, bound in bounds.items() if bound.where.startswith("servers/calc/")}
    assert "CHEMCLAW_CALC_MAX_CONCURRENT_REQUESTS" in calc, (
        "calc's admission ceiling is a settings field, not a constant; if the scan cannot see it "
        "the ratchet does not cover the one server whose calls take minutes"
    )
    assert len(calc) > 20, (
        f"the settings mechanism contributes {len(calc)} of calc's numbers; a collapse here is a "
        "ratchet that has quietly stopped covering the server with the most to move"
    )


# What `egress.py` says is outside the runtime guard, as the distinctive phrase for each channel.
# Transcribed here rather than parsed out of either document, because what is checked is that two
# independently-written paragraphs name the same set — and a derivation from one of them would make
# the other's omission invisible, which is the defect this test exists for.
_UNGUARDED_CHANNELS = ("child process", "ctypes", "_socket.socket", "compiled extension")


def test_claude_md_and_the_guard_name_the_same_channels_as_outside_it() -> None:
    """`CLAUDE.md` named three of the four channels the guard cannot reach, and omitted `_socket`.

    That is the omission that matters most of the four: `_socket.socket` is the one the guard
    *provably* cannot reach — `arm()` rebinds the Python subclass's methods, never the C type's —
    and a reader of the shorter list would take the static scan's `_socket` entry for
    belt-and-braces rather than for the only in-repo layer that sees it. Both documents are prose
    about the same mechanism, written months apart, and nothing compared them.

    The phrases are the test's own data; the check is that each appears on both sides. A channel
    added to one document and not the other fails here, in either direction.

    **A phrase check alone let both documents miscount what covers the four**, which is the half
    added on 2026-09-12. `CLAUDE.md` read "Two of them layer 2 *can* [see]" and named `_socket` and
    `grpc` — but `grpc` is not one of the four channels, it is an *instance* of the fourth, and the
    same sentence then said `ctypes` is off layer 2's list on purpose, which only means anything
    because layer 2 can see it. So the second half of this check is against `FORBIDDEN_MODULES`
    itself: what the document says that list holds, and does not hold, is what it holds.
    """
    guard = (ROOT / "packages/mcp_server_kit/src/mcp_server_kit/egress.py").read_text(
        encoding="utf-8"
    )
    guard_docstring = guard[: guard.index('"""', guard.index('"""') + 3)]

    readme = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    start = readme.index("1. **The runtime guard**")
    layer_one = readme[start : readme.index("\n2. **The static scan**", start)]

    for channel in _UNGUARDED_CHANNELS:
        assert channel in guard_docstring, (
            f"`egress.py` no longer names {channel!r} as outside the guard; if the guard now "
            "covers it, `CLAUDE.md` §1 and this list are what say so"
        )
        assert channel in layer_one, (
            f"`CLAUDE.md`'s 'No egress. Ever.' §1 does not name {channel!r}, which `egress.py` "
            "says is outside the runtime guard — a reader of the shorter list believes in a "
            "boundary that is not there"
        )

    from mcp_server_kit.no_egress import FORBIDDEN_MODULES

    for module in ("_socket", "grpc"):
        assert module in FORBIDDEN_MODULES, (
            f"`CLAUDE.md` §1 says layer 2 refuses {module!r}; `no_egress.FORBIDDEN_MODULES` does "
            "not list it, so the document describes a control that is not there"
        )
        assert f"`{module}`" in layer_one, (
            f"{module!r} is on layer 2's list and `CLAUDE.md` §1 no longer says so"
        )
    assert "ctypes" not in FORBIDDEN_MODULES, (
        "`CLAUDE.md` §1 and `no_egress.py` both argue `ctypes` is off layer 2's list on purpose; "
        "it is on the list now, so both paragraphs are wrong and the argument needs rewriting"
    )


def test_every_path_claude_md_cites_under_a_real_directory_resolves() -> None:
    """A document that cites a test as the thing holding a claim must cite one that exists.

    `CLAUDE.md` named `tests/test_deploy.py` as what asserts the NetworkPolicy in both directions.
    That file has never existed: the assertion is each server's own
    `servers/*/tests/test_deploy.py`, and the root file with the closest name,
    `tests/test_deploy_shape.py`, carries no egress assertion at all. A citation to a file nobody
    can open is the same failure as the port table this repository deleted — a second declaration
    nothing checks.

    Only paths rooted at a real top-level *entry* are checked, which needs no allowlist: this
    document also writes `app.py`, `connector.yaml` and `mcp_server_kit/egress.py` as deliberate
    shorthand for "the one in every server" or "the module", and none of those begins with an entry
    that exists here.

    **An entry, not a directory.** This filtered to `is_dir()` while the sibling check in
    `tests/test_backlog_register.py` — written in the same commit, and claiming in its docstring to
    use "the same trick" — did not. The consequence was silent: a citation to a root-level *file*
    was unchecked, and the files in question are `MODULES.md` (this repository's only port
    registry), `Makefile`, `pyproject.toml` and `uv.lock`. The backlog version was the correct one.
    Measured on 2026-09-12, dropping the filter brings `MODULES.md`, `README.md` and `conftest.py`
    into the check, all three resolving.

    **What this heuristic cannot do, measured rather than assumed**: catch a citation to a
    root-level file that has been *removed*. "Is this token a path" is decided by whether its first
    segment exists, so a single-segment citation is self-rooting — renaming `MODULES.md` in this
    document to `MODULESGONE.md` leaves the check green, because the token stops being read as a
    path at the same moment it stops resolving. What it does catch is a path *under* an entry that
    exists, which is every citation in this document that names a test, a module or a manifest. The
    same limit applies to `tests/test_backlog_register.py`'s anchor check, which shares the trick.
    """
    top_level = {path.name for path in ROOT.iterdir()}
    cited = sorted(
        set(
            re.findall(
                r"`([A-Za-z0-9_./*-]+\.(?:py|yaml|yml|json|md|toml))`",
                (ROOT / "CLAUDE.md").read_text(encoding="utf-8"),
            )
        )
    )
    rooted = [path for path in cited if path.split("/")[0] in top_level]
    assert rooted, "no rooted paths found in CLAUDE.md; has the citation style changed?"
    missing = [
        path
        for path in rooted
        if not (sorted(ROOT.glob(path)) if "*" in path else (ROOT / path).exists())
    ]
    assert not missing, f"`CLAUDE.md` cites paths that do not exist: {missing!r}"

    # The negative half of the claim above, so the corrected sentence cannot go stale the other way.
    assert not (ROOT / "tests/test_deploy.py").exists(), (
        "a root `tests/test_deploy.py` now exists; `CLAUDE.md` §4 says it does not and points at "
        "the per-server files instead"
    )
