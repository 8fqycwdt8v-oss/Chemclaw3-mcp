"""Manifests and the served surface: registration by mount, classification, credentials, readiness,
and the admission ceilings a manifest's `queued:` list must match.
"""

from __future__ import annotations

import ast
import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


SERVERS = ROOT / "servers"


MANIFESTS = ROOT / "manifests"


INTERNAL_MANIFESTS = ROOT / "manifests-internal"


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


def declared_mount(server: Path) -> str:
    """What this server says it is: a connector Chemclaw3 dials, or a backend it calls."""
    mount = manifest_of(server).get("mount", "connector")
    assert mount in MOUNTS, f"{server.name} declares mount: {mount!r}; expected one of {[*MOUNTS]}"
    return str(mount)


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_the_manifest_is_registered_by_symlink_in_the_bucket_it_declares(server: Path) -> None:
    """Two directories, because two of these servers must never be mounted as connectors."""
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
    """The second layer: a backend's manifest cannot load as a connector even if found."""
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


def test_no_server_registers_a_resource_or_a_prompt() -> None:
    """`connector_app`'s caller-rebind and error-sanitize only cover `server._tool_manager`."""
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


def _called_name(node: ast.Call) -> str:
    """The bare name a call names, whatever it hangs off — `os.getenv` and `getenv` read the
    same."""
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else ""


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_every_server_hands_connector_app_a_readiness_check(server: Path) -> None:
    """`/healthz` is readiness, so every server must give `connector_app` something to consult."""
    app = next((server / "src").glob("*/app.py"), None)
    assert app is not None, f"{server.name} has no src/<package>/app.py"
    tree = ast.parse(app.read_text(encoding="utf-8"), filename=str(app))
    passed = {
        keyword.arg: keyword.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _called_name(node) == "connector_app"
        for keyword in node.keywords
    }
    assert "readiness" in passed, (
        f"{app.relative_to(ROOT)} calls connector_app without `readiness=`, so /healthz is a "
        "constant 200 and this pod takes traffic whatever state it is in."
    )
    nones = [
        child
        for child in ast.walk(passed["readiness"])
        if isinstance(child, ast.Constant) and child.value is None
    ]
    assert not nones, (
        f"{app.relative_to(ROOT)} can pass `readiness=None` to connector_app, which is the "
        "constant-200 branch: /healthz then answers 200 with a corpus that failed its checksum. "
        "Pass a callable unconditionally, or make the degraded case the callable's answer."
    )


# What one of these subprocesses does: refuse every vendored corpus at the loader, then import the
# server and ask its probe. Run out of process because the import is the subject — a module already
# in `sys.modules` from another test would make the answer depend on collection order — and because
# patching a module-level `from mcp_server_kit import load_dataset` binding after the fact is not
# the thing a corrupt file does.
_DRIVER = """
import importlib, json, sys

import mcp_server_kit
import mcp_server_kit.datasets as datasets

MARKER = "driven: this corpus is not the one the manifest approved"


def _refuse(*_args, **_kwargs):
    raise datasets.DatasetError(MARKER)


# Both names: a server reaches the loader through the package re-export, and the package binding is
# what `from mcp_server_kit import load_dataset` already copied into each engine module.
datasets.load_dataset = _refuse
mcp_server_kit.load_dataset = _refuse

package = sys.argv[1]
importlib.import_module(package + ".tools")
app = importlib.import_module(package + ".app").app

from fastapi.testclient import TestClient

with TestClient(app) as client:
    answer = client.get("/healthz")
print(json.dumps({"status": answer.status_code, "body": answer.json(), "marker": MARKER}))
"""


def _dataset_servers() -> list[Path]:
    """Every server that loads a vendored corpus, derived from the corpora rather than listed.

    `dataset.json` is what `mcp_server_kit.load_dataset` refuses without, so its presence under a
    server's package is this repository's own definition of "has a corpus to be corrupt". Derived
    so that the ninth server to vendor a table owes this proof the day it does, and so that a
    server which *stops* carrying one drops out instead of leaving a test asserting nothing.
    """
    return [server for server in server_dirs() if any((server / "src").glob("*/**/dataset.json"))]


