"""Compile each tool schema once instead of on every tool call.

`mcp.server.lowlevel.server`'s `CallToolRequest` handler has two `jsonschema.validate(...)` call
sites — the arguments against `inputSchema` and the structured result against `outputSchema` —
and `jsonschema.validate` is the *convenience* entry point: it runs `cls.check_schema(schema)`
(re-validating the schema against the JSON Schema meta-schema) and then builds a fresh validator,
every single time. Its own docstring says as much — "if you intend to validate multiple instances
with the same schema, you likely would prefer using the
`jsonschema.protocols.Validator.validate` method directly".

**Only the second of those two runs in this fleet, and saying "twice per call" was wrong.**
`FastMCP._setup_handlers` registers the handler with `validate_input=False`, so an argument is
checked by pydantic inside `Tool.run` and never reaches jsonschema — verified end to end, a
`{"name": 5}` against a `string` argument comes back as pydantic's `Input should be a valid
string`, not as upstream's `Input validation error: 5 is not of type 'string'`. Both call sites are
covered here anyway, because the shim replaces the name rather than one of its uses, and because
`validate_input` is upstream's default and a bump could restore it. The measured win is entirely
the output schema, which is also the expensive one.

A tool's schema is generated once from its signature and never changes, so that work is pure
repetition, and it runs **on the event loop**, outside every `asyncio.to_thread` in this
repository. Measured on `props`, per `solvent_properties` call: `check_schema` on its output
schema alone is 6.97 ms against 0.077 ms for a compiled validator. End to end over a real socket,
the shipped and the fixed server driven interleaved, that call went from 16.89 ms p50 to 3.96 ms
and the pod's own CPU from 10.75 ms to 2.15 ms per call — a ceiling of 93 calls/s per core to
465. The worst schemas in the fleet are worse: `calc.search_binding_modes` re-checked its output
schema in 15.1 ms per call, and `calc.calculation_key` — the one tool `servers/calc` deliberately
leaves *outside* admission control so a caller can still ask "have I computed this already?" while
the pod is full — spent ~93% of its 14.8 ms doing it.

**The key is the schema's canonical JSON text, not its identity, and that is a measured decision
rather than a stylistic one.** Keying on `id(schema)` is the obvious cheap choice and it is wrong
here twice over. `Server._get_cached_tool_definition` refreshes `Server._tool_cache` by re-running
the `ListToolsRequest` handler, and `FastMCP.list_tools` builds a *new* `mcp.types.Tool` per call
whose `inputSchema`/`outputSchema` pydantic re-validates into *new* dicts — so every `tools/list`
(Chemclaw3 sends one per turn per connector) mints fresh schema objects. An identity key would
therefore miss on every turn *and* grow without bound if it held the schemas alive to stay sound.
Held weakly it is worse than useless: measured over four `tools/list` rounds on a one-tool server,
round 3's `outputSchema` was allocated at the exact address round 0's `inputSchema` had used, so an
`id()` cache without a strong reference would have validated a result against the *arguments*
schema. Canonical JSON costs 0.003 to 0.019 ms per call against the 0.25 to 7 ms it saves, and it
bounds the cache by the *content* of the served surface — two schemas per tool, whatever
`tools/list` does with object identity.

**What this does not change is the validation itself.** `jsonschema.validate` is
`validator_for(schema)`, `check_schema(schema)`, `cls(schema)`, then
`best_match(validator.iter_errors(instance))` and raise; the only step dropped on a cache hit is
`check_schema`, which is a property of the schema and not of the instance. The first call for a
schema still runs the whole of upstream's function, so an invalid schema still raises `SchemaError`
from the same place. Anything passed a `cls` or extra arguments — nothing in the MCP SDK does —
falls straight through to upstream. `tests/test_schema_cache.py` drives valid and invalid payloads
through both paths and asserts the exception type, the `message`, the `json_path` and the
`validator` keyword are identical.

**Installed by replacing the `jsonschema` name in the SDK module's own namespace**, because the
call site is inside a closure the SDK registers in `Server.request_handlers` at import — there is
no hook, no argument and no subclass seam, and reimplementing the handler would fork sixty lines of
upstream behaviour that has nothing to do with validation. The shim delegates every other attribute
to the real module, so the handler's `except jsonschema.ValidationError` is upstream's own class.
That is a dependency on an upstream *shape*, so it is pinned in `tests/test_upstream_surface.py`
rather than only described here.
"""

from __future__ import annotations

import json
import logging
from types import ModuleType
from typing import Any

