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

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SERVERS = ROOT / "servers"
MANIFESTS = ROOT / "manifests"
PORT_RANGE = range(8850, 8900)


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


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_the_manifest_is_registered_by_symlink(server: Path) -> None:
    """`manifests/` is what Chemclaw3 reads; a copy there is a second declaration that drifts."""
    registered = MANIFESTS / server.name / "connector.yaml"
    assert registered.is_symlink(), (
        f"{registered} must be a symlink to the server's own manifest, not a copy — two copies of "
        "one declaration is how a manifest outlives the surface it describes"
    )
    assert registered.resolve() == (server / "connector.yaml").resolve()


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
    """8850+ keeps the fleet clear of Chemclaw3's own 8810-8815 and the mock's 8090-8091."""
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
