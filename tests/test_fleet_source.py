"""Serving-code discipline: no `assert` as a control, and no refusal echoes caller text past the
echo bound.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


# Modules whose product is an assertion failure, imported only by tests, so an `assert` there is
# the verdict. Everywhere else under `src/` is serving code, where `python -O` deletes asserts.
ASSERT_IS_THE_PRODUCT = {
    "packages/mcp_server_kit/src/mcp_server_kit/testing.py",
    "packages/mcp_server_kit/src/mcp_server_kit/no_egress.py",
}


def _assert_offences(roots: list[Path]) -> list[str]:
    """Every `assert` statement under `roots`, minus the modules whose product is an assertion.

    AST-based rather than grep-based, for `no_egress.py`'s reason one layer over: an `assert` in a
    docstring, a comment or a string literal reads identically as text and not at all as a tree.
    """
    offences: list[str] = []
    for root in roots:
        for source in sorted(root.rglob("*.py")):
            relative = (
                source.relative_to(ROOT).as_posix()
                if source.is_relative_to(ROOT)
                else source.as_posix()
            )
            if relative in ASSERT_IS_THE_PRODUCT:
                continue
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            offences += [
                f"{relative}:{node.lineno}"
                for node in ast.walk(tree)
                if isinstance(node, ast.Assert)
            ]
    return sorted(offences)


def test_no_serving_module_enforces_an_invariant_with_assert() -> None:
    """`python -O` deletes every `assert`, so an invariant enforced by one depends on a flag."""
    # `src/` only, since a test module's asserts are its verdict. Derived from the tree, so a new
    # package or server is scanned the day it appears.
    roots = sorted((ROOT / "packages").glob("*/src")) + sorted((ROOT / "servers").glob("*/src"))
    assert len(roots) > 1, "no source trees found; has the workspace layout changed?"
    offences = _assert_offences(roots)
    assert not offences, (
        "these modules enforce a runtime invariant with `assert`, which `python -O` removes:\n  "
        + "\n  ".join(offences)
        + "\nUse `if not ...: raise` — a ValueError where the caller can act on it, otherwise a "
        "RuntimeError that `connector_app` sanitises."
    )


def test_the_assert_scan_reads_a_tree_and_not_the_text(tmp_path: Path) -> None:
    """The bite test: an `assert` in serving code is flagged, one in prose is not.

    Without it the check above is green over a tree containing no Python at all, and the property
    that makes it AST-based rather than a `grep` is asserted nowhere.
    """
    (tmp_path / "flagged.py").write_text("def f(x: int) -> None:\n    assert x > 0\n", "utf-8")
    (tmp_path / "clean.py").write_text(
        '"""A docstring that says assert, and a string that is one."""\n'
        'NOTE = "assert x > 0"\n'
        "# assert x > 0\n"
        "def f(x: int) -> None:\n"
        "    if x <= 0:\n"
        "        raise ValueError('x must be positive')\n",
        "utf-8",
    )
    offences = _assert_offences([tmp_path])
    assert offences == [f"{(tmp_path / 'flagged.py').as_posix()}:2"], offences


# Refusal echo: a caller-derived string in a raised `ValueError` reaches the model verbatim, so it
# goes through `mcp_server_kit.limits.echo`.
#
# Caller-derived means this vocabulary: `smiles`/`*_smiles` (also `job.smiles`, `result.smiles`),
# `formula`, `solvent`, `name`. A value spelled otherwise, or a message built into a variable
# before the `raise`, is outside this scan.
_ECHO_VOCABULARY = frozenset({"formula", "name", "solvent"})


# Interpolations the scan finds that quote something the caller did not supply, keyed by
# `path::expression` rather than by line so an edit above one does not orphan its argument.
ECHO_IS_NOT_CALLER_DERIVED = {
    # `connector_app`'s own name for the server, written by the server's author in `app.py`.
    "packages/mcp_server_kit/src/mcp_server_kit/app.py::name",
    # The reagent tables' own canonical SMILES, quoted in a duplicate-name error raised while the
    # vendored corpus is indexed — no request is in flight and no caller wrote the string.
    "servers/chem/src/chemclaw_mcp_chem/engine/reagents.py::smiles",
    "servers/safety/src/chemclaw_mcp_safety/engine/reagents.py::smiles",
    # An environment variable's own name, in the import-time refusal `env_bound` raises; the value
    # beside it is the operator's and is already quoted as a number or as `raw!r` of an env value.
    "packages/mcp_server_kit/src/mcp_server_kit/limits.py::name",
    # A parameter's name as the calling code spells it (`name="mass_kg"`), never the caller's text.
    "servers/thermalsafety/src/chemclaw_mcp_thermalsafety/engine/runaway.py::name",
    # Self-tests over the server's own published fixtures, run at readiness with no request.
    "servers/thermalsafety/src/chemclaw_mcp_thermalsafety/engine/selftest.py::formula",
    "servers/unitops/src/chemclaw_mcp_unitops/engine/selftest.py::name",
}


def _echo_subject(node: ast.expr) -> bool:
    """Whether an interpolated expression names caller-derived text by this fleet's vocabulary."""
    if isinstance(node, ast.Name):
        return "smiles" in node.id or node.id in _ECHO_VOCABULARY
    if isinstance(node, ast.Attribute):
        return "smiles" in node.attr
    return False


