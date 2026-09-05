"""Test helpers — chiefly the two checks every server in this repository must pass.

`served_tools` opens a real MCP session against the app (in-process, over ASGI, no socket) and
returns what the server actually advertises. That is the only honest input to the manifest mirror
test: a `connector.yaml` is a *claim* about the tool surface, and Chemclaw3's own history is a
list of claims that outlived the code they described.

`assert_manifest_matches` is the check itself, kept here rather than copied into each server's
tests so it cannot drift into seven slightly different assertions.

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

import json
import os
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import httpx
import yaml
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Tool

__all__ = [
    "SURFACE_FILENAME",
    "SURFACE_UPDATE_ENV",
    "assert_manifest_matches",
    "load_manifest",
    "served_tools",
    "tool_surface",
]

# The recorded argument surface, beside the `connector.yaml` it belongs to — the two halves of one
# contract, reviewed in one diff. Not under `tests/`: what a server advertises is not a test detail.
SURFACE_FILENAME = "tool-surface.json"

# Set this to rewrite the golden. Never set in CI, so a mismatch there is a failure rather than a
# silent re-record — the property that makes an accidental rename loud.
SURFACE_UPDATE_ENV = "MCP_UPDATE_TOOL_SURFACE"


def load_manifest(path: Path) -> dict[str, Any]:
    """Parse a `connector.yaml`. Raises rather than returning `{}` for an empty or invalid file."""
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError(f"{path} is not a mapping")
    return parsed


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
    manifest = load_manifest(manifest_path)
    endpoint = manifest.get("endpoint", {})
    # `or []` rather than a default: a manifest with a bare `tools:` key parses to `None`, and
    # `sorted(None)` is a TypeError naming this line instead of the assertion below naming the
    # manifest — which is the whole reason this helper exists.
    declared = sorted(endpoint.get("tools") or [])
    served = sorted(tool if isinstance(tool, str) else tool.name for tool in tools)
    assert served == declared, (
        f"{manifest_path} declares {declared} but the server serves {served}; "
        "the manifest is the contract Chemclaw3 reads, so these must be equal"
    )
    read_only = set(endpoint.get("read_only") or [])
    state_changing = set(endpoint.get("state_changing") or [])
    unclassified = set(declared) - read_only - state_changing
    both = read_only & state_changing
    assert not unclassified, f"{manifest_path}: unclassified tool(s) {sorted(unclassified)}"
    assert not both, f"{manifest_path}: tool(s) classified twice {sorted(both)}"
    schemas = [tool for tool in tools if isinstance(tool, Tool)]
    if schemas:
        _assert_surface_unchanged(surface_path or manifest_path.parent / SURFACE_FILENAME, schemas)
