"""The `connector.yaml` model: what it refuses, and why refusing it *here* is the point.

Chemclaw3 models these files with `extra="forbid"`, so a bad manifest aborts its startup. This
repository owns the manifests, so the refusal belongs in this suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from mcp_server_kit.testing import load_manifest

ROOT = Path(__file__).resolve().parents[3]

# The `endpoint:` block is a name of its own because every case below rebuilds it with one key
# changed, and `COMPLETE["endpoint"]` is an `object` to any reader that does not already know the
# shape — including this one.
ENDPOINT: dict[str, object] = {
    "transport": "http",
    "url": "http://127.0.0.1:8850/mcp",
    "health_url": "http://127.0.0.1:8850/healthz",
    "request_timeout": 15,
    "auth": {"mode": "bearer", "token_env": "PROBE_TOKEN"},
    "tools": ["a_tool"],
    "read_only": ["a_tool"],
}

COMPLETE: dict[str, object] = {
    "name": "probe",
    "description": "a probe server",
    "endpoint": ENDPOINT,
}


def _written(tmp_path: Path, manifest: dict[str, object]) -> Path:
    path = tmp_path / "connector.yaml"
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return path


def test_a_complete_manifest_validates(tmp_path: Path) -> None:
    """The happy path, and the fields the callers reach through."""
    manifest = load_manifest(_written(tmp_path, COMPLETE))
    assert manifest.name == "probe"
    assert manifest.mount == "connector"
    assert manifest.endpoint.auth.token_env == "PROBE_TOKEN"
    assert manifest.endpoint.tools == ["a_tool"]
    assert manifest.endpoint.state_changing == []


@pytest.mark.parametrize(
    ("label", "endpoint"),
    [
        ("no transport", {k: v for k, v in ENDPOINT.items() if k != "transport"}),
        ("a bare tools key", {**ENDPOINT, "tools": None}),
        ("a bare read_only key", {**ENDPOINT, "read_only": None}),
        ("a bare state_changing key", {**ENDPOINT, "state_changing": None}),
        ("an empty tools list", {**ENDPOINT, "tools": [], "read_only": []}),
        ("no tools key", {k: v for k, v in ENDPOINT.items() if k not in ("tools", "read_only")}),
    ],
)
def test_an_endpoint_the_consumer_cannot_load_is_refused_here(
    tmp_path: Path, label: str, endpoint: dict[str, object]
) -> None:
    """Each of these would abort Chemclaw3's startup with a `ConnectorError`, so it is refused here.

    No `transport:` fails the discriminated union, a bare list key is YAML `None`, and an empty or
    absent `tools` fails classification. Coercing them would pass here on a file the consumer cannot
    load.
    """
    with pytest.raises(ValueError, match="not a connector manifest"):
        load_manifest(_written(tmp_path, {**COMPLETE, "endpoint": endpoint}))


def test_a_bare_top_level_list_key_is_refused_as_the_consumer_refuses_it(tmp_path: Path) -> None:
    """`skills:` with nothing under it is `None`, and the consumer's `list[str]` refuses `None`."""
    with pytest.raises(ValueError, match="skills"):
        load_manifest(_written(tmp_path, {**COMPLETE, "skills": None}))


def test_default_enabled_is_a_field_the_fleet_can_declare(tmp_path: Path) -> None:
    """The consumer's `default_enabled`, which a shadowing fleet manifest has to be able to say.

    A fleet manifest that wins the name collision must be able to carry `false`, or it binds every
    tool schema on every model call.
    """
    assert load_manifest(_written(tmp_path, COMPLETE)).default_enabled is True
    declared = load_manifest(_written(tmp_path, {**COMPLETE, "default_enabled": False}))
    assert declared.default_enabled is False


def test_a_key_this_fleet_invents_is_refused_here_rather_than_at_chemclaw3_s_startup(
    tmp_path: Path,
) -> None:
    """`extra="forbid"`, in the repository that owns the file.

    An `arguments:` key under `endpoint:` is the concrete case: Chemclaw3 would refuse it.
    """
    manifest = dict(COMPLETE)
    manifest["endpoint"] = {**ENDPOINT, "arguments": {"a_tool": {}}}
    with pytest.raises(ValueError, match="arguments"):
        load_manifest(_written(tmp_path, manifest))
    with pytest.raises(ValueError, match="mystery"):
        load_manifest(_written(tmp_path, {**COMPLETE, "mystery": 1}))


def test_auth_cannot_be_omitted_or_declared_none(tmp_path: Path) -> None:
    """Bearer auth is required on every manifest, including the loopback dev URL.

    Chemclaw3 accepts `mode: none` for loopback only, which would make auth a function of the
    address; here neither omission nor `mode: none` is representable.
    """
    without = {
        **COMPLETE,
        "endpoint": {k: v for k, v in ENDPOINT.items() if k != "auth"},
    }
    with pytest.raises(ValueError, match="auth"):
        load_manifest(_written(tmp_path, without))
    none_mode = dict(COMPLETE)
    none_mode["endpoint"] = {**ENDPOINT, "auth": {"mode": "none"}}
    with pytest.raises(ValueError, match="bearer"):
        load_manifest(_written(tmp_path, none_mode))


def test_every_shipped_manifest_in_this_repository_validates() -> None:
    """Both manifest directories validate against the model, without a running server.

    A misspelled `mount: backend` under `manifests-internal/` would otherwise surface only at
    Chemclaw3's startup.
    """
    manifests = sorted(ROOT.glob("servers/*/connector.yaml"))
    assert len(manifests) >= 8, f"only {len(manifests)} manifests found — the glob is wrong"
    mounts = {path.parent.name: load_manifest(path).mount for path in manifests}
    assert set(mounts) == {path.parent.name for path in manifests}
    assert mounts["calc"] == "backend", "calc is a backend and its manifest must say so"


@pytest.mark.parametrize("version", ["1.0.0", "0.12.345", "10.0.1"])
def test_a_contract_version_is_a_semver_string(tmp_path: Path, version: str) -> None:
    """`MAJOR.MINOR.PATCH`, digits only: the shape the consumer's model accepts."""
    manifest = load_manifest(_written(tmp_path, {**COMPLETE, "contract_version": version}))
    assert manifest.contract_version == version


@pytest.mark.parametrize("version", ["1.0", "v1.0.0", "1.0.0-rc1", "1.0.0.0", "", 1])
def test_a_contract_version_that_is_not_semver_is_refused(tmp_path: Path, version: object) -> None:
    """Anything else is a manifest error here, before it is one at the consumer's startup."""
    with pytest.raises(ValueError, match="not a connector manifest"):
        load_manifest(_written(tmp_path, {**COMPLETE, "contract_version": version}))


def test_a_manifest_without_a_contract_version_still_validates(tmp_path: Path) -> None:
    """The field is optional, as it is in the consumer's model."""
    assert load_manifest(_written(tmp_path, COMPLETE)).contract_version is None
