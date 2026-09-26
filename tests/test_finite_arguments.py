"""Every float argument of every served tool refuses a NaN and an infinity, derived from its schema.

`mcp_server_kit/finite.py` holds the mechanism and its own tests; this file holds the claim that it
reaches the whole fleet. Derived rather than listed, from the JSON schema each tool advertises: a
number anywhere in it — a bare argument, an optional one, an element of a list, a field of a nested
model — is a place a caller can put a NaN, so each one is driven. A server that registered a tool
after `connector_app` had run, or built its app some other way, is what this would catch.

Three servers carried their own engine guards before this and they stay: they are what a direct
Python caller meets. This is about what a caller of the *served* surface meets.
"""

from __future__ import annotations

import asyncio
import importlib
import math
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp_server_kit.finite import NON_FINITE_REFUSAL, NonFiniteLiteralRefusal
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]

#: pydantic's own refusals of a non-finite value against a declared bound: a NaN fails every
#: comparison, so a `gt=0` field refuses it before `finite.py` is reached, in its own words.
_BOUND_ERRORS = {"greater_than", "greater_than_equal", "less_than", "less_than_equal"}

#: Deep enough for every argument model served today; a schema that recurses past it is not driven.
_MAX_DEPTH = 6

_Path = tuple[str | int, ...]


def _servers() -> list[str]:
    """Every server directory, by name."""
    return sorted(
        path.name
        for path in (ROOT / "servers").iterdir()
        if path.is_dir() and (path / "connector.yaml").exists()
    )


