"""The memoised validator must accept and refuse exactly what upstream's does.

A differential test runs the same schemas and instances through `jsonschema.validate` and
`cached_validate`, comparing message, failing keyword and JSON path, which is what the SDK hands
to the model.

The key is the schema's content, not `id()`: `FastMCP.list_tools` rebuilds schema dicts on every
call, so equal schemas must share one entry and different schemas never do.
"""

from __future__ import annotations

import contextlib
import copy
from collections.abc import Iterator
from typing import Any

import jsonschema  # type: ignore[import-untyped]
import pytest
from mcp_server_kit import schema_cache
from mcp_server_kit.schema_cache import cached_validate, validator_cache_size

# Shapes chosen for what they make `best_match` do, not for what they describe: a plain required
# field, a nested object, an enum, a union whose branches both fail, and an array with a typed item
# — the last two are where a naive reimplementation picks a different error to raise than upstream.
SCHEMAS: dict[str, dict[str, Any]] = {
    "scalar": {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    },
    "nested": {
        "type": "object",
        "properties": {
            "solvent": {
                "type": "object",
                "properties": {"cas": {"type": "string"}, "bp_c": {"type": "number"}},
                "required": ["cas"],
            }
        },
        "required": ["solvent"],
    },
    "enum": {
        "type": "object",
        "properties": {"method": {"enum": ["antoine", "trouton"]}},
        "required": ["method"],
    },
    "union": {
        "type": "object",
        "properties": {"charge": {"anyOf": [{"type": "integer"}, {"type": "null"}]}},
    },
    "array": {
        "type": "object",
        "properties": {"names": {"type": "array", "items": {"type": "string"}, "minItems": 1}},
        "required": ["names"],
    },
}

INSTANCES: list[Any] = [
    {},
    {"name": "toluene"},
    {"name": 5},
    {"solvent": {"cas": "108-88-3", "bp_c": 110.6}},
    {"solvent": {"bp_c": 110.6}},
    {"solvent": "toluene"},
    {"method": "antoine"},
    {"method": "guessing"},
    {"charge": 0},
    {"charge": "zero"},
    {"names": ["toluene", "ethanol"]},
    {"names": []},
    {"names": [1, 2]},
    "not an object at all",
]


@pytest.fixture(autouse=True)
def empty_cache() -> Iterator[None]:
    """Every test starts from a cold cache, so a size assertion means what it says."""
    schema_cache._VALIDATORS.clear()
    yield
    schema_cache._VALIDATORS.clear()


def _outcome(validate: Any, instance: Any, schema: Any) -> tuple[Any, ...]:
    """What one validation did, in the terms a caller can observe.

    Message, failing keyword and value, and both paths; two validators agreeing on all five are
    indistinguishable downstream.
    """
    try:
        validate(instance=instance, schema=schema)
    except jsonschema.ValidationError as error:
        return (
            type(error).__name__,
            error.message,
            error.validator,
            error.validator_value,
            error.json_path,
            tuple(error.absolute_path),
        )
    return ("ok",)


@pytest.mark.parametrize("schema_name", sorted(SCHEMAS))
def test_the_cached_validator_agrees_with_upstream_on_every_instance(schema_name: str) -> None:
    """The differential test. Same verdict, same message, same keyword, same path — every time.

    Run against a *copy* of the schema on each side, because the cached path is allowed to mutate
    nothing and a shared object would hide it if it did.
    """
    schema = SCHEMAS[schema_name]
    for instance in INSTANCES:
        upstream = _outcome(jsonschema.validate, instance, copy.deepcopy(schema))
        cached = _outcome(cached_validate, instance, copy.deepcopy(schema))
        assert cached == upstream, (
            f"{schema_name} disagreed on {instance!r}: upstream {upstream}, cached {cached}"
        )
        # And again, now that the validator is warm — the second call is the one that skips
        # `check_schema`, so it is the one that could differ.
        assert _outcome(cached_validate, instance, copy.deepcopy(schema)) == upstream