# `jsonschema` ships no `py.typed`, and it is a transitive dependency of the MCP SDK rather than
# something this workspace declares — so the ignores live here rather than as a blanket override in
# the root `pyproject.toml`, where they would also hide a real error in somebody else's module.
import jsonschema  # type: ignore[import-untyped]
import jsonschema.exceptions  # type: ignore[import-untyped]
import jsonschema.validators  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

__all__ = ["cached_validate", "install_validator_cache", "validator_cache_size"]

# One compiled validator per distinct schema *content*. Bounded by the served tool surface — the
# only schemas that reach here come from `mcp.types.Tool.inputSchema`/`outputSchema`, which come
# from the server's own tool registry — so there is nothing here for a caller to grow.
_VALIDATORS: dict[str, Any] = {}

# Set on the SDK module once, so a second `connector_app` in the same process (every test that
# builds two servers) does not wrap the shim in a shim.
_INSTALLED = "_mcp_server_kit_validator_cache"


def _cache_key(schema: object) -> str:
    """The canonical JSON text of `schema`, which is what two equal schemas share.

    `sort_keys` because a schema is a mapping and pydantic gives no ordering promise, and the
    tightest separators because this string is only ever compared, never read.
    """
    return json.dumps(schema, sort_keys=True, separators=(",", ":"))


def cached_validate(
    instance: object, schema: object, cls: Any = None, *args: Any, **kwargs: Any
) -> None:
    """`jsonschema.validate`, with the compiled validator kept between calls.

    Signature-compatible with upstream's, including the keyword spellings the MCP SDK uses
    (`instance=`, `schema=`). Anything that supplies its own validator class or extra construction
    arguments is handed straight to upstream: caching those would mean keying on them too, and
    nothing in this fleet's call path does it.

    Raises:
        jsonschema.ValidationError: `instance` does not satisfy `schema` — the same error object
            upstream's `best_match` selects, so the message the SDK folds into its error result is
            byte-identical.
        jsonschema.SchemaError: `schema` is not a valid schema. Raised from the first call for that
            schema and from every later one, because an invalid schema is never cached.
    """
    if cls is not None or args or kwargs:
        jsonschema.validate(instance, schema, cls, *args, **kwargs)
        return
    try:
        key = _cache_key(schema)
    except (TypeError, ValueError):
        # A schema that is not JSON-serialisable cannot be content-addressed. Nothing upstream
        # produces one, and validating it is still upstream's job rather than an error of ours.
        jsonschema.validate(instance, schema)
        return
    validator = _VALIDATORS.get(key)
    if validator is None:
        validator_cls = jsonschema.validators.validator_for(schema)
        # The one step a cache hit skips, kept here so an invalid schema is refused exactly as
        # upstream refuses it — and refused every time, since a schema that raises is never stored.
        validator_cls.check_schema(schema)
        validator = validator_cls(schema)
        _VALIDATORS[key] = validator
    error = jsonschema.exceptions.best_match(validator.iter_errors(instance))
    if error is not None:
        raise error


def validator_cache_size() -> int:
    """How many distinct schemas are compiled, for a test that asserts the cache stays bounded."""
    return len(_VALIDATORS)


class _JsonschemaShim:
    """`jsonschema` with `validate` memoised, and everything else the real module's.

    A shim rather than a patch of `jsonschema.validate` itself: this must change how the *MCP SDK*
    validates and nothing else in the process. A server's own code, a test, and any dependency that
    imported `jsonschema` keep upstream's function.
    """

    def __init__(self, module: ModuleType) -> None:
        """Bind the real module every other attribute is read from."""
        self._module = module
        self.validate = cached_validate

    def __getattr__(self, attribute: str) -> Any:
        """Everything but `validate` — notably `ValidationError`, which the SDK catches by name."""
        return getattr(self._module, attribute)


def install_validator_cache() -> None:
    """Point the MCP SDK's `CallToolRequest` handler at the memoised validator. Idempotent.

    Called from `connector_app`'s lifespan rather than at import, for the reason
    `configure_logging()` is: every server builds its app at module scope, and importing a module
    must not change how a *host* process behaves. Startup is the first moment this process is
    unambiguously the one being configured, and it is still long before any tool call.
    """
    from mcp.server.lowlevel import server as lowlevel

    if getattr(lowlevel, _INSTALLED, False):
        return
    # Rebinding a module global of a third party, which mypy is right to notice: `jsonschema` is
    # not part of the SDK's declared surface, and the marker beside it does not exist until now.
    lowlevel.jsonschema = _JsonschemaShim(jsonschema)  # type: ignore[attr-defined]
    setattr(lowlevel, _INSTALLED, True)
    logger.debug("tool schema validators are memoised per schema")
