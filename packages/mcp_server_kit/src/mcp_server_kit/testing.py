"""Test helpers — chiefly the checks every server in this repository must pass, written once.

`served_tools` opens a real MCP session against the app (in-process, over ASGI, no socket) and
returns what the server actually advertises. That is the only honest input to the manifest mirror
test: a `connector.yaml` is a *claim* about the tool surface, and Chemclaw3's own history is a
list of claims that outlived the code they described.

`assert_manifest_matches` is the check itself, kept here rather than copied into each server's
tests so it cannot drift into seven slightly different assertions.

`assert_bearer_is_enforced` is the other one, and it is here for the same anti-drift reason plus a
sharper one: `connector_app` is shared, so a single proof of the bearer check *looks* sufficient —
and that is the inference a mount bypass defeats, since a mounted MCP surface is precisely the route
an enclosing app's declared credential does not reach. Nothing but a request against a running
server can tell the two apart, so the helper takes a base URL rather than an app.

**It checked names and never arguments, and the claim that rests on the arguments is load-bearing.**
`MODULES.md` makes `chem` and `safety` drop-in *replacements* for Chemclaw3's in-tree bundles on
the grounds of "same manifest `name`, same tools, same arguments" — and nothing in either
repository read an `inputSchema`. A renamed argument keeps every name-level check green: the tool
is declared, it is served, it is classified. It reaches the model as a tool that advertises,
validates, and rejects every call written against the old name.

So a server may hand this function the served `Tool` objects instead of their names, and the
argument surface is then checked against a `tool-surface.json` recorded beside the manifest. **A
golden file rather than a manifest key, and that was checked rather than assumed**: Chemclaw3's
`HttpEndpoint` is `ConfigDict(extra="forbid")`, so an `arguments:` key under `endpoint:` would abort
the other repository's startup on the very manifest it was meant to enrich. A file beside the
manifest costs it nothing and is read in the same diff as the change that moves it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

# `[testing]`-extra only: this module drives a *running* server, and a serving image never
# imports it. `TID253` is the belt over `no_egress.network_imports`, whose per-server scan does
# not read this package at all.
import httpx  # noqa: TID253
import yaml
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Tool
from pydantic import BaseModel, ConfigDict, Field, ValidationError

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

    What it is for: a server's admission ceiling and batch bound are read from the environment
    **at import**, so the only honest way to show that the variable is what built them is to
    build them again with the variable set to something else and read the *module's* value back.

    Every server that tried to assert that instead re-typed the expression under test into its own
    test — `int(os.environ.get("CHEMCLAW_RXNLABEL_MAX_BATCH", "500"))` compared to `(7, 9)`, which
    is how that bound was spelled before `env_bound` and is quoted here as the *defect*, not as a
    line anybody can still grep for — which asserts that `os.environ.get` works. Measured:
    replacing the module's read with a hardcoded constant left 209 tests green in one server and
    203 in another, and `tests/test_fleet.py`'s
    inventory of numeric bounds could not catch it either, because that inventory is *derived from
    the source* and a removed read simply shrinks it.

    `importlib.reload` would do the reading and is the wrong tool: it rebinds the entry in
    `sys.modules`, so every other module that did `from ...tools import server` at import keeps a
    reference to the old object while new callers get a different one. This executes the same
    source into a throwaway module instead, so nothing outside the assertion can see it.

    **It is registered in `sys.modules` for the duration of the execution and removed afterwards**,
    which reads like a contradiction of the paragraph above and is not: the name is the throwaway
    one, so no existing importer can reach it, and it is gone before this returns. It has to be
    there because a module is not self-contained while it executes — `dataclasses` resolves a
    string annotation by looking its own class's module up in `sys.modules`, and with
    `from __future__ import annotations` in force every annotation is a string. Driven on
    `servers/props`' `correlations.py`, whose `VapourPressure` is a `slots=True` dataclass: without
    the registration the re-execution dies with `AttributeError: 'NoneType' object has no attribute
    '__dict__'` from inside `dataclasses`, which names neither this function nor the module it was
    given. `finally`, so a module that raises on purpose — which is what every bound test asks for
    — does not leave the name behind for the next test to find.
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


#: The cap `Chemclaw3` puts on every manifest text field
#: (`core.manifest_io.MAX_MANIFEST_TEXT_CHARS`). A literal here because this repository may not
#: import that one, and held against the real value by `tests/test_consumer_agreement.py` whenever
#: a consumer checkout is on the machine.
MAX_MANIFEST_TEXT_CHARS = 4_000

#: The shape `Chemclaw3`'s `ConnectorManifest.name` requires. A bundle's name is a directory name,
#: a `CHEMCLAW_CONNECTOR_URLS` key and a metric label over there, which is why it is constrained at
#: all.
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
    #: The consumer's switch for a bundle that is declared but not bound unless a deployment names
    #: it (`D-2026-09-20-declaring-a-capability-and-binding-it-are-different-decisions` there). A
    #: fleet manifest that shadows a consumer copy declaring `false` has to be able to say `false`
    #: too, or the shadow silently binds every tool schema it carries on every model call.
    default_enabled: bool = True


def load_manifest(path: Path) -> ConnectorManifest:
    """Parse and validate a `connector.yaml`.

    Raises `ValueError` rather than returning `{}` for an empty, non-mapping or invalid file —
    `ValidationError` is a `ValueError`, so the contract the callers were written against is
    unchanged and the diagnosis is better.
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
        base_url: The server's MCP endpoint, e.g. `http://127.0.0.1:8850/mcp`. Loopback, so the
            egress guard permits it.
        token: The bearer token, when the server declares one. Passed on a caller-supplied httpx
            client because that is the only way this version of the MCP client takes headers.

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

    Deliberately lossy and deliberately *not* the schema itself. What has to fail loudly is a
    renamed, retyped, or newly required argument; what must not fail is a reworded description or a
    reordered `$defs` block, because a golden that churns on prose is a golden people regenerate
    without reading. A `$ref` to a nested model is `object` for the same reason — its own fields are
    that model's contract, not this tool's argument list.
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
        tools: The `Tool` objects a real `tools/list` returned. The schemas have to come from the
            transport rather than from the Python function, because what Chemclaw3 binds is the
            served `inputSchema` and nothing else.

    Returns:
        `{tool: {argument: {"type": ..., "required": ..., "default": ...}}}`, with `default` present
        only where the schema declares one. JSON-serialisable and stable under re-serving.
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

    Recording is an explicit act (`MCP_UPDATE_TOOL_SURFACE=1`) precisely because the file is the
    contract: a mechanism that regenerated itself on mismatch would report every rename as clean.
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

    Args:
        manifest_path: The server's `connector.yaml`.
        tools: The served tool *names*, or — preferred — the `Tool` objects a real `tools/list`
            returned. Names alone check everything below except the arguments, which no schema
            reaches; passing the objects is a one-word change at the call site and is what makes an
            accidental rename fail in CI.
        surface_path: Where the recorded argument surface lives. Defaults to `tool-surface.json`
            beside the manifest, which is where it belongs — the manifest declares the tools and
            this declares their arguments.

    Checks four things, because each has its own failure:

    1. Every served tool is declared. An undeclared tool is reachable by anything that can open a
       socket to the pod while looking, in review, like it does not exist.
    2. Every declared tool is served. A manifest naming a tool nobody serves makes Chemclaw3
       advertise a capability that fails at call time.
    3. Every tool is classified exactly once as `read_only` or `state_changing` — the same rule
       Chemclaw3's `HttpEndpoint` enforces (D-167). Getting it wrong by omission fails *open*:
       the plan gate would let an unapproved plan call a state-changing tool.
    4. Every tool's argument names, types and requiredness are the recorded ones (when `Tool`
       objects are passed). Names alone cannot see this, and it is the check `MODULES.md`'s
       drop-in-replacement claim actually rests on.
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


