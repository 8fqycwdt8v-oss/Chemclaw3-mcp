"""Test helpers — the checks every server in this repository must pass, written once.

- `served_tools` opens a real MCP session and returns what the server advertises — the only honest
  input to the manifest check.
- `assert_manifest_matches` holds `connector.yaml` and the served surface together, including each
  tool's arguments against a recorded `tool-surface.json` beside the manifest (a separate file
  because Chemclaw3's manifest model forbids extra keys).
- `assert_wire_contract` holds a backend's served input and output schemas to the typed models in
  `chemclaw_contracts`, so a tool that drifts from the wire its consumer codes against fails here.
- `assert_bearer_is_enforced` drives a *running* server, because a mounted MCP surface can bypass a
  credential the enclosing app declares, and only a real request shows it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any, Literal, Self

# `[testing]`-extra only: drives a running server and is never imported by a serving image.
import httpx  # noqa: TID253
import yaml
from chemclaw_contracts import CONTRACT_VERSION_PATTERN
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Tool
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

__all__ = [
    "CONNECTOR_NAME_PATTERN",
    "MAX_MANIFEST_TEXT_CHARS",
    "SURFACE_FILENAME",
    "SURFACE_UPDATE_ENV",
    "BearerAuth",
    "ConnectorManifest",
    "HttpEndpoint",
    "assert_bearer_is_enforced",
    "assert_manifest_matches",
    "assert_wire_contract",
    "load_manifest",
    "reimported",
    "served_tools",
    "tool_surface",
]

# The recorded argument surface, beside the `connector.yaml` it belongs to — the two halves of one
# contract, reviewed in one diff. Not under `tests/`: what a server advertises is not a test detail.
SURFACE_FILENAME = "tool-surface.json"

# Set this to rewrite the golden. Never set in CI, so a mismatch there is a failure rather than a
# silent re-record — the property that makes an accidental rename loud.
SURFACE_UPDATE_ENV = "MCP_UPDATE_TOOL_SURFACE"


def reimported(module: ModuleType) -> ModuleType:
    """Execute `module`'s source again, under the environment in force now, as a separate object.

    For asserting that an environment variable read at import is what built a bound: rebuild with a
    different value and read the module's own result, rather than re-typing the expression in the
    test. Not `importlib.reload`, which rebinds `sys.modules` under existing importers. The
    throwaway name is registered in `sys.modules` only during execution (dataclasses resolve string
    annotations through it) and removed in a `finally`.
    """
    if module.__spec__ is None or module.__spec__.origin is None:  # pragma: no cover - not a file
        raise ValueError(f"{module.__name__} has no source file to re-execute")
    spec = importlib.util.spec_from_file_location(
        f"{module.__name__}__reimported_for_a_test", module.__spec__.origin
    )
    if spec is None or spec.loader is None:  # pragma: no cover - unreadable source
        raise ValueError(
            f"{module.__name__} could not be re-imported from {module.__spec__.origin}"
        )
    fresh = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = fresh
    try:
        spec.loader.exec_module(fresh)
    finally:
        del sys.modules[spec.name]
    return fresh


class BearerAuth(BaseModel):
    """The only auth mode this fleet declares, and the variable both sides read.

    `mode: none` is expressible in Chemclaw3's model and is not expressible here, deliberately:
    `CLAUDE.md` requires bearer on every manifest *including the loopback dev URL*, because a
    manifest whose auth mode changes with its address is one whose serving side gets it wrong. A
    model that accepted `none` would make the rule a review convention again.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["bearer"]
    token_env: str = Field(min_length=1)


