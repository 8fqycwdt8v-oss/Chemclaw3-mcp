"""The package owns every manifest, and the repository's paths to them are links, not copies."""

from __future__ import annotations

import importlib.metadata
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path

import chemclaw_contracts as contracts
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]


def _servers() -> list[str]:
    """Every directory under `servers/` that declares a manifest."""
    return sorted(p.name for p in (ROOT / "servers").iterdir() if (p / "connector.yaml").exists())


def test_the_version_is_one_semver_string_the_wheel_agrees_with() -> None:
    """`__version__` is the only spelling; the installed metadata reads it."""
    assert re.fullmatch(r"\d+\.\d+\.\d+", contracts.__version__)
    assert importlib.metadata.version("chemclaw-contracts") == contracts.__version__


def test_every_server_has_exactly_one_manifest_here() -> None:
    """The package serves the connectors and the backends of `servers/`, no more and no fewer."""
    served = [*contracts.manifest_names(), *contracts.manifest_names(internal=True)]
    assert sorted(served) == _servers()
    assert len(set(served)) == len(served), "a name is both a connector and a backend"


@pytest.mark.parametrize("name", _servers())
def test_the_repository_paths_are_links_to_the_package_file(name: str) -> None:
    """`servers/<n>/connector.yaml` and the mount bucket resolve to the packaged file."""
    packaged = contracts.manifest_path(name)
    mount = yaml.safe_load(packaged.read_text(encoding="utf-8")).get("mount", "connector")
    bucket = "manifests" if mount == "connector" else "manifests-internal"
    for link in (ROOT / "servers" / name, ROOT / bucket / name):
        assert (link / "connector.yaml").is_symlink(), f"{link} must be a link, not a copy"
        assert (link / "connector.yaml").resolve() == packaged.resolve()


def test_the_connector_directory_holds_no_backend() -> None:
    """`manifests_dir()` is mountable as `CHEMCLAW_CONNECTORS_DIR`: nothing in it is `mount:`ed."""
    for name in contracts.manifest_names():
        assert "mount" not in yaml.safe_load(contracts.manifest_path(name).read_text("utf-8"))
    for name in contracts.manifest_names(internal=True):
        declared = yaml.safe_load(contracts.manifest_path(name).read_text("utf-8"))
        assert declared["mount"] == "backend"


def test_an_unknown_name_names_what_is_served() -> None:
    """The refusal lists the served names, so a typo reads as one."""
    with pytest.raises(KeyError, match=r"no manifest of that name.*chem"):
        contracts.manifest_path("no-such-server")


def test_a_declared_version_is_read_and_a_malformed_one_is_refused(tmp_path: Path) -> None:
    """`MAJOR.MINOR.PATCH` or nothing; anything else is a manifest error, never a guess."""
    path = tmp_path / "connector.yaml"
    path.write_text("name: probe\n", encoding="utf-8")
    assert contracts.declared_contract_version(path) is None
    path.write_text("name: probe\ncontract_version: 1.2.3\n", encoding="utf-8")
    assert contracts.declared_contract_version(path) == "1.2.3"
    for bad in ("1.0", "v1.0.0", "1.0.0-rc1", "1"):
        path.write_text(f"name: probe\ncontract_version: '{bad}'\n", encoding="utf-8")
        with pytest.raises(ValueError, match="MAJOR\\.MINOR\\.PATCH"):
            contracts.declared_contract_version(path)
    path.write_text("name: probe\ncontract_version: 1.0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="MAJOR\\.MINOR\\.PATCH"):
        contracts.declared_contract_version(path)


def test_the_built_wheel_carries_every_manifest() -> None:
    """A consumer installs the wheel, so the manifests must be in it, not only in the tree."""
    with tempfile.TemporaryDirectory() as out:
        built = subprocess.run(
            [
                "uv",
                "build",
                "--wheel",
                "--offline",
                "--out-dir",
                out,
                str(ROOT / "packages" / "chemclaw_contracts"),
            ],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        assert built.returncode == 0, built.stderr
        (wheel,) = Path(out).glob("*.whl")
        names = set(zipfile.ZipFile(wheel).namelist())
    for name in contracts.manifest_names():
        assert f"chemclaw_contracts/manifests/{name}/connector.yaml" in names
    for name in contracts.manifest_names(internal=True):
        assert f"chemclaw_contracts/manifests_internal/{name}/connector.yaml" in names
    assert "chemclaw_contracts/py.typed" in names