def _tools_list(mcp_url: str, headers: dict[str, str]) -> httpx.Response:
    """One `tools/list` POST at `/mcp`, sent exactly as an MCP client's first call would be.

    A raw POST rather than an MCP session because what is under test is the ASGI stack *in front*
    of the transport: a refusal happens before a session can exist, so a client that insists on a
    handshake cannot observe one.
    """
    return httpx.post(
        mcp_url,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"accept": "application/json, text/event-stream", **headers},
        timeout=15.0,
    )


async def assert_bearer_is_enforced(base_url: str, manifest_path: Path, *, token: str) -> None:
    """Drive a **running** server's `/mcp` through every arm of the bearer rule, and its probes.

    `CLAUDE.md` states the rule in the imperative — *"Bearer auth enforced on `/mcp` itself … verify
    against a running server; do not read it off the source"* — because the failure it guards
    against is invisible in the source. An MCP surface is *mounted*, and a mount bypasses the
    enclosing app's dependencies: the credential can be declared, reviewed, and applied to
    everything except the one route that matters. `connector_app` is shared, so one proof arguably
    covers all seven servers — but "the helper is shared, so it must be applied" is exactly the
    inference a mount bypass defeats, and every one of that helper's documented traps is a case
    where its behaviour was not what its source suggested.

    Args:
        base_url: A running server's base URL on loopback, e.g. `http://127.0.0.1:8850`.
        manifest_path: That server's `connector.yaml`. The declared `token_env` is read from it
            rather than passed in, so this also checks the *serving* side enforces the variable
            Chemclaw3 was told to send — two names that agree today and are not held together by
            anything else.
        token: The value the fixture put in that variable. Asserted to *be* what the variable
            holds: a check that offers a token the server was never given proves nothing by
            refusing it.

    The arms, each of which fails on its own:

    1. No `authorization` header at all — the anonymous handshake Chemclaw3 once completed.
    2. A wrong token — so the 401 above is the credential being checked rather than the header
       being required.
    3. The right secret under the wrong scheme, which a `startswith`-shaped check would serve.
    4. The right token — or a refusal proves nothing, since a server that refuses everything is
       indistinguishable from one that enforces a credential.
    5. The declared variable **unset**, with the right token still offered: fail closed. Chemclaw3
       mounted a secret, recorded the control as enabled, and served every tool to anything that
       could reach the pod, because the serving side never checked. The variable is read per
       request, so this arm needs no second server — and it is restored and re-served afterwards,
       which is what makes the 401 attributable to the unset variable rather than to a wedged
       process.
    6. The variable holding **only whitespace**, which is the shape an empty secret template
       produces. Fail closed, for the same reason as 5 — and it is a separate arm because the
       normalisation below is what could have turned it into an empty secret rather than an
       absent one.
    7. **Whitespace around the credential, on both sides.** `auth.py` strips it from the offered
       header *and* from the provisioned variable, so this asserts the tolerance in the direction
       it exists for (a secret written by `echo`, carrying a trailing newline, still authenticates
       an unpadded caller) and in the direction a caller controls (a padded header against an
       unpadded secret). It was asserted in neither direction while the offered side alone was
       stripped, which is how the asymmetry survived. The refusal arms above keep this from
       widening into "anything close enough": a token differing by one non-whitespace byte is
       already arm 2.

    `/healthz` is driven through the same arm: a kubelet probe carries no identity, so the failure
    mode where a token problem takes the pod out of the cluster as well as off the network is one
    this has to exclude.
    """
    # The model makes `mode: bearer` and a non-empty `token_env` unrepresentable otherwise, so what
    # is left to assert here is the half a model cannot see: that the variable the manifest names
    # is the one this fixture actually provisioned.
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

    # Whitespace, both ways round. The padded *secret* is the arm that matters operationally: a
    # Kubernetes Secret written with `echo` carries a trailing newline, and before this the server
    # refused every request including one offering exactly those bytes.
    # The padded header is padded on the *inside* (`Bearer  <tok>`) rather than trailing: h11
    # refuses to send a field value with leading or trailing whitespace, so the trailing-tab
    # variant a raw socket found is not expressible through an HTTP client at all. This arm still
    # reaches the same `offered.strip()`.
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
        # ASYNC210: a blocking call in an async function. The server this drives is a uvicorn in
        # its own thread with its own loop (see each server's `test_server.py`), so blocking this
        # loop cannot stall the thing being probed - and `_tools_list` beside it is sync for the
        # same reason. An in-process ASGI transport would make this a deadlock rather than a smell.
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
