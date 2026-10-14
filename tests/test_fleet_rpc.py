"""Every `mount: backend` server serves `/rpc/<tool>` beside `/mcp`, and answers alike on both.

A backend is called from inside Chemclaw3's code, not by the agent, so its wire is the typed
HTTP/JSON one (`D-2026-10-14-backends-speak-typed-http-json-beside-mcp-for-one-release`). This
holds the whole backend set to that, from the packaged manifests rather than a list of names:

- every tool the manifest declares is a routed operation that validates its body against the
  contract model (a body no model accepts is a 400 `refused`, never a 404), and an operation the
  server does not serve is a 404 `unknown_op`;
- the credential is checked before the operation is looked up;
- for a fixture of deterministic calls, the `/rpc` body equals the structured answer the MCP tool
  manager gives, and satisfies the contract's response model.

The apps run in a child interpreter, as `test_fleet_contract_version` does, so importing the
backends' engines leaves this process's heap as it was. The wire-level agreement (a real MCP
session against a real listener) is each backend's own `test_server.py`, which
`test_fleet_auth.py` requires to call `assert_rpc_agrees_with_mcp`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from chemclaw_contracts import manifest_names, manifest_path
from chemclaw_contracts.rpc import RpcError
from mcp_server_kit.testing import load_manifest

ROOT = Path(__file__).resolve().parents[1]

#: Deterministic calls whose `/rpc` body must equal the tool manager's structured answer. One
#: cheap tool family per backend is enough: the dispatch is shared, so what differs per tool is the
#: model, and `servers/*/tests/test_server.py` holds a wider set over a real MCP session.
PARITY: dict[str, list[tuple[str, dict[str, Any]]]] = {
    "calc": [
        ("calculation_key", {"tool": "predict_solubility", "arguments": {"smiles": "CCO"}}),
        ("embed_structure", {"smiles": "O"}),
    ],
    "rxnlabel": [
        ("labeller_version", {}),
        ("name_reaction", {"reaction_smiles": "CC(=O)O.OCC>>CC(=O)OCC"}),
    ],
}

#: Import each backend's app, drive its routes without a socket, print what each answered.
_PROBE = """
import asyncio, importlib, json, os, sys
from fastapi.testclient import TestClient
from chemclaw_contracts import manifest_path
from mcp_server_kit.testing import load_manifest

spec = json.loads(sys.argv[1])
out = {}
for name, wanted in spec["backends"].items():
    manifest = load_manifest(manifest_path(name))
    os.environ[manifest.endpoint.auth.token_env] = "fleet-probe"
    module = importlib.import_module(f"chemclaw_mcp_{name}.app")
    tools = importlib.import_module(f"chemclaw_mcp_{name}.tools")
    client = TestClient(module.app, base_url="http://127.0.0.1:8899")
    auth = {"Authorization": "Bearer fleet-probe"}
    answer = {"tools": {}, "parity": {}}
    for tool in manifest.endpoint.tools:
        r = client.post(f"/rpc/{tool}", json={"unknown_argument": 1}, headers=auth)
        answer["tools"][tool] = [r.status_code, r.json()]
    r = client.post("/rpc/not_a_tool", json={}, headers=auth)
    answer["unknown"] = [r.status_code, r.json()]
    first = manifest.endpoint.tools[0]
    answer["anonymous"] = {
        "known": client.post(f"/rpc/{first}", json={}).status_code,
        "unknown": client.post("/rpc/not_a_tool", json={}).status_code,
    }
    for tool, arguments in wanted:
        r = client.post(f"/rpc/{tool}", json=arguments, headers=auth)
        over_mcp = asyncio.run(tools.server.call_tool(tool, arguments))[1]
        answer["parity"][tool] = [r.status_code, r.json(), over_mcp]
    out[name] = answer
json.dump(out, sys.stdout)
"""


def _backends() -> list[str]:
    """The servers whose manifest says `mount: backend`, from the package, not from a list."""
    return [
        name
        for name in (*manifest_names(), *manifest_names(internal=True))
        if load_manifest(manifest_path(name)).mount == "backend"
    ]


@pytest.fixture(scope="module")
def probed() -> dict[str, dict[str, Any]]:
    """What each backend's routes answered, from one child interpreter."""
    spec = {"backends": {name: PARITY.get(name, []) for name in _backends()}}
    run = subprocess.run(
        [sys.executable, "-c", _PROBE, json.dumps(spec)],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=600,
    )
    assert run.returncode == 0, run.stderr[-3000:]
    answers: dict[str, dict[str, Any]] = json.loads(run.stdout)
    return answers


def test_the_backends_are_the_two_this_programme_moves() -> None:
    """The set under test is read from the manifests; a third backend must bring a parity row."""
    assert sorted(_backends()) == sorted(PARITY), (
        f"backends are {_backends()} and the parity fixture covers {sorted(PARITY)}; a backend "
        "serves `/rpc` beside `/mcp` and owes a deterministic call in PARITY"
    )


@pytest.mark.parametrize("name", _backends())
def test_every_declared_tool_is_a_routed_operation_that_validates_its_body(
    name: str, probed: dict[str, dict[str, Any]]
) -> None:
    """A body no contract model accepts is refused as `refused`; a 404 would mean no route."""
    tools = probed[name]["tools"]
    declared = load_manifest(manifest_path(name)).endpoint.tools
    assert sorted(tools) == sorted(declared)
    for tool, (status, body) in tools.items():
        assert status == 400, f"{name}/rpc/{tool} answered {status}: {body}"
        assert RpcError.model_validate(body).kind == "refused", f"{name}/rpc/{tool}: {body}"


@pytest.mark.parametrize("name", _backends())
def test_an_unserved_operation_is_a_404_and_the_credential_comes_first(
    name: str, probed: dict[str, dict[str, Any]]
) -> None:
    """The route serves the contract's operations only; the credential is checked before that."""
    status, body = probed[name]["unknown"]
    assert status == 404
    assert RpcError.model_validate(body).kind == "unknown_op"
    assert probed[name]["anonymous"] == {"known": 401, "unknown": 401}


@pytest.mark.parametrize("name", _backends())
def test_rpc_returns_what_the_mcp_tool_manager_returns(
    name: str, probed: dict[str, dict[str, Any]]
) -> None:
    """For each fixture call the `/rpc` body is the tool manager's structured answer, unchanged."""
    parity = probed[name]["parity"]
    assert sorted(parity) == sorted(tool for tool, _ in PARITY[name])
    for tool, (status, over_rpc, over_mcp) in parity.items():
        assert status == 200, f"{name}/rpc/{tool}: {over_rpc}"
        assert over_rpc == over_mcp, f"{name}/{tool}: /rpc and the tool manager disagree"
