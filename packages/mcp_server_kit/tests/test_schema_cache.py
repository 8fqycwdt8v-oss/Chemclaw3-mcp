"""The memoised validator must accept and refuse exactly what upstream's does.

`schema_cache` exists for speed, and a speed-up that changes *which* payloads a server accepts, or
what it says when it refuses one, is a correctness regression wearing a benchmark. So the central
test here is a differential one: the same schemas and the same instances through
`jsonschema.validate` and through `cached_validate`, asserting the outcomes agree down to the
message, the failing keyword and the JSON path — the four things the SDK folds into
`Input validation error: ...` and hands to the model.

The second thing asserted is the property that makes the *key* safe. Keying a compiled validator on
`id(schema)` is the obvious choice and it is wrong here, because `FastMCP.list_tools` rebuilds every
tool's schema dicts on every `tools/list` and Chemclaw3 sends one per turn per connector. These
tests pin content-addressing from both sides: equal-but-distinct schema objects share one entry
(so the cache does not grow per turn), and different schemas never do (so no instance is ever
checked against the wrong schema).
"""

from __future__ import annotations

import contextlib
import copy
from collections.abc import Iterator
from typing import Any

import jsonschema
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

    `message` is what the SDK puts in front of the model; `validator` and `validator_value` are
    which keyword failed and against what; `json_path` and `absolute_path` are where. Two
    validators that agree on all five are indistinguishable to everything downstream.
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
    """`check_schema` is skipped on a cache *hit*, and a bad schema never produces one.

    The saving is the whole point of the module, so the risk it creates has to be pinned: a schema
    that upstream refuses must be refused on the hundredth call as loudly as on the first, which is
    true only because a schema that raises is never stored.
    """
    broken = {"type": "obhect"}
    for _ in range(3):
        with pytest.raises(jsonschema.SchemaError):
            cached_validate(instance={}, schema=broken)
        with pytest.raises(jsonschema.SchemaError):
            jsonschema.validate(instance={}, schema=broken)
    assert validator_cache_size() == 0


def test_equal_schemas_from_different_objects_share_one_compiled_validator() -> None:
    """The reason the key is the schema's *content* and not its address.

    `FastMCP.list_tools` builds a new `mcp.types.Tool` per call, and pydantic re-validates its
    `inputSchema`/`outputSchema` into new dicts — so every `tools/list` hands the validator a fresh
    object holding the same schema. An identity key would miss on all of them; this asserts the
    content key does not, which is what keeps the cache both effective and bounded by the served
    surface rather than by the request rate.
    """
    for _ in range(50):
        cached_validate(instance={"name": "toluene"}, schema=copy.deepcopy(SCHEMAS["scalar"]))
    assert validator_cache_size() == 1


def test_different_schemas_never_share_an_entry() -> None:
    """The other direction, and the one that would be a *wrong answer* rather than a slow one.

    Measured on a one-tool server, four `tools/list` rounds apart: round 3's `outputSchema` was
    allocated at the address round 0's `inputSchema` had used. A cache keyed on `id()` without a
    strong reference would therefore have validated a tool's result against its arguments schema
    and accepted it. Content keys cannot collide that way, and this is the assertion that says so.
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
    """A `set` in a schema has no canonical JSON, so it cannot be keyed — and must still validate.

    Nothing upstream produces one; the branch exists so that a schema this module cannot address
    is handed to `jsonschema.validate` unchanged rather than becoming a `TypeError` from
    `json.dumps` that a caller would read as a validation failure.
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
    once = lowlevel.jsonschema
    schema_cache.install_validator_cache()
    assert lowlevel.jsonschema is once
    # And the shim is still `jsonschema` to every other name the SDK reads off it — the handler's
    # `except jsonschema.ValidationError` has to catch upstream's own class.
    assert lowlevel.jsonschema.ValidationError is jsonschema.ValidationError
    assert lowlevel.jsonschema.SchemaError is jsonschema.SchemaError
