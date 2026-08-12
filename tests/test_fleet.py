"""Fleet-level invariants — the ones no single server can see about itself.

Each server's own tests check that server. Nothing there can notice that two servers claimed the
same port, that a manifest was copied into `manifests/` instead of symlinked and has since drifted,
or that a server exists which `MODULES.md` has never heard of. Those are exactly the failures that
appear once, in a deployment, months later.

The shape is Chemclaw3's `tests/test_repo_map.py`: check the declarations against the directories
on disk, **in both directions**, because a one-way check passes happily while the tree grows things
the documentation does not know about.
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


def registered_manifests() -> list[Path]:
    """Every manifest Chemclaw3 would read from `manifests/`, whatever it points at.

    Not the same set as `server_dirs()`, and the difference is the point. `manifests/` is a
    directory rather than a detail of `servers/` precisely so a server hosted in another repository
    — `retro` is the one due — can be declared here with no code beside it. Those manifests were
    the ones nothing checked: not classification, not bearer auth, not port uniqueness, which is the
    whole contract this repository owes a server it does not host.
    """
    return sorted(path / "connector.yaml" for path in MANIFESTS.iterdir() if path.is_dir())


def parse(manifest: Path) -> dict[str, object]:
    """One parsed manifest, from its own path."""
    loaded = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"{manifest} is not a mapping"
    return loaded


def endpoint_of(manifest: Path) -> dict[str, object]:
    """One manifest's `endpoint:` block."""
    endpoint = parse(manifest)["endpoint"]
    assert isinstance(endpoint, dict), f"{manifest} has no endpoint mapping"
    return endpoint


def manifest_of(server: Path) -> dict[str, object]:
    """One server's parsed manifest."""
    return parse(server / "connector.yaml")


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
        # The file that asserts the NetworkPolicy beside it denies egress in both directions. It
        # was the one required artifact this list did not require, which is the wrong one to leave
        # optional when the whole posture rests on it.
        "tests/test_deploy.py",
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


@pytest.mark.parametrize("manifest", registered_manifests(), ids=lambda path: path.parent.name)
def test_every_manifest_names_the_directory_it_is_registered_under(manifest: Path) -> None:
    """The name Chemclaw3 dials is the directory name; a manifest calling itself something else
    would be advertised under one string and addressed by another."""
    assert parse(manifest)["name"] == manifest.parent.name


@pytest.mark.parametrize("manifest", registered_manifests(), ids=lambda path: path.parent.name)
def test_every_tool_is_classified_exactly_once(manifest: Path) -> None:
    """The rule Chemclaw3's HttpEndpoint enforces (D-167). Omission fails *open* at the gate."""
    name = manifest.parent.name
    endpoint = endpoint_of(manifest)
    tools = set(endpoint.get("tools", []))
    read_only = set(endpoint.get("read_only", []))
    state_changing = set(endpoint.get("state_changing", []))
    assert tools, f"{name} declares no tools"
    assert not (tools - read_only - state_changing), f"{name}: unclassified tools"
    assert not (read_only & state_changing), f"{name}: tools classified twice"
    assert not ((read_only | state_changing) - tools), f"{name}: classified an unserved tool"


@pytest.mark.parametrize("manifest", registered_manifests(), ids=lambda path: path.parent.name)
def test_a_networked_manifest_carries_a_credential(manifest: Path) -> None:
    """Bearer even on the loopback dev URL — an auth mode that changes with the address gets
    forgotten on the serving side the day the address changes."""
    name = manifest.parent.name
    auth = endpoint_of(manifest).get("auth", {})
    assert isinstance(auth, dict)
    assert auth.get("mode") == "bearer", f"{name} must declare bearer auth"
    assert auth.get("token_env"), f"{name} declares bearer with no token_env"


def test_ports_are_unique_and_inside_this_repository_s_block() -> None:
    """8850+ keeps the fleet clear of Chemclaw3's own 8810-8815 and the mock's 8090-8091.

    Over `manifests/`, not `servers/`: a collision between a server hosted here and one hosted
    elsewhere is exactly as fatal to a local full-stack run, and only this side can see it.
    """
    seen: dict[int, str] = {}
    for manifest in registered_manifests():
        name = manifest.parent.name
        url = endpoint_of(manifest)["url"]
        found = re.search(r":(\d+)/mcp", str(url))
        assert found, f"{name}: cannot read a port out of {url!r}"
        port = int(found.group(1))
        assert port in PORT_RANGE, f"{name} claims {port}, outside 8850-8899"
        assert port not in seen, f"{name} and {seen[port]} both claim port {port}"
        seen[port] = name


@pytest.mark.parametrize("manifest", registered_manifests(), ids=lambda path: path.parent.name)
def test_a_manifest_is_either_a_server_here_or_documented_as_hosted_elsewhere(
    manifest: Path,
) -> None:
    """Nothing was checking `manifests/` in this direction, so nothing could see an orphan.

    A manifest with no server under `servers/` is not an error — it is how this repository declares
    a capability hosted in another one, which the connector seam exists to allow. What *would* be an
    error is one nobody can trace: a directory left behind after a server was removed, still read by
    Chemclaw3, still advertising tools. So a manifest that is not a symlink into `servers/` has to
    say in `MODULES.md` where its server actually lives.
    """
    name = manifest.parent.name
    if manifest.is_symlink():
        assert manifest.resolve() == (SERVERS / name / "connector.yaml").resolve()
        return
    catalogue = (ROOT / "MODULES.md").read_text(encoding="utf-8")
    assert re.search(rf"`{re.escape(name)}`[^\n]*(hosted|repositor)", catalogue, re.IGNORECASE), (
        f"manifests/{name}/ has no server under servers/ and MODULES.md does not record where it "
        "is hosted — an orphan manifest is a capability Chemclaw3 advertises and cannot reach"
    )


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_every_server_s_sources_reach_the_type_checker(server: Path) -> None:
    """`make type` must cover this server, or its code is outside the gate that says it is checked.

    Not hypothetical. `SRC` was a hand-written list of two directories, and `rxnpredict` was added
    to neither — so `make check` and CI reported success over 15 source files while 28 more were
    never type-checked at all. A glob fixes today's instance; this fixes the next one.
    """
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    src_line = next(line for line in makefile.splitlines() if line.startswith("SRC "))
    covered = "servers/*/src" in src_line or f"servers/{server.name}/src" in src_line
    assert covered, f"{server.name}/src is not in the Makefile's SRC: {src_line!r}"
    assert (server / "src").is_dir()


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
