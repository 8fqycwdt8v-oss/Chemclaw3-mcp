"""Serving-code discipline: no `assert` as a control, and no refusal echoes caller text past the
echo bound.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


# The modules whose *product* is an assertion failure. Both are imported by tests and by nothing
# else — `testing.assert_manifest_matches`, `testing.assert_bearer_is_enforced` and
# `no_egress.assert_no_egress_sources` exist to fail a test — so an `assert` there is the verdict
# rather than a control. Everything else under `src/` is serving code, where an `assert` is a
# control `python -O` deletes.
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
    # `src/` only: a test module's asserts are its verdict, which is the same exemption
    # `ASSERT_IS_THE_PRODUCT` grants the two helpers that live under `src/` because they are
    # imported *by* tests. Derived from the tree rather than listed, so a new package or server is
    # scanned the day it appears.
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


# Refusal echo. A caller-derived string interpolated into a raised exception reaches the model
# verbatim (a `ValueError` passes `connector_app` unchanged) and so is bounded by
# `mcp_server_kit.limits.echo`, the one config-driven bound — see that function's docstring and
# `D-2026-09-26-one-echo-bound-and-the-refusals-that-bypassed-it`.
#
# **What counts as caller-derived is a vocabulary, and the vocabulary is the rule's reach.** The
# fleet's refusals name what they quote after what it is: a structure is `smiles` or `*_smiles` (or
# `job.smiles`, `result.smiles`), a formula `formula`, a solvent `solvent`, a looked-up name `name`.
# A value spelled any other way walks past this scan, and so does a message built into a variable
# before the `raise` — both are written down here rather than implied.
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

    **Four servers carried their own copy of the bound and most refusals bypassed all four.**
    `chem`, `calc`, `safety` and `rxnpredict` each declared a 120-character `_MAX_ECHO_CHARS`, and
    `grep -rnE '\\{[a-z_]*\\.?smiles[^}]*!r\\}'` over the serving trees still found fifteen raises
    interpolating the structure raw: measured, `predict_pka` on `"C" * 1500` — an ordinary accepted
    call, inside both structural bounds — raised a 1,587-character refusal. Read as a tree rather
    than grepped, so `{smiles}` without `!r` and a multi-line f-string are the same offence.
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
