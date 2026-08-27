"""Test helpers — chiefly the two checks every server in this repository must pass.

`served_tools` opens a real MCP session against the app (in-process, over ASGI, no socket) and
returns what the server actually advertises. That is the only honest input to the manifest mirror
test: a `connector.yaml` is a *claim* about the tool surface, and Chemclaw3's own history is a
list of claims that outlived the code they described.

`assert_manifest_matches` is the check itself, kept here rather than copied into each server's
tests so it cannot drift into seven slightly different assertions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import yaml
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

__all__ = ["assert_manifest_matches", "load_manifest", "served_tools"]


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


def assert_manifest_matches(manifest_path: Path, tool_names: list[str]) -> None:
    """Assert the manifest and the served surface agree, in both directions.

    Checks three things, because each has its own failure:

    1. Every served tool is declared. An undeclared tool is reachable by anything that can open a
       socket to the pod while looking, in review, like it does not exist.
    2. Every declared tool is served. A manifest naming a tool nobody serves makes Chemclaw3
       advertise a capability that fails at call time.
    3. Every tool is classified exactly once as `read_only` or `state_changing` — the same rule
       Chemclaw3's `HttpEndpoint` enforces (D-167). Getting it wrong by omission fails *open*:
       the plan gate would let an unapproved plan call a state-changing tool.
    """
    manifest = load_manifest(manifest_path)
    endpoint = manifest.get("endpoint", {})
    # `or []` rather than a default: a manifest with a bare `tools:` key parses to `None`, and
    # `sorted(None)` is a TypeError naming this line instead of the assertion below naming the
    # manifest — which is the whole reason this helper exists.
    declared = sorted(endpoint.get("tools") or [])
    served = sorted(tool_names)
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