class QueuedDispatch(BaseModel):
    """`endpoint.queued:` — the tools Chemclaw3 calls through a queue rather than directly.

    Chemclaw3's `chemclaw.connectors.manifest.QueuedDispatch`, mirrored for the reason this module
    mirrors anything: that model is `extra="forbid"`, so a key spelled differently here would abort
    the consumer's startup. Which tools belong in it is this fleet's to say — a server gates its
    heavy calls behind an admission ceiling, and `tests/test_fleet.py` holds the queued set to the
    gated set — because a full pod is then a wait in the queue instead of a refusal.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tools: list[str] = Field(min_length=1)
    inline_wait_seconds: float = Field(gt=0)


class HttpEndpoint(BaseModel):
    """The `endpoint:` block, modelled the way the repository that reads it models it.

    Chemclaw3's `chemclaw.connectors.manifest.HttpEndpoint` is `extra="forbid"`, so a key this
    fleet invents aborts *that* repository's startup with a `ConnectorError` naming the file. This
    model exists so the refusal happens here instead, in the suite of the repository that **owns**
    the manifests — the general form of that argument is
    `D-2026-09-14-the-gate-that-catches-a-change-is-the-gate-of-the-tree-it-is-made-in`.

    **Three shapes the consumer refuses are refused here too**, and this model used to accept all
    of them while calling itself a stand-in: an endpoint with no `transport:` (a
    `union_tag_not_found` over there, because its endpoint is a discriminated union), a bare
    `tools:`/`read_only:`/`state_changing:` key (YAML's `None`, a `list_type` error over there),
    and `tools: []` (refused over there by the classification validator — an endpoint serving
    nothing is not an endpoint). This model once coerced the first two, on the argument that a bare
    key "means an empty list to whoever wrote it". What it meant to the consumer was a
    `ConnectorError` at startup, and a stand-in that is kinder than the model it stands in for
    turns this suite green on a manifest that cannot be loaded.

    **`knowledge_read` is here because it is a field over there**, and this model refused it
    (`D-2026-09-16-a-stand-in-that-refuses-a-real-field-is-not-a-stand-in`). A fleet manifest
    declaring one would have failed `load_manifest` with "is not a connector manifest", which is
    a false sentence about a key the consumer defines and reads. Nothing here declares one yet,
    which is exactly why it went unnoticed — a latent false refusal costs nothing until the day
    somebody writes the field and is told their manifest is not a manifest.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    transport: Literal["http"]
    url: str = Field(min_length=1)
    health_url: str | None = None
    request_timeout: int | None = Field(default=None, gt=0)
    auth: BearerAuth
    tools: list[str] = Field(min_length=1)
    read_only: list[str] = Field(default_factory=list)
    state_changing: list[str] = Field(default_factory=list)
    knowledge_read: list[str] = Field(default_factory=list)
    queued: QueuedDispatch | None = None

    @model_validator(mode="after")
    def _queues_only_tools_it_serves(self) -> Self:
        """Refuse a queued name the endpoint does not serve, as the consumer does."""
        if self.queued is not None:
            if len(set(self.queued.tools)) != len(self.queued.tools):
                raise ValueError("`queued.tools` lists a tool more than once")
            unserved = sorted(set(self.queued.tools) - set(self.tools))
            if unserved:
                raise ValueError(
                    f"`queued.tools` names tool(s) {unserved} the endpoint does not serve"
                )
        return self


#: Chemclaw3's cap on every manifest text field (`core.manifest_io.MAX_MANIFEST_TEXT_CHARS`),
#: checked against it by `tests/test_consumer_agreement.py` when a checkout is available.
MAX_MANIFEST_TEXT_CHARS = 4_000

#: The shape Chemclaw3's `ConnectorManifest.name` requires.
CONNECTOR_NAME_PATTERN = r"^[a-z][a-z0-9-]*$"


class ConnectorManifest(BaseModel):
    """A whole `connector.yaml`: what it is, what it says it serves, and where it may be registered.

    `mount` is the key Chemclaw3 refuses (`extra="forbid"` over there), which is what makes
    `manifests-internal/` mechanical rather than trusted — see `tests/test_fleet.py`. It is
    modelled rather than ignored so that a typo in it is a refusal here instead of a backend
    silently declaring itself a connector.

    **The contract this model is held to, stated because it was wrong in both directions at once**
    (`D-2026-09-16-a-stand-in-that-refuses-a-real-field-is-not-a-stand-in`). It must refuse
    everything the consumer refuses and accept everything the consumer accepts, and every
    deliberate difference is named here:

    - `mount` — accepted here, refused there. That asymmetry *is* `manifests-internal/`.
    - `auth.mode` — `bearer` only, where the consumer also allows `none`. `CLAUDE.md`'s rule, and
      `BearerAuth` carries the argument.
    - `endpoint` — required here, optional there. Over there a bundle may contribute Temporal jobs
      and no endpoint; a server in this fleet that serves no MCP surface is not a server, and
      `assert_manifest_matches` has nothing to drive without a URL.
    - **The `read_only`/`state_changing` partition is not enforced by this model**, and the
      consumer's `HttpEndpoint` does enforce it. That one is checked here by
      `assert_manifest_matches` instead, against the tools a server **actually serves** rather than
      against the ones it declares — which is strictly the stronger question, and is the reason
      this model is allowed to be the weaker half of a pair rather than a hole. Every server in
      this fleet owes that call (`tests/test_fleet.py`), so nothing reaches a deployment unchecked.

    Everything else agreed by inspection and did not agree in fact. Measured at `6c6a0eb`:
    `{"name": "Calc_Server!", "description": "x" * 20_000}` validated here and aborts the
    consumer's startup — it enforces a pattern on `name` and a 4,000-character cap on
    `description` — while `endpoint.knowledge_read` and the top-level `jobs`, `skills`, `profiles`,
    `note_types` and `relations`, all real fields of the consumer's model, were refused here with
    "is not a connector manifest".

    The five top-level lists are accepted and **not** validated in depth: `JobSpec` alone carries
    an effect model, a queue, a compensation and three cross-field validators, and a second copy of
    that would be a second answer to one question — the defect this whole model exists to avoid one
    layer down. What holds them is `tests/test_consumer_agreement.py`, which runs every shipped
    manifest through the consumer's *own* model whenever a checkout is on the machine.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, pattern=CONNECTOR_NAME_PATTERN)
    description: str = Field(min_length=1, max_length=MAX_MANIFEST_TEXT_CHARS)
    mount: Literal["connector", "backend"] = "connector"
    endpoint: HttpEndpoint
    # Declared so they are accepted, typed loosely on purpose — see the class docstring.
    jobs: list[dict[str, Any]] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    profiles: list[str] = Field(default_factory=list)
    note_types: list[str] = Field(default_factory=list)
    relations: list[str] = Field(default_factory=list)
    #: The consumer's declared-but-not-bound switch; a fleet manifest shadowing a consumer copy
    #: must be able to say `false` too, or it binds every tool schema on every model call.
    default_enabled: bool = True
    #: The version of the surface this manifest declares (`docs/adding-a-server.md` has the bump
    #: rules). Optional here because it is optional over there; `/healthz` reports the same string.
    contract_version: str | None = Field(default=None, pattern=CONTRACT_VERSION_PATTERN)


def load_manifest(path: Path) -> ConnectorManifest:
    """Parse and validate a `connector.yaml`.

    Raises `ValueError` (a `ValidationError` is one) for an empty, non-mapping or invalid file.
    """
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError(f"{path} is not a mapping")
    try:
        return ConnectorManifest.model_validate(parsed)
    except ValidationError as error:
        raise ValueError(f"{path} is not a connector manifest: {error}") from error


async def served_tools(base_url: str, *, token: str | None = None) -> list[str]:
    """The tool names a running server advertises, via a real MCP handshake.

    Args:
        base_url: The server's MCP endpoint on loopback, e.g. `http://127.0.0.1:8850/mcp`.
        token: The bearer token, when the server declares one.

    Returns:
        The advertised tool names, sorted.
    """
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with (
        httpx.AsyncClient(headers=headers) as http_client,
        streamable_http_client(base_url, http_client=http_client) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        listed = await session.list_tools()
        return sorted(tool.name for tool in listed.tools)


def _argument_type(schema: dict[str, Any]) -> str:
    """One argument's type, as a short stable string a reader can compare across a diff.

    Deliberately lossy: renames, retypes and new required arguments must fail; reworded descriptions
    or reordered `$defs` must not. A `$ref` to a nested model is `object`.
    """
    declared = schema.get("type")
    if isinstance(declared, str):
        return declared
    members = schema.get("anyOf")
    if isinstance(members, list):
        # Declaration order, not sorted: `str | None` reads as `string|null` the way it was written.
        return "|".join(_argument_type(member) for member in members if isinstance(member, dict))
    if "enum" in schema:
        return "enum"
    if "$ref" in schema:
        return "object"
    return "any"


def tool_surface(tools: Iterable[Tool]) -> dict[str, dict[str, dict[str, Any]]]:
    """The argument surface a server advertises: per tool, per argument, type and requiredness.

    Args:
        tools: The `Tool` objects a real `tools/list` returned — the served `inputSchema` is what
            Chemclaw3 binds.

    Returns:
        `{tool: {argument: {"type": ..., "required": ..., "default": ...}}}`, `default` only where
        declared. JSON-serialisable and stable.
    """
    surface: dict[str, dict[str, dict[str, Any]]] = {}
    for tool in tools:
        schema = tool.inputSchema or {}
        properties = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        arguments: dict[str, dict[str, Any]] = {}
        for name in sorted(properties):
            declared = properties[name] if isinstance(properties[name], dict) else {}
            argument: dict[str, Any] = {
                "type": _argument_type(declared),
                "required": name in required,
            }
            if "default" in declared:
                argument["default"] = declared["default"]
            arguments[name] = argument
        surface[tool.name] = arguments
    return surface


def _assert_surface_unchanged(surface_path: Path, tools: Sequence[Tool]) -> None:
    """Assert the served argument surface is the recorded one, or record it when asked to.

    Recording is explicit (`MCP_UPDATE_TOOL_SURFACE=1`), so a mismatch is never silently accepted.
    """
    served = tool_surface(tools)
    if os.environ.get(SURFACE_UPDATE_ENV):
        surface_path.write_text(json.dumps(served, indent=2, sort_keys=True) + "\n", "utf-8")
        return
    assert surface_path.exists(), (
        f"{surface_path} does not exist, so nothing checks this server's argument names. "
        f"Record it with {SURFACE_UPDATE_ENV}=1 and review the file in the pull request."
    )
    recorded = json.loads(surface_path.read_text(encoding="utf-8"))
    assert recorded == served, (
        f"{surface_path} records an argument surface the server no longer serves.\n"
        f"recorded: {json.dumps(recorded, indent=2, sort_keys=True)}\n"
        f"served:   {json.dumps(served, indent=2, sort_keys=True)}\n"
        "A renamed or retyped argument is a breaking change for every caller written against the "
        f"old one — Chemclaw3 binds this schema verbatim. If it is intended, re-record with "
        f"{SURFACE_UPDATE_ENV}=1."
    )


def assert_manifest_matches(
    manifest_path: Path,
    tools: Sequence[str] | Sequence[Tool],
    *,
    surface_path: Path | None = None,
) -> None:
    """Assert the manifest and the served surface agree, in both directions.

    Checks that every served tool is declared (else it is reachable but invisible in review), every
    declared tool is served, every tool is classified exactly once as `read_only` or
    `state_changing` (an omission fails open past the plan gate), and — when `Tool` objects are
    passed — every tool's argument names, types and requiredness match the recorded surface.

    Args:
        manifest_path: The server's `connector.yaml`.
        tools: The served tool names, or (preferred) the `Tool` objects, which enable the argument
            check.
        surface_path: The recorded argument surface; defaults to `tool-surface.json` beside the
            manifest.
    """
    endpoint = load_manifest(manifest_path).endpoint
    declared = sorted(endpoint.tools)
    served = sorted(tool if isinstance(tool, str) else tool.name for tool in tools)
    assert served == declared, (
        f"{manifest_path} declares {declared} but the server serves {served}; "
        "the manifest is the contract Chemclaw3 reads, so these must be equal"
    )
    read_only = set(endpoint.read_only)
    state_changing = set(endpoint.state_changing)
    unclassified = set(declared) - read_only - state_changing
    both = read_only & state_changing
    assert not unclassified, f"{manifest_path}: unclassified tool(s) {sorted(unclassified)}"
    assert not both, f"{manifest_path}: tool(s) classified twice {sorted(both)}"
    schemas = [tool for tool in tools if isinstance(tool, Tool)]
    if schemas:
        _assert_surface_unchanged(surface_path or manifest_path.parent / SURFACE_FILENAME, schemas)


def _schema_shape(schema: Any, defs: Mapping[str, Any], ignore: frozenset[str]) -> Any:
    """A JSON schema reduced to what a caller codes against: types, members, requiredness, defaults.

    `$ref`s are followed. Titles, descriptions and numeric or length bounds are dropped, because a
    reworded description or a tightened bound is not a change of wire shape. `ignore` names
    properties to leave out at any depth (an output-only field a request model carries).
    """
    if not isinstance(schema, dict):
        return "any"
    if "$ref" in schema:
        shape = _schema_shape(defs[str(schema["$ref"]).rsplit("/", 1)[-1]], defs, ignore)
    elif "anyOf" in schema:
        members = [_schema_shape(member, defs, ignore) for member in schema["anyOf"]]
        shape = {"anyOf": sorted(members, key=lambda member: json.dumps(member, sort_keys=True))}
    else:
        shape = {}
        if "type" in schema:
            shape["type"] = schema["type"]
        if "enum" in schema:
            shape["enum"] = sorted(schema["enum"], key=str)
        properties = schema.get("properties")
        if isinstance(properties, dict):
            shape["properties"] = {
                name: _schema_shape(member, defs, ignore)
                for name, member in sorted(properties.items())
                if name not in ignore
            }
            shape["required"] = sorted(
                name for name in schema.get("required", []) if name not in ignore
            )
        if "items" in schema:
            shape["items"] = _schema_shape(schema["items"], defs, ignore)
    if "default" in schema:
        shape = {**shape, "default": schema["default"]}
    return shape


def _shape_differences(contract: Any, served: Any, *, subset: bool, where: str) -> list[str]:
    """Where `contract` and `served` disagree; with `subset`, `served` may carry extra members.

    Subset mode is for answers: a field the server adds is additive, but one the contract names and
    the server dropped or retyped is not, and a contract may not require what the server does not.
    """
    if not (isinstance(contract, dict) and isinstance(served, dict)):
        return [] if contract == served else [f"{where}: contract {contract!r}, served {served!r}"]
    if "properties" not in contract or "properties" not in served:
        return [] if contract == served else [f"{where}: contract {contract!r}, served {served!r}"]
    found: list[str] = []
    names, offered = set(contract["properties"]), set(served["properties"])
    missing = names - offered
    extra = set() if subset else offered - names
    if missing:
        found.append(f"{where}: the server does not serve {sorted(missing)}")
    if extra:
        found.append(f"{where}: the server serves {sorted(extra)}, which the contract lacks")
    if subset:
        unserved = set(contract["required"]) - set(served["required"])
        if unserved:
            found.append(f"{where}: the contract requires {sorted(unserved)}, the server does not")
    elif contract["required"] != served["required"]:
        found.append(
            f"{where}: required {contract['required']} in the contract, {served['required']} served"
        )
    for name in sorted(names & offered):
        found += _shape_differences(
            contract["properties"][name],
            served["properties"][name],
            subset=subset,
            where=f"{where}.{name}",
        )
    return found


def assert_wire_contract(
    tools: Sequence[Tool],
    requests: Mapping[str, type[BaseModel]],
    responses: Mapping[str, type[BaseModel]],
    *,
    ignore: Iterable[str] = (),
) -> None:
    """Assert a server's served schemas agree with the contract models, in both directions.

    Every served tool has a request and a response model and every model names a served tool. A
    request model must describe the input schema exactly (names, types, requiredness, defaults,
    nested members). A response model must be a subset of the output schema: the server may add
    fields, but not drop or retype the ones the contract names.

    Args:
        tools: The `Tool` objects a `tools/list` returned.
        requests: Tool name to the model of its arguments.
        responses: Tool name to the model of its answer.
        ignore: Property names left out of the comparison at every depth, for a field that exists
            on one side only by design (an output-only identifier a request model also carries).
    """
    skipped = frozenset(ignore)
    served = {tool.name: tool for tool in tools}
    problems: list[str] = []
    for label, models in (("request", requests), ("response", responses)):
        if set(models) != set(served):
            problems.append(
                f"{label} models cover {sorted(models)} but the server serves {sorted(served)}"
            )
    for name in sorted(set(served) & set(requests) & set(responses)):
        tool = served[name]
        schema = tool.inputSchema or {}
        contract = requests[name].model_json_schema(mode="validation")
        problems += _shape_differences(
            _schema_shape(contract, contract.get("$defs", {}), skipped),
            _schema_shape(schema, schema.get("$defs", {}), skipped),
            subset=False,
            where=f"{name} input",
        )
        output = tool.outputSchema or {}
        answer = responses[name].model_json_schema(mode="serialization")
        problems += _shape_differences(
            _schema_shape(answer, answer.get("$defs", {}), skipped),
            _schema_shape(output, output.get("$defs", {}), skipped),
            subset=True,
            where=f"{name} output",
        )
    assert not problems, (
        "the served wire has drifted from `chemclaw_contracts`:\n  "
        + "\n  ".join(problems)
        + "\nChange the contract model and bump the manifest's `contract_version` (major for a "
        "removed, renamed or retyped argument or field; minor for an additive one) in the same "
        "commit."
    )


def _tools_list(mcp_url: str, headers: dict[str, str]) -> httpx.Response:
    """One `tools/list` POST at `/mcp`, sent exactly as an MCP client's first call would be.

    Raw, because a refusal happens before any session could exist.
    """
    return httpx.post(
        mcp_url,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"accept": "application/json, text/event-stream", **headers},
        timeout=15.0,
    )


async def assert_bearer_is_enforced(base_url: str, manifest_path: Path, *, token: str) -> None:
    """Drive a **running** server's `/mcp` through every arm of the bearer rule, and its probes.

    Verified against a running server because a mount can bypass the enclosing app's credential.
    Arms: no header, wrong token, right secret under the wrong scheme (all 401); the right token
    (served); the variable unset and whitespace-only (fail closed, then restored and re-served);
    surrounding whitespace on the offered and the provisioned side (served). `/healthz` must answer
    without a credential.

    Args:
        base_url: A running server's base URL on loopback, e.g. `http://127.0.0.1:8850`.
        manifest_path: That server's `connector.yaml`, from which the declared `token_env` is read,
            so the serving side is held to the variable Chemclaw3 was told to send.
        token: The value the fixture put in that variable; asserted to be what it holds.
    """
    # The model already guarantees bearer mode and a `token_env`; check it is the one provisioned.
    token_env = load_manifest(manifest_path).endpoint.auth.token_env
    assert os.environ.get(token_env) == token, (
        f"{manifest_path} declares {token_env}, which is the variable the running server reads on "
        "every request. This check is only evidence if the token it offers is the one that "
        "variable holds, and it is not — the fixture set a different variable, or none."
    )

    mcp_url = f"{base_url.rstrip('/')}/mcp"
    for description, headers in (
        ("no authorization header", {}),
        ("a wrong bearer token", {"authorization": f"Bearer {token}-wrong"}),
        ("the right secret under the wrong scheme", {"authorization": f"Basic {token}"}),
    ):
        response = _tools_list(mcp_url, headers)
        assert response.status_code == 401, (
            f"{mcp_url} answered {response.status_code} to a tools/list with {description}. The "
            "MCP surface is mounted, and a mount bypasses the enclosing app's dependencies — a "
            "credential that is declared but not enforced here serves every tool to anything that "
            "can open a socket to the pod."
        )

    served = await served_tools(mcp_url, token=token)
    assert served, f"{mcp_url} served no tools to the declared credential"

    # Whitespace both ways round; the padded secret is the operational case (an `echo`-written
    # Secret). The header is padded inside (`Bearer  <tok>`) because h11 refuses trailing
    # whitespace.
    for description, provisioned, offered in (
        ("a padded header against the provisioned secret", token, f" {token}"),
        ("an unpadded header against a newline-provisioned secret", f"{token}\n", token),
    ):
        os.environ[token_env] = provisioned
        try:
            response = _tools_list(mcp_url, {"authorization": f"Bearer {offered}"})
            assert response.status_code != 401, (
                f"{mcp_url} refused {description}. Surrounding whitespace is normalised on both "
                "sides deliberately — see `auth.BearerAuthMiddleware` — and a secret provisioned "
                "with a trailing newline otherwise takes the whole server off the network."
            )
        finally:
            os.environ[token_env] = token

    os.environ[token_env] = "   \n\t "
    try:
        response = _tools_list(mcp_url, {"authorization": f"Bearer {token}"})
        assert response.status_code == 401, (
            f"{mcp_url} answered {response.status_code} with {token_env} holding nothing but "
            "whitespace. That is an unset credential, and the normalisation this file asserts "
            "above must not turn it into an empty secret that an empty offer matches."
        )
    finally:
        os.environ[token_env] = token

    del os.environ[token_env]
    try:
        response = _tools_list(mcp_url, {"authorization": f"Bearer {token}"})
        assert response.status_code == 401, (
            f"{mcp_url} answered {response.status_code} with {token_env} unset. A declared "
            "credential whose variable is missing must refuse every request: a misconfigured "
            "deployment has to serve nothing, never everything."
        )
        # ASYNC210: the server runs in its own thread and loop, so blocking here cannot stall it; an
        # in-process ASGI transport would deadlock instead.
        probe = httpx.get(f"{base_url.rstrip('/')}/healthz", timeout=15.0)  # noqa: ASYNC210
        assert probe.status_code != 401, (
            f"/healthz answered 401 with {token_env} unset. A kubelet probe and a Prometheus "
            "scrape carry no identity, so a credential problem must not also take the pod out of "
            "the cluster."
        )
    finally:
        os.environ[token_env] = token

    assert await served_tools(mcp_url, token=token) == served, (
        "the server did not serve the same surface once the credential was restored, so the 401 "
        "above is not attributable to the unset variable"
    )
