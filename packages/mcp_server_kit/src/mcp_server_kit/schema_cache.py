"""Compile each tool schema once instead of on every tool call.

The MCP SDK's `CallToolRequest` handler calls `jsonschema.validate`, which re-checks the schema
against the meta-schema and builds a new validator every call — on the event loop, and the
dominant per-call cost for output schemas. (Input validation is off under `FastMCP`; both sites
are covered in case that changes.)

- Keyed by the schema's canonical JSON, not `id()`: `tools/list` mints new schema dicts each turn,
  and a weak identity key could even match a different schema at a reused address.
- Only `check_schema` is skipped on a hit, so errors are identical to upstream's (asserted in
  `tests/test_schema_cache.py`); calls with a custom `cls` or extra arguments go to upstream.
- Installed by replacing the `jsonschema` name in the SDK module's namespace, the only seam;
  pinned in `tests/test_upstream_surface.py`.
"""

from __future__ import annotations

import json
import logging
from types import ModuleType
from typing import Any

# Untyped transitive dependency; ignored here rather than in a blanket `pyproject.toml` override.
import jsonschema  # type: ignore[import-untyped]
import jsonschema.exceptions  # type: ignore[import-untyped]
import jsonschema.validators  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

__all__ = ["cached_validate", "install_validator_cache", "validator_cache_size"]

# One compiled validator per distinct schema content, bounded by the served tool surface.
_VALIDATORS: dict[str, Any] = {}

# Set on the SDK module once, so a second `connector_app` in the same process (every test that
# builds two servers) does not wrap the shim in a shim.
_INSTALLED = "_mcp_server_kit_validator_cache"


def _cache_key(schema: object) -> str:
    """The canonical JSON text of `schema`, which is what two equal schemas share."""
    return json.dumps(schema, sort_keys=True, separators=(",", ":"))


def cached_validate(
    instance: object, schema: object, cls: Any = None, *args: Any, **kwargs: Any
) -> None:
    """`jsonschema.validate`, with the compiled validator kept between calls.

    Signature-compatible with upstream's; a custom validator class or extra arguments go straight to
    upstream.

    Raises:
        jsonschema.ValidationError: `instance` does not satisfy `schema` (upstream's `best_match`).
        jsonschema.SchemaError: `schema` is invalid; never cached, so raised on every call.
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

    A shim in the SDK's namespace, so nothing else in the process changes behaviour.
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

    Called from `connector_app`'s lifespan, not at import, so importing a server changes nothing.
    """
    from mcp.server.lowlevel import server as lowlevel

    if getattr(lowlevel, _INSTALLED, False):
        return
    # Rebinding a module global of a third party, which mypy is right to notice: `jsonschema` is
    # not part of the SDK's declared surface, and the marker beside it does not exist until now.
    lowlevel.jsonschema = _JsonschemaShim(jsonschema)  # type: ignore[attr-defined]
    setattr(lowlevel, _INSTALLED, True)
    logger.debug("tool schema validators are memoised per schema")
