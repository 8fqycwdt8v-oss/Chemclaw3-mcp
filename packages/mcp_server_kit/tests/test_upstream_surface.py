"""Every upstream shape this kit depends on, asserted in one place.

`mcp_server_kit` is a thin layer over the MCP SDK by design, and most of what it does is public
API. A handful of things are not: a module global it rebinds, two private attributes it reads, and
one upstream *absence* it exists to compensate for. Each of those is a sentence in some module's
docstring today, and a docstring is evidence about what its author believed — so a dependency bump
that invalidates one would leave six confident paragraphs and a green build.

This file is the other half. Every assertion names the first-party module that breaks if it fails,
and two of them assert an absence, so that upstream *fixing* something turns the workaround red
instead of letting it outlive its reason. Chemclaw3 keeps a file with the same name for the same
purpose, and for the same finding: what breaks on a dependency bump is not the volume of
first-party code but the number of places reading a shape upstream never promised.

**When one of these fails**, the fix is never to update the assertion and move on. Go and read the
module it names, decide whether the dependency is still the right one, and record the answer.
"""

from __future__ import annotations

import inspect
from typing import Any

import jsonschema
import jsonschema.validators
from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel import server as lowlevel
from mcp.server.streamable_http import MCP_SESSION_ID_HEADER, StreamableHTTPServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager


def _probe() -> FastMCP:
    """A one-tool server, enough to exercise the handler registry and the tool cache."""
    server = FastMCP("upstream-surface-probe")

    @server.tool()
    def echo(text: str) -> str:
        """Return what was passed in."""
        return text

    return server


def test_the_lowlevel_server_still_validates_through_a_module_global_named_jsonschema() -> None:
    """`schema_cache.install_validator_cache` rebinds that global; there is no other seam.

    The validation happens inside a closure the SDK registers in `Server.request_handlers` at
    decoration time — no hook, no argument, no subclass point. Rebinding the name the closure looks
    up is the narrowest possible intervention, and it stops working silently the moment upstream
    imports `validate` directly (`from jsonschema import validate`) or moves the call.
    """
    assert hasattr(lowlevel, "jsonschema"), (
        "mcp.server.lowlevel.server no longer has a module-global `jsonschema`; "
        "mcp_server_kit/schema_cache.py rebinds exactly that name"
    )
    source = inspect.getsource(lowlevel)
    assert "jsonschema.validate(instance=arguments, schema=tool.inputSchema)" in source, (
        "the lowlevel CallToolRequest handler no longer validates arguments through "
        "`jsonschema.validate`; mcp_server_kit/schema_cache.py memoises exactly that call"
    )
    assert (
        "jsonschema.validate(instance=maybe_structured_content, schema=tool.outputSchema)" in source
    ), (
        "the lowlevel CallToolRequest handler no longer validates results through "
        "`jsonschema.validate`; mcp_server_kit/schema_cache.py memoises exactly that call"
    )
    assert "except jsonschema.ValidationError" in source, (
        "the handler no longer catches `jsonschema.ValidationError` off the module global; "
        "mcp_server_kit/schema_cache.py's shim delegates that attribute for this reason"
    )


def test_fastmcp_still_disables_the_lowlevel_servers_input_validation() -> None:
    """Which of the two `jsonschema.validate` call sites actually runs, and it is only one.

    `FastMCP._setup_handlers` registers the call-tool handler with `validate_input=False`, so an
    argument is checked by pydantic inside `Tool.run` and never reaches jsonschema; the *output*
    schema is the one that costs 6.97 ms a call. `schema_cache.py` says so, and a bump that
    re-enabled input validation would make that paragraph wrong in the direction that reads as a
    bigger win than it is. Nothing breaks if it flips — the shim covers both sites — but the prose
    has to be corrected, so this is a red build rather than a stale sentence.
    """
    source = inspect.getsource(FastMCP._setup_handlers)
    assert "call_tool(validate_input=False)" in source, (
        "FastMCP no longer disables the lowlevel input validation; mcp_server_kit/schema_cache.py "
        "says only the output schema reaches jsonschema in this fleet"
    )


