"""Every upstream shape this kit depends on, asserted in one place.

A module global the kit rebinds, private attributes it reads, and upstream absences it
compensates for. Each assertion names the first-party module that breaks if it fails, and absence
pins turn red when upstream fixes something, so a workaround cannot outlive its reason.

**When one fails**, do not update the assertion and move on: read the module it names and decide
whether the dependency is still right.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from typing import Any

import jsonschema  # type: ignore[import-untyped]
import jsonschema.validators  # type: ignore[import-untyped]
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.tools import Tool, ToolManager
from mcp.server.fastmcp.utilities.func_metadata import FuncMetadata
from mcp.server.lowlevel import server as lowlevel
from mcp.server.streamable_http import MCP_SESSION_ID_HEADER, StreamableHTTPServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecurityMiddleware


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


def test_the_session_manager_reads_its_idle_timeout_and_ceiling_when_it_mints() -> None:
    """`sessions.py` sets two attributes rather than rebuilding the manager with the keywords.

    Sound only because a session takes both from the manager when it is minted; rebuilding would
    restate every constructor argument and drop whichever upstream adds next.
    """
    source = inspect.getsource(StreamableHTTPSessionManager._admit_session)
    assert "idle_timeout=self.session_idle_timeout" in source, (
        "a new session no longer takes its idle timeout from the manager at mint; "
        "mcp_server_kit/sessions.py assigns session_idle_timeout after construction"
    )
    assert "self.max_sessions" in source, (
        "the manager no longer reads its own ceiling at mint; mcp_server_kit/sessions.py turns it "
        "off after construction so the kit's ceiling is the only one"
    )


def test_a_request_in_flight_suspends_the_sessions_idle_deadline() -> None:
    """Upstream holds a session open while any request on it is in flight, a GET stream included.

    `sessions.py` no longer wraps tool calls for this; without it a four-hour CREST search would be
    reaped mid-stream with no error written. `test_sessions.py` drives it end to end.
    """
    source = inspect.getsource(StreamableHTTPServerTransport.handle_request)
    assert "self._requests_in_flight += 1" in source
    assert "self.idle_scope.deadline = math.inf" in source, (
        "the transport no longer suspends its idle deadline while a request is in flight; "
        "a long tool call can be reaped as idle again"
    )


def test_a_live_session_is_reachable_by_id_through_the_managers_instance_map() -> None:
    """The private names `sessions.py` reads a session through, and the header it starts from.

    Without `_server_instances` and `idle_scope`, the first lease cannot find or shorten a session.
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
    admit = inspect.getsource(StreamableHTTPSessionManager._admit_session)
    assert "self._server_instances[session_id] = http_transport" in admit


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
    source = inspect.getsource(StreamableHTTPSessionManager._admit_session)
    assert "if requestor is not None:" in source, (
        "upstream no longer guards `_session_owners` on an authenticated requestor; "
        "mcp_server_kit/sessions.py sweeps `_server_instances` only, on the grounds that the "
        "other map stays empty in this fleet"
    )
    assert "self._session_owners[session_id] = requestor" in source
    constructor = inspect.getsource(StreamableHTTPSessionManager.__init__)
    assert "self._session_owners" in constructor, (
        "upstream no longer keeps a second per-session map; re-read the sweep in "
        "mcp_server_kit/sessions.py, which was written knowing about exactly this one"
    )


def test_the_tool_manager_and_the_transport_guard_still_have_the_shape_rpc_calls() -> None:
    """`rpc.py` dispatches through the tool manager and runs the guard `/mcp` runs.

    It calls `ToolManager.call_tool(name, arguments, context=None, convert_result=True)`, reads
    element 1 of the `(unstructured, structured)` pair that `FastMCP.call_tool` also hands the
    transport, and awaits `TransportSecurityMiddleware.validate_request(request, is_post=True)`.
    """
    parameters = set(inspect.signature(ToolManager.call_tool).parameters)
    assert {"context", "convert_result"} <= parameters, (
        "ToolManager.call_tool no longer takes `context` and `convert_result`; "
        "mcp_server_kit/rpc.py dispatches through both"
    )
    assert "convert_result=True" in inspect.getsource(FastMCP.call_tool), (
        "FastMCP.call_tool no longer asks the tool manager for the converted pair; "
        "mcp_server_kit/rpc.py answers with the same element the transport does"
    )
    assert {"request", "is_post"} <= set(
        inspect.signature(TransportSecurityMiddleware.validate_request).parameters
    ), "TransportSecurityMiddleware.validate_request changed; mcp_server_kit/rpc.py runs it"


def test_the_validation_the_kit_moves_off_the_loop_is_still_upstream_s_six_lines() -> None:
    """`validation._OffloadedValidation` copies `FuncMetadata.call_fn_with_arg_validation`.

    For a large call it runs these statements itself, split at the validation, so a step upstream
    adds here would be skipped on exactly those calls. `Tool.run` must still reach it through
    `self.fn_metadata`, and still format a failure into its message, which is why the copy bounds a
    failure in the worker thread.
    """
    source = textwrap.dedent(inspect.getsource(FuncMetadata.call_fn_with_arg_validation))
    definition = ast.parse(source).body[0]
    assert isinstance(definition, ast.AsyncFunctionDef)
    statements = [ast.unparse(statement) for statement in definition.body]
    if statements and statements[0].startswith(("'", '"')):  # the docstring
        statements = statements[1:]
    assert statements == [
        "arguments_pre_parsed = self.pre_parse_json(arguments_to_validate)",
        "arguments_parsed_model = self.arg_model.model_validate(arguments_pre_parsed)",
        "arguments_parsed_dict = arguments_parsed_model.model_dump_one_level()",
        "arguments_parsed_dict |= arguments_to_pass_directly or {}",
        "if fn_is_async:\n    return await fn(**arguments_parsed_dict)\n"
        "else:\n    return fn(**arguments_parsed_dict)",
    ], (
        "FuncMetadata.call_fn_with_arg_validation changed; mcp_server_kit/validation.py runs a "
        "copy of it for every large tools/call"
    )
    run = inspect.getsource(Tool.run)
    assert "await self.fn_metadata.call_fn_with_arg_validation(" in run, (
        "Tool.run no longer validates through self.fn_metadata; mcp_server_kit/validation.py "
        "replaces exactly that object"
    )
    assert 'raise ToolError(f"Error executing tool {self.name}: {e}") from e' in run, (
        "Tool.run no longer formats the failure into its message; mcp_server_kit/validation.py "
        "bounds it in the worker because it did"
    )
