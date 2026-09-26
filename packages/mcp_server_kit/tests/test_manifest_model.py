"""The `connector.yaml` model: what it refuses, and why refusing it *here* is the point.

`assert_manifest_matches` used to walk a raw dict defensively — `endpoint.get("tools") or []` with
a comment at the call site explaining that a bare `tools:` key parses to `None`. That is a model,
written by hand, in a package that already ships pydantic, and it could see nothing it had not been
told to look for.

The repository that *reads* these files models them and is `extra="forbid"`, so a key invented here
aborts Chemclaw3's startup rather than this suite. This repository owns the manifests, so the
refusal belongs in this suite: the same argument
`D-2026-09-14-the-gate-that-catches-a-change-is-the-gate-of-the-tree-it-is-made-in` makes for a
change, applied to a declaration.
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
    """Each of these validated here once and aborts Chemclaw3's startup with a `ConnectorError`.

    Measured against the consumer's own `ConnectorManifest.model_validate`: no `transport:` is
    `union_tag_not_found` (the endpoint is a discriminated union there), a bare list key is YAML's
    `None` and a `list_type` error, and an empty or absent `tools` is refused by the classification
    validator. This model used to *coerce* the bare keys to `[]`, which made this suite green on a
    manifest the one reader that matters cannot load.
    """
    with pytest.raises(ValueError, match="not a connector manifest"):
        load_manifest(_written(tmp_path, {**COMPLETE, "endpoint": endpoint}))


def test_a_bare_top_level_list_key_is_refused_as_the_consumer_refuses_it(tmp_path: Path) -> None:
    """`skills:` with nothing under it is `None`, and the consumer's `list[str]` refuses `None`."""
    with pytest.raises(ValueError, match="skills"):
        load_manifest(_written(tmp_path, {**COMPLETE, "skills": None}))


def test_default_enabled_is_a_field_the_fleet_can_declare(tmp_path: Path) -> None:
    """The consumer's `default_enabled`, which a shadowing fleet manifest has to be able to say.

    A fleet manifest wins the name collision over the consumer's own copy when this fleet's
    `manifests/` comes first on `CHEMCLAW_CONNECTORS_DIR`. If the consumer's copy says `false` and
    this model cannot represent the key, the shadow carries the default `true` and binds every tool
    schema it declares on every model call.
    """
    assert load_manifest(_written(tmp_path, COMPLETE)).default_enabled is True
    declared = load_manifest(_written(tmp_path, {**COMPLETE, "default_enabled": False}))
    assert declared.default_enabled is False


def test_a_key_this_fleet_invents_is_refused_here_rather_than_at_chemclaw3_s_startup(
    tmp_path: Path,
) -> None:
    """`extra="forbid"`, in the repository that owns the file.

    An `arguments:` key under `endpoint:` is the concrete case: `test_manifest_surface.py`'s module
    docstring records that it was considered and rejected *because* Chemclaw3 would refuse it. Until
    this model existed nothing on this side would have noticed somebody adding it anyway.
    """
    manifest = dict(COMPLETE)
    manifest["endpoint"] = {**ENDPOINT, "arguments": {"a_tool": {}}}
    with pytest.raises(ValueError, match="arguments"):
        load_manifest(_written(tmp_path, manifest))
    with pytest.raises(ValueError, match="mystery"):
        load_manifest(_written(tmp_path, {**COMPLETE, "mystery": 1}))


def test_auth_cannot_be_omitted_or_declared_none(tmp_path: Path) -> None:
    """`CLAUDE.md` requires bearer on every manifest, *including the loopback dev URL*.

    Chemclaw3's model accepts `mode: none` for a loopback address, and would refuse it the moment a
    deployment moved that address — which makes a manifest's auth mode a function of where it is
    pointed. This fleet's rule is stricter and was a review convention until now: neither omission
    nor `mode: none` is representable here.
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
    """Both manifest directories, against the model, in one place.

    Each server's own `test_server.py` validates its manifest as a side effect of
    `assert_manifest_matches`, which is the direction that matters at call time. This is the
    direction that does not need a running server: a manifest under `manifests-internal/` whose
    `mount: backend` was misspelled would still be refused by Chemclaw3 — with a `ConnectorError`
    at *its* startup, which is the one place nobody here can watch.
    """
    manifests = sorted(ROOT.glob("servers/*/connector.yaml"))
    assert len(manifests) >= 8, f"only {len(manifests)} manifests found — the glob is wrong"
    mounts = {path.parent.name: load_manifest(path).mount for path in manifests}
    assert set(mounts) == {path.parent.name for path in manifests}
    assert mounts["calc"] == "backend", "calc is a backend and its manifest must say so"
