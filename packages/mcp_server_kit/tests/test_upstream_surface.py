"""Every upstream shape this kit depends on, asserted in one place.

A module global the kit rebinds, private attributes it reads, and upstream absences it
compensates for. Each assertion names the first-party module that breaks if it fails, and absence
pins turn red when upstream fixes something, so a workaround cannot outlive its reason.

**When one fails**, do not update the assertion and move on: read the module it names and decide
whether the dependency is still right.
"""

from __future__ import annotations

import inspect
from typing import Any

import jsonschema  # type: ignore[import-untyped]
import jsonschema.validators  # type: ignore[import-untyped]
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

    Validation happens in a closure the SDK registers at decoration time, so rebinding the name it
    looks up is the narrowest intervention, and it fails silently if upstream imports `validate`
    directly.
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

    `FastMCP` registers call-tool with `validate_input=False`, so only the output schema reaches
    jsonschema. The shim covers both, but `schema_cache.py`'s reasoning would need correcting if
    this flipped.
    """
    source = inspect.getsource(FastMCP._setup_handlers)
    assert "call_tool(validate_input=False)" in source, (
        "FastMCP no longer disables the lowlevel input validation; mcp_server_kit/schema_cache.py "
        "says only the output schema reaches jsonschema in this fleet"
    )


def test_jsonschema_validate_is_still_check_schema_then_best_match() -> None:
    """`cached_validate` is `jsonschema.validate` with the schema-side work hoisted out of the loop.

    Faithful only while upstream is `validator_for`, `check_schema`, construct, `best_match`, raise;
    a new upstream step would silently be skipped.
    """
    source = inspect.getsource(jsonschema.validators.validate)
    for step in ("validator_for(schema)", "cls.check_schema(schema)", "best_match("):
        assert step in source, (
            f"jsonschema.validate no longer does `{step}`; "
            "mcp_server_kit/schema_cache.py reimplements it minus the per-call check_schema"
        )


def test_fastmcp_still_does_not_pass_a_session_idle_timeout() -> None:
    """An absence pin: `FastMCP` does not pass a session idle timeout, which is why `sessions.py`
    exists.

    If upstream starts passing it, `sessions.py` becomes an override rather than the only GC.
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

    Sound only because both readers read `self.session_idle_timeout` at run time; rebuilding would
    restate every constructor argument and drop whichever upstream adds next.
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

    Without `_server_instances` and `idle_scope`, a tool call cannot find its session and long calls
    would silently stop being held open.
    """
    manager = StreamableHTTPSessionManager(app=_probe()._mcp_server)
    assert isinstance(manager._server_instances, dict)
    assert hasattr(StreamableHTTPServerTransport(mcp_session_id=None), "idle_scope")
    assert MCP_SESSION_ID_HEADER == "mcp-session-id"


def test_a_session_is_minted_exactly_when_the_session_id_header_is_absent() -> None:
    """Upstream mints a session exactly when the session-id header is absent.

    `apply_session_ceiling` decides on the header alone, as `_handle_stateful_request` does. Read
    from source because the condition is what must not drift: minting on `initialize` instead would
    pass every behavioural test while the ceiling gated the wrong requests.
    """
    source = inspect.getsource(StreamableHTTPSessionManager._handle_stateful_request)
    assert "request_mcp_session_id = request.headers.get(MCP_SESSION_ID_HEADER)" in source
    assert "if request_mcp_session_id is None:" in source, (
        "upstream no longer decides to mint a session on the absence of the session-id header; "
        "`mcp_server_kit.sessions._would_mint_a_session` mirrors that branch and has to be "
        "re-derived from whatever replaced it"
    )
    # And the map it adds to is the one the ceiling counts, so "will mint" and "is counted" are
    # about the same dict.
    assert "self._server_instances[http_transport.mcp_session_id] = http_transport" in source


async def test_list_tools_rebuilds_a_tools_schema_objects_every_time() -> None:
    """`list_tools` rebuilds a tool's schema objects every time, so `schema_cache` keys on content.

    An identity-keyed cache would miss on every turn and, held weakly, could return a validator for
    a different schema at a reused address.
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


def test_a_session_is_recorded_in_a_second_map_only_for_an_authenticated_scope_user() -> None:
    """Upstream records a session owner only for an authenticated scope user.

    `_drop_terminated_sessions` sweeps `_server_instances` but not `_session_owners`. That is sound
    only while `BearerAuthMiddleware` sets no `AuthenticatedUser`; if upstream records owners
    unconditionally, or the kit adopts upstream's bearer middleware, deleted sessions leak there.
    """
    source = inspect.getsource(StreamableHTTPSessionManager._handle_stateful_request)
    assert "if requestor is not None:" in source, (
        "upstream no longer guards `_session_owners` on an authenticated requestor; "
        "mcp_server_kit/sessions.py sweeps `_server_instances` only, on the grounds that the "
        "other map stays empty in this fleet"
    )
    assert "self._session_owners[http_transport.mcp_session_id] = requestor" in source
    constructor = inspect.getsource(StreamableHTTPSessionManager.__init__)
    assert "self._session_owners" in constructor, (
        "upstream no longer keeps a second per-session map; re-read the sweep in "
        "mcp_server_kit/sessions.py, which was written knowing about exactly this one"
    )
