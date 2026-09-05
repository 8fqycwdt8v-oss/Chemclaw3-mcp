"""The argument surface is part of the contract, and until now nothing anywhere checked it.

`assert_manifest_matches` checked tool *names* in both directions and the exactly-once
`read_only`/`state_changing` classification, and never read an `inputSchema`. Across both
repositories the only assertion about a served schema compared `calc`'s to `calc`'s own table. Yet
`MODULES.md` rests a whole migration on the stronger claim — `chem` and `safety` are drop-in
**replacements** for Chemclaw3's in-tree bundles because they have the "same manifest `name`, same
tools, **same arguments**" — and the calling side takes the served `inputSchema` verbatim, so a
renamed argument reaches the model as a silently different tool. It advertises, it validates, and
every call written against the old name fails at call time.

**A golden file rather than a manifest key**, and the reason is in the other repository:
Chemclaw3's `HttpEndpoint` is `ConfigDict(extra="forbid")`, so an `arguments:` key added to
`endpoint:` here would abort that repository's startup on the manifest it was meant to enrich —
checked in `chemclaw/connectors/manifest.py`, not assumed. A file beside `connector.yaml` costs
Chemclaw3 nothing, and it is reviewed in the same diff as the change that moves it.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import FastMCP
from mcp.types import Tool
from mcp_server_kit.app import connector_app
from mcp_server_kit.testing import SURFACE_UPDATE_ENV, assert_manifest_matches, tool_surface

MANIFEST = """\
name: surface-probe
endpoint:
  transport: http
  url: http://127.0.0.1:8850/mcp
  tools:
    - render
  read_only:
    - render
"""


def _probe_server() -> FastMCP:
    """One tool whose three arguments cover the shapes a schema takes: required, default, union."""
    server = FastMCP("surface-probe")

    @server.tool()
    def render(smiles: str, width: int = 300, label: str | None = None) -> str:
        """Draw a structure."""
        return smiles

    return server


def _renamed_probe_server() -> FastMCP:
    """The same tool with one argument renamed — the failure this whole file exists to catch."""
    server = FastMCP("surface-probe")

    @server.tool()
    def render(structure: str, width: int = 300, label: str | None = None) -> str:
        """Draw a structure."""
        return structure

    return server


def _free_port() -> int:
    """An ephemeral loopback port, released immediately for uvicorn to claim."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextmanager
def _serving(server: FastMCP, *, name: str) -> Iterator[str]:
    """Run one capability under uvicorn on loopback for the duration of one test."""
    app = connector_app(server, name=name)
    port = _free_port()
    uvicorn_server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=uvicorn_server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base}/healthz", timeout=1.0).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:  # pragma: no cover - only reached if the app never becomes ready
        pytest.fail("the probe server did not become ready within 30 s")
    try:
        yield base
    finally:
        uvicorn_server.should_exit = True
        thread.join(timeout=10)


@asynccontextmanager
async def _session(base: str) -> AsyncIterator[ClientSession]:
    """An initialised MCP session against the running server."""
    async with (
        httpx.AsyncClient() as http_client,
        streamable_http_client(f"{base}/mcp", http_client=http_client) as (rx, tx, _),
        ClientSession(rx, tx) as session,
    ):
        await session.initialize()
        yield session


async def _served(server: FastMCP, *, name: str) -> list[Tool]:
    """What a caller is actually advertised, over a real socket and a real handshake.

    The schemas are the subject here, so they are read from the transport rather than from the
    `FastMCP` object: this repository's rule for the manifest is "verify against a running server;
    do not read it off the source", and an argument surface is the same kind of claim.
    """
    with _serving(server, name=name) as base:
        async with _session(base) as session:
            return list((await session.list_tools()).tools)


def _manifest(tmp_path: Path) -> Path:
    """A `connector.yaml` declaring the probe's one tool, in a directory of its own."""
    path = tmp_path / "connector.yaml"
    path.write_text(MANIFEST, encoding="utf-8")
    return path


async def test_a_missing_surface_file_says_how_to_record_it(tmp_path: Path) -> None:
    """A golden nobody has recorded fails loudly, and the failure is the instruction.

    Silently skipping would put the mechanism in the state the finding describes: present, and
    proving nothing about the servers that have not adopted it.
    """
    tools = await _served(_probe_server(), name="surface-probe-missing")
    with pytest.raises(AssertionError, match=SURFACE_UPDATE_ENV):
        assert_manifest_matches(_manifest(tmp_path), tools)


async def test_recording_the_surface_captures_names_types_and_requiredness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the golden holds is the calling contract, and nothing that churns on a reword.

    Descriptions are deliberately out: a tool docstring is the prompt and is edited often, and a
    golden that fails on prose is a golden people regenerate without reading.
    """
    monkeypatch.setenv(SURFACE_UPDATE_ENV, "1")
    manifest = _manifest(tmp_path)
    tools = await _served(_probe_server(), name="surface-probe-record")
    assert_manifest_matches(manifest, tools)

    recorded = json.loads((tmp_path / "tool-surface.json").read_text(encoding="utf-8"))
    assert recorded == {
        "render": {
            "label": {"type": "string|null", "required": False, "default": None},
            "smiles": {"type": "string", "required": True},
            "width": {"type": "integer", "required": False, "default": 300},
        }
    }
    assert tool_surface(tools) == recorded


async def test_a_renamed_argument_fails_against_the_recorded_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one failure `MODULES.md`'s "same arguments" claim rests on, made loud.

    Every name-level check passes here: the tool is still `render`, still declared, still
    classified. Only the argument moved, which is exactly what makes it a silent break — Chemclaw3
    takes the served `inputSchema` verbatim, so the model is advertised a tool that validates and
    then rejects every call written against the old name.
    """
    monkeypatch.setenv(SURFACE_UPDATE_ENV, "1")
    manifest = _manifest(tmp_path)
    assert_manifest_matches(manifest, await _served(_probe_server(), name="surface-probe-before"))
    monkeypatch.delenv(SURFACE_UPDATE_ENV)

    renamed = await _served(_renamed_probe_server(), name="surface-probe-after")
    with pytest.raises(AssertionError) as failure:
        assert_manifest_matches(manifest, renamed)
    message = str(failure.value)
    assert "smiles" in message and "structure" in message
    assert "render" in message


async def test_names_only_still_checks_the_manifest_and_says_nothing_about_arguments(
    tmp_path: Path,
) -> None:
    """The legacy call is still the old check, on purpose — the surface is opt-in per server.

    Seven servers pass tool *names* today and are converted one at a time; a signature change that
    broke them all at once would be a worse defect than the one being fixed. What must not happen
    is the argument check appearing to run when it did not, which is why the golden's absence is an
    error above rather than a skip.
    """
    tools = await _served(_probe_server(), name="surface-probe-legacy")
    assert_manifest_matches(_manifest(tmp_path), [tool.name for tool in tools])
    assert not (tmp_path / "tool-surface.json").exists()