@pytest.mark.parametrize("server", _dataset_servers(), ids=lambda path: path.name)
def test_a_corrupt_corpus_is_the_probe_s_answer_rather_than_an_import_error(server: Path) -> None:
    """A corpus that fails its checksum makes this pod **unready**, never unable to start."""
    package = next((server / "src").glob("*/app.py")).parent.name
    finished = subprocess.run(
        [sys.executable, "-c", _DRIVER, package],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert finished.returncode == 0, (
        f"{server.name} could not be imported with its corpus refused, so a `records.csv` that "
        "failed its checksum would take the pod down before `/healthz` could say why:\n"
        f"{finished.stderr[-2000:]}"
    )
    answered = json.loads(finished.stdout.strip().splitlines()[-1])
    assert answered["status"] == 503, (
        f"{server.name} imported with every corpus refused and then answered "
        f"{answered['status']} from /healthz: {answered['body']}. A pod that cannot read its "
        "table must be kept out of its Service, not left in it answering from nothing."
    )
    assert answered["marker"] in str(answered["body"]), (
        f"{server.name} answered 503 without naming what failed: {answered['body']}. The reason "
        "is the whole difference this test is about — an operator reads it off the probe instead "
        "of off a crash loop."
    )


# Which servers answer for their own concurrency, and which are argued not to need to.
#
# **A ceiling is `engine/admission.py`**, and each server that ships one is held by its own module.
# `kinetics` was argued out of one at 836 µs and moved in when its integrator's step count became
# the caller's to set. The absence of a ceiling in an eighth server was held by
# nobody, which `docs/BACKLOG.md` recorded as "an eighth server without one passes every test here"
# — and then an eighth server arrived (`thermalsafety`) with exactly that shape: an argued absence
# in a README that no test reads.
#
# It cannot be derived from the manifest. `D-2026-09-12-one-tool-call-is-not-one-thread` measured
# that the `read_only`/`state_changing` split does not carry, because every `chem` tool is
# `read_only`, correctly, and the heavy band of them shares a ceiling. So the rule is the same
# shape as `BLIND_ANSWER_IS_ARGUED`: present, or argued here, and checked in both directions.
#
# Each argument below is a measurement rather than an adjective, because "it is fast" is what every
# server's author believes on the day they write it.
# Every figure below is **engine CPU per call** — `time.process_time` around the engine function,
# warmed so the lazy dataset load is not in the average. One basis for all three deliberately: the
# first draft of this table mixed 11.7 ms for `props` (a whole MCP round trip) with 63.7 µs for
# `thermalsafety` (the engine alone) and read as though one were 200x the other, when the two
# numbers were measuring different things. What a ceiling protects is the pod's CPU, so that is
# what is measured; the transport each call also pays is the same for every server in this fleet
# and is what the millisecond figures were mostly made of.
CEILING_IS_ARGUED_ABSENT = {
    # A dict lookup and a bisection over a 44-row vendored table: 0.7 µs for the lookup, 1.8 µs for
    # `vapour_pressure`, and 10.3 µs for a Hansen sweep across the whole table — which is the
    # largest single call `MAX_COMPARED_SOLVENTS` permits, since that bound *is* the table's size.
    # It was set after 100 000 x "dcm" was measured at 14.83 s holding the event loop with a
    # `/healthz` probe stuck behind it, which is the input bound rather than a concurrency one.
    "props": "a table lookup and a bisection, with its one unbounded input bounded",
    # An RDKit screen against fixed alert tables, already under a component bound: 321 µs to screen
    # a 37-heavy-atom drug structure (imatinib), 339 µs for the genotoxic alert pass. RDKit holds
    # the GIL, which is why `chem` needs a ceiling and this does not: `render_structure` generates
    # 2D coordinates and draws, tens of milliseconds, two orders of magnitude above a substructure
    # match against a fixed table.
    "safety": "a bounded screen over fixed tables, with no depiction and no subprocess",
    # Closed-form arithmetic over `math`: 1.9 µs to 4.9 µs for the six single-peak tools and
    # 29.8 µs for `system_suitability_report` over a three-peak table with two six-injection
    # series — most of even those being pydantic building the result model rather than any
    # chromatography. No subprocess, no pinned thread, and both list inputs are bounded
    # (`MAX_INJECTIONS`, `MAX_PEAKS`) so the cost cannot run away unpriced.
    "suitability": "closed-form arithmetic, transport-bound, with both list inputs bounded",
    # Closed-form arithmetic over the standard library: 0.25 µs for `adiabatic_temperature_rise`,
    # 63.7 µs for `tmr_ad` (a fixed 200-step bisection, the slowest of the seven) and 4.5 µs for
    # `oxygen_balance_screen`. No subprocess, no pinned thread, and `MAX_FORMULA_CHARACTERS` bounds
    # the one input whose length was unbounded.
    "thermalsafety": "closed-form arithmetic, transport-bound, with its one input bounded",
    # Closed-form correlations over `math`: 1.4 µs for `crystallisation_yield`, 12.2 µs for
    # `shortcut_distillation` (a fixed 200-step bisection for Underwood's root, the widest thing
    # this server does) and 1.6-7.2 µs for the other five. No subprocess, no pinned thread, and no
    # list input — every argument is one scalar, so there is no input whose length could run the
    # cost away and nothing for a ceiling to bound. For scale: one whole call over a real MCP
    # session on loopback is 8.49 ms, so the arithmetic is ~0.1% of what the pod spends serving it.
    "unitops": "closed-form correlations with one fixed-step solver, measured at 12.2 µs at its "
    "widest",
}


def _servers_with_a_ceiling() -> set[str]:
    """Every server shipping an `engine/admission.py`, by directory name."""
    return {
        server.name
        for server in server_dirs()
        if next((server / "src").glob("*/engine/admission.py"), None) is not None
    }


def test_every_server_either_bounds_its_concurrency_or_argues_why_it_need_not() -> None:
    """A ninth server owes the same answer, which is the whole point of deriving it."""
    declared = {server.name for server in server_dirs()}
    assert declared, "no servers found; has the workspace layout changed?"
    unaccounted = sorted(declared - _servers_with_a_ceiling() - set(CEILING_IS_ARGUED_ABSENT))
    assert not unaccounted, (
        f"{unaccounted} bound nothing about how many of their tools may run at once, and no "
        "argument says why they need not. Add an `engine/admission.py` (see "
        "`servers/calc/src/chemclaw_mcp_calc/engine/admission.py`), or an entry in "
        "CEILING_IS_ARGUED_ABSENT with the measurement — not the adjective — behind it."
    )


def test_no_server_is_argued_out_of_a_ceiling_it_actually_has() -> None:
    """The other direction, and the one that makes the first mean something over time.

    A server that grows real work adds a ceiling; if its exemption stays, the next reader finds an
    argument for "this one is arithmetic" beside a module bounding its concurrency, and cannot tell
    which is true. The same rule `test_the_argued_blind_handlers_are_still_there` states for the
    allowlist above, and the same rule `DEFERRED.md` states for a closed row: delete it in the
    commit that closes it.
    """
    declared = {server.name for server in server_dirs()}
    contradicted = sorted(set(CEILING_IS_ARGUED_ABSENT) & _servers_with_a_ceiling())
    assert not contradicted, (
        f"{contradicted} ship an `engine/admission.py` and are also argued not to need one. "
        "Delete the CEILING_IS_ARGUED_ABSENT entry in the commit that added the ceiling."
    )
    gone = sorted(set(CEILING_IS_ARGUED_ABSENT) - declared)
    assert not gone, (
        f"CEILING_IS_ARGUED_ABSENT names servers that are not here: {gone}. Delete the entry with "
        "the server."
    )


@pytest.mark.parametrize("server", sorted(_servers_with_a_ceiling()))
def test_a_gated_call_its_signature_refuses_costs_no_slot(server: str) -> None:
    """Every gated tool, in every server with a ceiling, builds its work before it charges for it.

    All six gates charged first — `acquire`, then `work(*args, **kwargs)` — and calling an
    `async def` binds its arguments on the spot, so a call the signature refused raised `TypeError`
    between the charge and the only code that gives a slot back. Driven on `kinetics` before the
    fix, one such call left `in_flight` at 1, which at a ceiling of one is a pod that refuses every
    well-formed call for the rest of its life. `mcp_server_kit.limits.Admission.admit` takes the
    already-built awaitable, so the order cannot be written backwards through it; this holds each
    server to going through it, derived from the served surface rather than a list of tool names.
    """
    import asyncio

    package = f"chemclaw_mcp_{server}"
    tools = importlib.import_module(f"{package}.tools")
    marker = importlib.import_module(f"{package}.engine.admission").ADMISSION_MARKER
    gated = [
        tool.fn
        for tool in tools.server._tool_manager.list_tools()
        if getattr(tool.fn, marker, False)
    ]
    assert gated, f"{server} ships a ceiling and gates none of its served tools"
    for fn in gated:
        with pytest.raises(TypeError):
            asyncio.run(fn(not_an_argument_of_any_tool=1))
        assert tools._admission.in_flight == 0, (
            f"{server}'s {fn.__name__} kept a slot for a call that never started: the gate charges "
            "before it builds the work. Route it through `_admission.admit(work(...), ...)`."
        )


def _gated_servers() -> list[str]:
    """Every server with an admission gate, from the gate modules on disk."""
    return sorted(path.parents[3].name for path in SERVERS.glob("*/src/*/engine/admission.py"))


@pytest.mark.parametrize("name", _gated_servers())
def test_a_full_pod_says_so_in_the_fleet_s_one_format(name: str) -> None:
    """Every gate refuses with `[<its own name>-at-capacity]` at the head of the message.

    That token is the only thing that tells a caller "retry shortly" rather than "your input is
    wrong", so a server that refuses without it has its full pods read as bad requests and never
    retried — which is what five of six did before the format moved into the kit. Driven through
    each server's real `acquire` on a full gate, because the property is what reaches the wire, and
    a gate named after another server would mint a marker the caller attributes to the wrong pod.
    """
    import importlib

    from mcp_server_kit.limits import AtCapacityError, at_capacity_marker

    module = importlib.import_module(f"chemclaw_mcp_{name}.engine.admission")
    gate = module.Admission(1)
    assert gate.take().charged == 1
    with pytest.raises(AtCapacityError) as refused:
        gate.acquire("probe")
    assert str(refused.value).startswith(f"{at_capacity_marker(name)} ")


def _admission_gated_tools(server: Path) -> set[str]:
    """The tools a server's `tools.py` decorates with its admission gate, read from the tree.

    An AST read rather than an import: the gate's decorator is `_admitted` — or, in `rxnpredict`,
    a cost-specific `_admitted_<kind>` — in every gated server (the coverage tests beside each one
    hold that), and importing `rxnpredict`'s tools would load its predictor stack to answer a
    question about decorators.
    """
    gated: set[str] = set()
    for path in server.glob("src/*/tools.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and any(
                isinstance(d, ast.Name) and d.id.startswith("_admitted")
                for d in node.decorator_list
            ):
                gated.add(node.name)
    return gated


def _agent_facing_servers() -> list[str]:
    """Every server in `manifests/` — the ones Chemclaw3's agent can call, gated or not."""
    return sorted(path.parent.name for path in (ROOT / "manifests").glob("*/connector.yaml"))


def test_the_gate_reader_finds_every_gated_server() -> None:
    """`_admission_gated_tools` sees a gate wherever a server ships `engine/admission.py`.

    Without this, a renamed decorator would empty every gated set at once, and the test below
    would pass on every server by comparing an empty manifest list with an empty code list.
    """
    shipping = {
        name
        for name in _agent_facing_servers()
        if any((SERVERS / name).glob("src/*/engine/admission.py"))
    }
    read = {name for name in _agent_facing_servers() if _admission_gated_tools(SERVERS / name)}
    assert shipping, "no agent-facing server ships an admission gate; the reader is looking wrong"
    assert read == shipping, f"gate shipped but not read: {sorted(shipping - read)}"


@pytest.mark.parametrize("name", _agent_facing_servers())
def test_a_server_queues_exactly_what_it_gates(name: str) -> None:
    """The manifest's `queued:` set is the server's admission-gated set, in both directions.

    A gated tool left off the list is refused by a full pod straight into a chemist's turn; a tool
    on it that is not gated pays ~80 ms of broker round trip for a slot nothing would ever deny.
    Both are a drift between two declarations of one fact — what is heavy on this server — so the
    manifest is held to the code that decides it.
    """
    manifest = yaml.safe_load((ROOT / "manifests" / name / "connector.yaml").read_text())
    queued = set(((manifest.get("endpoint") or {}).get("queued") or {}).get("tools") or [])
    gated = _admission_gated_tools(SERVERS / name)
    assert queued == gated, (
        f"{name}: missing from queued: {sorted(gated - queued)}, "
        f"queued but not gated: {sorted(queued - gated)}"
    )