def _unbounded_echoes(roots: list[Path], relative_to: Path | None = None) -> list[str]:
    """Every `path::expression` a raised f-string interpolates without going through `echo`.

    Only a bare name or attribute is an offence: `echo(smiles)` is a call and passes, and so is
    `len(smiles)` — a length quotes no text.
    """
    offences: list[str] = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text("utf-8"))
            shown = (path.relative_to(relative_to) if relative_to else path).as_posix()
            for raised in ast.walk(tree):
                if not isinstance(raised, ast.Raise) or raised.exc is None:
                    continue
                for node in ast.walk(raised.exc):
                    if isinstance(node, ast.FormattedValue) and _echo_subject(node.value):
                        offences.append(f"{shown}::{ast.unparse(node.value)}")
    return sorted(set(offences))


def test_no_refusal_interpolates_caller_text_past_the_echo_bound() -> None:
    """A refusal quotes what it was given through `mcp_server_kit.limits.echo`, or not at all.

    An accepted structure can still be long enough to flood a refusal message. Read as a tree, so
    `{smiles}` with or without `!r` and multi-line f-strings are the same offence.
    """
    roots = sorted((ROOT / "packages").glob("*/src")) + sorted((ROOT / "servers").glob("*/src"))
    assert len(roots) > 1, "no source trees found; has the workspace layout changed?"
    offences = [
        one for one in _unbounded_echoes(roots, ROOT) if one not in ECHO_IS_NOT_CALLER_DERIVED
    ]
    assert not offences, (
        "these raises quote caller-derived text without `mcp_server_kit.limits.echo`:\n  "
        + "\n  ".join(offences)
        + "\nWrap it as `{echo(value)!r}`, or add it to ECHO_IS_NOT_CALLER_DERIVED with the "
        "argument for why no caller wrote it."
    )


def test_the_argued_echoes_are_still_there() -> None:
    """An exemption that no longer names an interpolation is an argument about nothing."""
    roots = sorted((ROOT / "packages").glob("*/src")) + sorted((ROOT / "servers").glob("*/src"))
    stale = sorted(ECHO_IS_NOT_CALLER_DERIVED - set(_unbounded_echoes(roots, ROOT)))
    assert not stale, f"ECHO_IS_NOT_CALLER_DERIVED names interpolations that are gone: {stale}"


def test_the_echo_scan_reads_a_tree_and_not_the_text(tmp_path: Path) -> None:
    """The bite test: a raw interpolation in a raise is flagged; a bounded or unraised one is not.

    Without it the check above is green over a tree with no raises at all.
    """
    (tmp_path / "flagged.py").write_text(
        "def f(smiles: str, job) -> None:\n"
        "    raise ValueError(\n"
        "        f'bad: {smiles}'\n"
        "        f' and {job.smiles!r}'\n"
        "    )\n",
        "utf-8",
    )
    (tmp_path / "clean.py").write_text(
        "from mcp_server_kit.limits import echo\n"
        "def f(smiles: str) -> str:\n"
        "    if not smiles:\n"
        "        raise ValueError(f'bad: {echo(smiles)!r} of {len(smiles)} characters')\n"
        "    return f'{smiles} is fine'\n",
        "utf-8",
    )
    assert _unbounded_echoes([tmp_path], tmp_path) == [
        "flagged.py::job.smiles",
        "flagged.py::smiles",
    ]
