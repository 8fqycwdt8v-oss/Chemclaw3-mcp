"""Every `compute: true` server answers `calculation_key` over `/rpc` for every compute tool.

Chemclaw3 never recomputes a persisted result, so a compute server owes the key a result *would*
be stored under before it runs, or a caveat saying why there is none
(`D-2026-10-21-every-compute-server-serves-a-calculation-key`). This asks every such server, for
every tool its manifest declares:

- a compute tool (a row in `EXAMPLES`) answers 200 with a `ComputeIdentity` whose key is set or
  whose caveat says why not — the contract model refuses a bare null — or, where the key names a
  program this machine lacks, a refusal naming that program;
- every other tool is refused with the fleet's "not a compute tool" phrase, so a compute tool added
  without a row here fails rather than passing unasked.

The apps run in a child interpreter, as `test_fleet_rpc` does, so importing the engines leaves this
process's heap as it was.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from chemclaw_contracts import manifest_names, manifest_path
from chemclaw_contracts.compute import ComputeIdentity
from chemclaw_contracts.rpc import RpcError
from mcp_server_kit.keys import NOT_A_COMPUTE_TOOL
from mcp_server_kit.testing import load_manifest

ROOT = Path(__file__).resolve().parents[1]

_WATER: dict[str, Any] = {
    "elements": [8, 1, 1],
    "positions": [[0.0, 0.0, 0.1173], [0.0, 0.7572, -0.4692], [0.0, -0.7572, -0.4692]],
    "smiles": "O",
}

#: Per compute server, per compute tool: the arguments to key, and the program whose absence on
#: this machine makes a refusal (naming it) the honest answer instead of a key.
EXAMPLES: dict[str, dict[str, tuple[dict[str, Any], str | None]]] = {
    "calc": {
        "compute_xtb_energy": ({"smiles": "CCO"}, None),
        "compute_electronic_properties": ({"smiles": "CCO"}, None),
        "predict_site_reactivity": ({"smiles": "CCO"}, None),
        "compute_atomic_descriptors": ({"smiles": "CCO"}, "xtb"),
        "compute_surface_potential": ({"smiles": "CCO"}, "xtb"),
        "optimize_geometry": ({"smiles": "CCO"}, None),
        "predict_pka": ({"smiles": "CC(=O)O"}, None),
        "predict_solubility": ({"smiles": "CCO"}, None),
        "predict_logd": ({"smiles": "CC(=O)O"}, None),
        "predict_developability_profile": ({"smiles": "CCO"}, None),
        "relax_structure": ({"structure": _WATER}, None),
        "compute_properties_at": ({"structure": _WATER}, None),
        "compute_fukui_at": ({"structure": _WATER}, None),
        "compute_hessian": ({"structure": _WATER}, None),
        "scan_point": ({"structure": _WATER, "atoms": [0, 1], "value": 1.0}, None),
        "search_conformer_ensemble": ({"structure": _WATER}, "crest"),
        "search_binding_modes": ({"structure": _WATER}, "crest"),
    },
}

#: Import each compute server's app, ask `calculation_key` about every declared tool over `/rpc`.
_PROBE = """
import importlib, json, os, sys
from fastapi.testclient import TestClient
from chemclaw_contracts import manifest_path
from mcp_server_kit.testing import load_manifest

spec = json.loads(sys.argv[1])
out = {}
for name, examples in spec.items():
    manifest = load_manifest(manifest_path(name))
    os.environ[manifest.endpoint.auth.token_env] = "fleet-probe"
    module = importlib.import_module(f"chemclaw_mcp_{name}.app")
    client = TestClient(module.app, base_url="http://127.0.0.1:8899")
    auth = {"Authorization": "Bearer fleet-probe"}
    answers = {}
    for tool in manifest.endpoint.tools:
        if tool == "calculation_key":
            continue
        body = {"tool": tool, "arguments": examples.get(tool, {})}
        r = client.post("/rpc/calculation_key", json=body, headers=auth)
        answers[tool] = [r.status_code, r.json()]
    out[name] = answers
json.dump(out, sys.stdout)
"""


def _compute_servers() -> list[str]:
    """The servers whose packaged manifest declares `compute: true`."""
    return [
        name
        for name in (*manifest_names(), *manifest_names(internal=True))
        if load_manifest(manifest_path(name)).compute
    ]


@pytest.fixture(scope="module")
def keyed() -> dict[str, dict[str, list[Any]]]:
    """What `calculation_key` answered for every declared tool of every compute server."""
    spec = {
        name: {tool: arguments for tool, (arguments, _) in EXAMPLES.get(name, {}).items()}
        for name in _compute_servers()
    }
    run = subprocess.run(
        [sys.executable, "-c", _PROBE, json.dumps(spec)],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=600,
    )
    assert run.returncode == 0, run.stderr[-3000:]
    answers: dict[str, dict[str, list[Any]]] = json.loads(run.stdout)
    return answers


def test_every_compute_server_has_a_row_of_examples() -> None:
    """The set under test is read from the manifests; a new compute server must bring its rows."""
    assert sorted(_compute_servers()) == sorted(EXAMPLES), (
        f"compute servers are {_compute_servers()} and the examples cover {sorted(EXAMPLES)}"
    )


@pytest.mark.parametrize("name", _compute_servers())
def test_every_compute_tool_answers_a_key_or_says_why_not(
    name: str, keyed: dict[str, dict[str, list[Any]]]
) -> None:
    """200 with a key or a caveat; or, where the program is absent here, a refusal naming it."""
    for tool, (_, needs) in EXAMPLES[name].items():
        status, body = keyed[name][tool]
        if needs is not None and shutil.which(needs) is None:
            assert status == 400, f"{name}/{tool} keyed a calculation needing absent {needs}"
            assert needs in RpcError.model_validate(body).message, f"{name}/{tool}: {body}"
            continue
        assert status == 200, f"{name}/{tool}: calculation_key answered {status}: {body}"
        identity = ComputeIdentity.model_validate(body)
        assert identity.tool == tool
        if identity.key is not None:
            assert identity.calc_key == (
                f"{identity.key.calc_type}@{identity.key.calc_version}:"
                f"{identity.key.input_hash}:{identity.key.params_hash}"
            )


@pytest.mark.parametrize("name", _compute_servers())
def test_a_tool_with_no_example_is_refused_as_not_a_calculation(
    name: str, keyed: dict[str, dict[str, list[Any]]]
) -> None:
    """A compute tool added without a row would fail here on its missing subject, not pass."""
    for tool, (status, body) in keyed[name].items():
        if tool in EXAMPLES[name]:
            continue
        assert status == 400, f"{name}/{tool}: {body}"
        message = RpcError.model_validate(body).message
        assert NOT_A_COMPUTE_TOOL in message, (
            f"{name}/{tool} is not refused as a non-compute tool ({message!r}); if it computes, "
            "give it a row in EXAMPLES"
        )


def test_a_null_key_without_a_caveat_is_refused_by_the_contract() -> None:
    """The check above rests on the model refusing the one answer a caller cannot act on."""
    with pytest.raises(ValueError, match="no key and no caveat"):
        ComputeIdentity.model_validate({"tool": "t", "calc_version": "v"})
    with pytest.raises(ValueError, match="both set or both None"):
        ComputeIdentity.model_validate({"tool": "t", "calc_version": "v", "calc_key": "t@v:a:b"})