def _resolve(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    """`schema` with a `$ref` into the tool's own `$defs` followed."""
    ref = schema.get("$ref")
    if isinstance(ref, str):
        return dict(root["$defs"][ref.rsplit("/", 1)[-1]])
    return schema


def _branches(schema: dict[str, Any], root: dict[str, Any]) -> list[dict[str, Any]]:
    """The non-null alternatives of `schema` — itself, unless it is an `anyOf`."""
    schema = _resolve(schema, root)
    options = schema.get("anyOf") or schema.get("oneOf")
    if not options:
        return [schema]
    return [_resolve(option, root) for option in options if option.get("type") != "null"]


def _filler(schema: dict[str, Any], root: dict[str, Any]) -> Any:
    """A value `schema` accepts, for the siblings of the number being driven inside a nested model.

    A nested model is validated as a whole, so a missing sibling would refuse it before its float
    was ever looked at and the test would pass for the wrong reason. Top-level siblings need no
    filler: pydantic reports each argument separately.
    """
    branch = _branches(schema, root)[0]
    if "enum" in branch:
        return branch["enum"][0]
    kind = branch.get("type")
    if kind in {"number", "integer"}:
        low = branch.get("minimum", branch.get("exclusiveMinimum"))
        high = branch.get("maximum", branch.get("exclusiveMaximum"))
        if low is not None and high is not None:
            value = (low + high) / 2
        elif low is not None:
            value = low + 1
        elif high is not None:
            value = high - 1
        else:
            # Two rather than one for an integer: `calc`'s `Structure` reads its integers as atomic
            # numbers, and three hydrogens are an open shell it refuses before the float is seen.
            value = 2 if kind == "integer" else 1
        return int(value) if kind == "integer" else float(value)
    if kind == "string":
        return "C" * max(1, int(branch.get("minLength", 1)))
    if kind == "boolean":
        return False
    if kind == "array":
        count = max(int(branch.get("minItems", 0)), 3)
        count = min(count, int(branch.get("maxItems", count)))
        return [_filler(branch.get("items", {}), root) for _ in range(count)]
    if kind == "object":
        properties = branch.get("properties", {})
        return {name: _filler(properties[name], root) for name in branch.get("required", [])}
    return None


def _numbers(
    schema: dict[str, Any], root: dict[str, Any], at: _Path, depth: int
) -> Iterator[_Path]:
    """Every path under `schema` at which a JSON number is accepted."""
    if depth > _MAX_DEPTH:
        return
    for branch in _branches(schema, root):
        kind = branch.get("type")
        if kind == "number":
            yield at
        elif kind == "array":
            yield from _numbers(branch.get("items", {}), root, (*at, 0), depth + 1)
        elif kind == "object":
            for name, sub in branch.get("properties", {}).items():
                yield from _numbers(sub, root, (*at, name), depth + 1)


def _placed(schema: dict[str, Any], root: dict[str, Any], rest: _Path, bad: float) -> Any:
    """A value for `schema` with `bad` at `rest` and valid fillers everywhere else it needs them."""
    if not rest:
        return bad
    head, tail = rest[0], rest[1:]
    for branch in _branches(schema, root):
        if isinstance(head, int) and branch.get("type") == "array":
            items = _filler(branch, root)
            items[head] = _placed(branch.get("items", {}), root, tail, bad)
            return items
        if isinstance(head, str) and head in branch.get("properties", {}):
            value = _filler(branch, root)
            value[head] = _placed(branch["properties"][head], root, tail, bad)
            return value
    raise AssertionError(f"no branch of the schema reaches {rest}")


def _spelled(path: _Path) -> str:
    """`path` the way `finite.first_non_finite` names it in a refusal."""
    text = str(path[0])
    for part in path[1:]:
        text += f"[{part}]" if isinstance(part, int) else f".{part}"
    return text


def _cases() -> list[Any]:
    """(server, tool, path) for every number every served tool accepts."""
    cases = []
    for server in _servers():
        package = f"chemclaw_mcp_{server}"
        importlib.import_module(f"{package}.app")
        tools = importlib.import_module(f"{package}.tools")
        for tool in tools.server._tool_manager.list_tools():
            root = tool.parameters
            for name, sub in root.get("properties", {}).items():
                for path in _numbers(sub, root, (name,), 0):
                    cases.append(
                        pytest.param(server, tool.name, path, id=f"{tool.name}:{_spelled(path)}")
                    )
    return cases


_CASES = _cases()


def test_the_derivation_found_the_fleet_s_numbers() -> None:
    """A derivation that silently found nothing would make every test below vacuous."""
    assert len({server for server, *_ in (case.values for case in _CASES)}) >= 5
    nested = [case for case in _CASES if len(case.values[2]) > 1]
    assert nested, (
        "no number inside a list or a nested model was found; has the schema walk broken?"
    )


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf], ids=["nan", "inf", "-inf"])
@pytest.mark.parametrize(("server", "tool", "path"), _CASES)
def test_every_float_argument_refuses_a_non_finite_value(
    server: str, tool: str, path: _Path, bad: float
) -> None:
    """The served call is refused at validation, naming this argument, before any tool body runs."""
    tools = importlib.import_module(f"chemclaw_mcp_{server}.tools")
    manager = tools.server._tool_manager
    root = manager.get_tool(tool).parameters
    arguments = {path[0]: _placed(root["properties"][path[0]], root, path[1:], bad)}
    with pytest.raises(ToolError) as refused:
        asyncio.run(manager.call_tool(tool, arguments))
    cause = refused.value.__cause__
    assert isinstance(cause, ValidationError), (
        f"{tool}({_spelled(path)}={bad}) reached the tool body instead of being refused at "
        f"validation: {refused.value}"
    )
    here = [error for error in cause.errors() if error["loc"][:1] == path[:1]]
    ours = [error for error in here if f"{_spelled(path)} {NON_FINITE_REFUSAL}" in error["msg"]]
    bounded = [
        error
        for error in here
        if error["type"] in _BOUND_ERRORS
        and [part for part in error["loc"] if part in path] == list(path)
    ]
    assert ours or bounded, (
        f"{tool}({_spelled(path)}={bad}) was refused, but not for being non-finite: {here}. "
        "Either the filler for a sibling is wrong, or this argument accepts a NaN."
    )


@pytest.mark.parametrize("server", _servers())
def test_every_served_app_refuses_the_literal_before_the_transport_reads_it(server: str) -> None:
    """The body half is tool-agnostic, so what each server owes is only to be built with it."""
    app = importlib.import_module(f"chemclaw_mcp_{server}.app").app
    assert any(entry.cls is NonFiniteLiteralRefusal for entry in app.user_middleware), (
        f"{server}'s app does not carry NonFiniteLiteralRefusal: a bare `NaN` in a request body "
        "reaches an optional argument as `None`. Build the app with `connector_app`."
    )