def test_an_invalid_schema_still_raises_schema_error_every_time() -> None:
    """An invalid schema raises `SchemaError` on every call, not just the first.

    `check_schema` is skipped on a hit, which is safe only because a raising schema is never stored.
    """
    broken = {"type": "obhect"}
    for _ in range(3):
        with pytest.raises(jsonschema.SchemaError):
            cached_validate(instance={}, schema=broken)
        with pytest.raises(jsonschema.SchemaError):
            jsonschema.validate(instance={}, schema=broken)
    assert validator_cache_size() == 0


def test_equal_schemas_from_different_objects_share_one_compiled_validator() -> None:
    """Equal schemas from different objects share one compiled validator.

    Every `tools/list` hands the validator fresh dicts, so a content key keeps the cache effective
    and bounded by the served surface rather than the request rate.
    """
    for _ in range(50):
        cached_validate(instance={"name": "toluene"}, schema=copy.deepcopy(SCHEMAS["scalar"]))
    assert validator_cache_size() == 1


def test_different_schemas_never_share_an_entry() -> None:
    """Different schemas never share an entry.

    Freed dicts' addresses get reused, so an `id()` key could validate a result against an arguments
    schema; content keys cannot collide that way.
    """
    for schema in SCHEMAS.values():
        # `{}` satisfies some of these and not others; the verdict is not what this test is about.
        with contextlib.suppress(jsonschema.ValidationError):
            cached_validate(instance={}, schema=copy.deepcopy(schema))
    assert validator_cache_size() == len(SCHEMAS)
    # The verdict for one schema is unaffected by every other schema now in the cache.
    with pytest.raises(jsonschema.ValidationError) as raised:
        cached_validate(instance={"method": "guessing"}, schema=copy.deepcopy(SCHEMAS["enum"]))
    assert "guessing" in raised.value.message


def test_an_explicit_validator_class_falls_through_to_upstream() -> None:
    """Nothing in the MCP SDK passes one, and caching it would mean keying on it too."""
    with pytest.raises(jsonschema.ValidationError):
        cached_validate(
            instance={"name": 5},
            schema=copy.deepcopy(SCHEMAS["scalar"]),
            cls=jsonschema.Draft202012Validator,
        )
    assert validator_cache_size() == 0


def test_a_schema_that_is_not_json_serialisable_falls_through_to_upstream() -> None:
    """A schema that is not JSON-serialisable falls through to `jsonschema.validate` unchanged.

    It cannot be keyed, and a `TypeError` from `json.dumps` would read as a validation failure.
    """
    schema = {"type": "object", "properties": {"name": {"const": {1, 2}}}}
    assert _outcome(cached_validate, {"name": "toluene"}, schema) == _outcome(
        jsonschema.validate, {"name": "toluene"}, schema
    )
    assert validator_cache_size() == 0


def test_installing_the_cache_twice_does_not_wrap_the_shim_in_a_shim() -> None:
    """Two `connector_app` calls in one process is what every test file here does."""
    from mcp.server.lowlevel import server as lowlevel

    schema_cache.install_validator_cache()
    # `jsonschema` is a name the SDK imports and does not declare, which is exactly why this shim
    # can be swapped in at all — the ignores say so at the four sites that read it, the way
    # `schema_cache.install_validator_cache` already does at the site that writes it.
    once = lowlevel.jsonschema  # type: ignore[attr-defined]
    schema_cache.install_validator_cache()
    assert lowlevel.jsonschema is once  # type: ignore[attr-defined]
    # And the shim is still `jsonschema` to every other name the SDK reads off it — the handler's
    # `except jsonschema.ValidationError` has to catch upstream's own class.
    assert lowlevel.jsonschema.ValidationError is jsonschema.ValidationError  # type: ignore[attr-defined]
    assert lowlevel.jsonschema.SchemaError is jsonschema.SchemaError  # type: ignore[attr-defined]