def test_jsonschema_validate_is_still_check_schema_then_best_match() -> None:
    """`cached_validate` is that function with the schema-side work hoisted out of the loop.

    It is a faithful reimplementation only as long as upstream's is `validator_for`,
    `check_schema`, construct, `best_match(iter_errors(...))`, raise. If upstream grows a step, the
    cached path silently stops doing it — which is exactly the class of divergence
    `tests/test_schema_cache.py`'s differential test would catch for the *shapes it covers* and
    not necessarily for others.
    """
    source = inspect.getsource(jsonschema.validators.validate)
    for step in ("validator_for(schema)", "cls.check_schema(schema)", "best_match("):
        assert step in source, (
            f"jsonschema.validate no longer does `{step}`; "
            "mcp_server_kit/schema_cache.py reimplements it minus the per-call check_schema"
        )


def test_fastmcp_still_does_not_pass_a_session_idle_timeout() -> None:
    """An **absence** pin: the reason `mcp_server_kit/sessions.py` exists at all.

    `StreamableHTTPSessionManager` has taken `session_idle_timeout` for some time and recommends
    1800 s; `FastMCP.streamable_http_app()` has never passed it, so every server in this fleet ran
    with no session GC. If upstream starts passing it, this goes red and `sessions.py` becomes a
    deployment's *override* rather than the only thing standing between a pod and an OOMKill —
    which is a different module with a different argument.
    """
    source = inspect.getsource(FastMCP.streamable_http_app)
    assert "StreamableHTTPSessionManager(" in source, (
        "FastMCP.streamable_http_app no longer constructs the session manager lazily; "
        "mcp_server_kit/sessions.py sets its idle timeout after that call"
    )
    assert "session_idle_timeout" not in source, (
        "FastMCP now passes session_idle_timeout itself; re-read mcp_server_kit/sessions.py, "
        "which exists only because it did not"
    )


def test_the_session_manager_reads_its_idle_timeout_at_run_time() -> None:
    """`sessions.py` sets the attribute rather than rebuilding the manager with the keyword.

    That is only sound because both readers — the request handler that pushes the deadline forward
    and the session task that arms it — read `self.session_idle_timeout` when they run. Rebuilding
    the manager instead would mean restating every other constructor argument here, and silently
    dropping whichever one upstream adds next.
    """
    assert "session_idle_timeout" in inspect.signature(StreamableHTTPSessionManager).parameters
    source = inspect.getsource(StreamableHTTPSessionManager)
    assert source.count("self.session_idle_timeout") >= 3, (
        "the session manager no longer reads self.session_idle_timeout at request time; "
        "mcp_server_kit/sessions.py assigns it after construction"
    )
    assert "idle_scope.deadline" in source, (
        "the session manager no longer expires a session through an anyio CancelScope deadline; "
        "mcp_server_kit/sessions.py suspends exactly that deadline while a tool call runs"
    )


def test_a_live_session_is_reachable_by_id_through_the_managers_instance_map() -> None:
    """The two private names `sessions._current_session` walks, and the header it starts from.

    `_server_instances` maps a session id to its transport and `idle_scope` is the deadline on it.
    Neither is public API; without both, a tool call cannot find the session it is being served on,
    and `sessions.py` would silently stop holding long calls open — a CREST search cancelled at 30
    minutes with nothing in the logs but "idle timeout".
    """
    manager = StreamableHTTPSessionManager(app=_probe()._mcp_server)
    assert isinstance(manager._server_instances, dict)
    assert hasattr(StreamableHTTPServerTransport(mcp_session_id=None), "idle_scope")
    assert MCP_SESSION_ID_HEADER == "mcp-session-id"


async def test_list_tools_rebuilds_a_tools_schema_objects_every_time() -> None:
    """Why `schema_cache` keys on content: the schema *object* is not stable for a process.

    `Server._get_cached_tool_definition` refreshes `_tool_cache` by re-running the ListToolsRequest
    handler, and `FastMCP.list_tools` builds a fresh `mcp.types.Tool` per call whose schema dicts
    pydantic re-validates into new objects. Chemclaw3 sends one `tools/list` per turn per
    connector, so an identity-keyed cache would miss on every turn — and, held weakly, could hand
    back a validator compiled for a different schema that happened to reuse the address.
    """
    import mcp.types as types

    server = _probe()
    handler: Any = server._mcp_server.request_handlers[types.ListToolsRequest]
    seen = []
    for _ in range(4):
        await handler(types.ListToolsRequest(method="tools/list"))
        tool = server._mcp_server._tool_cache["echo"]
        seen.append((id(tool.inputSchema), id(tool.outputSchema)))
    assert len(set(seen)) > 1, (
        "tool schema objects are stable across tools/list now; mcp_server_kit/schema_cache.py "
        "pays a canonical-JSON key per call to be safe against them not being"
    )
