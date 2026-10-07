"""No egress: the lint ban and the static scan agree, the gate runs the offline lane, and a blind
handler that answers anyway classifies what it caught.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_lint_ban_and_the_static_scan_name_the_same_modules() -> None:
    """Two declarations of one rule, with this as the thing that reconciles them."""
    import tomllib

    from mcp_server_kit.no_egress import FORBIDDEN_MODULES

    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    banned = set(
        config["tool"]["ruff"]["lint"]["flake8-tidy-imports"]["banned-module-level-imports"]
    )
    assert banned == set(FORBIDDEN_MODULES), (
        "the ruff ban and the static scan name different modules — banned but unscanned: "
        f"{sorted(banned - set(FORBIDDEN_MODULES))}; scanned but unbanned: "
        f"{sorted(set(FORBIDDEN_MODULES) - banned)}. One rule, two declarations, and this test is "
        "the only thing that makes the second one honest."
    )


def test_the_lint_ban_names_its_exemptions_and_they_are_the_scan_s_own_boundary() -> None:
    """A second belt is only worth having while its exemption list stays readable."""
    import tomllib

    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    ignores = config["tool"]["ruff"]["lint"]["per-file-ignores"]
    exempted = {where for where, codes in ignores.items() if "TID253" in codes}
    assert exempted == {"**/tests/**", "scripts/*.py"}, (
        f"the TID253 exemptions are {sorted(exempted)}; a third one is a decision about where the "
        "no-egress boundary is, not a lint adjustment"
    )
    kit = ROOT / "packages" / "mcp_server_kit" / "src" / "mcp_server_kit"
    suppressed = sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "packages").rglob("src/**/*.py")
        if "# noqa: TID253" in path.read_text(encoding="utf-8")
    ) + sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "servers").rglob("src/**/*.py")
        if "# noqa: TID253" in path.read_text(encoding="utf-8")
    )
    assert suppressed == [
        (kit / "egress.py").relative_to(ROOT).as_posix(),
        (kit / "testing.py").relative_to(ROOT).as_posix(),
    ], (
        f"a module-level network import is suppressed in {suppressed}; two are argued, "
        "a third is not"
    )


# The blind handlers that answer without classifying, each argued here because ruff cannot be made
# to ask for the argument at the site: see `test_every_blind_handler_that_answers_anyway_is_argued`.
#
# `auth.py`'s body-cap guard discards the downstream app's exception **only when this middleware has
# already refused the request** — the app raised because the guard cut its receive channel, which is
# this code's own doing rather than a component going missing. A `record()` there would publish a
# degradation every time a caller sent an oversized body.
BLIND_ANSWER_IS_ARGUED = {
    "packages/mcp_server_kit/src/mcp_server_kit/auth.py:432",
}


_BLIND = {"Exception", "BaseException"}


def _blind_handlers_that_answer(roots: list[Path]) -> list[str]:
    """Every blind `except` under `roots` that can return without re-raising and says nothing."""
    offences: list[str] = []
    for root in roots:
        for source in sorted(root.rglob("*.py")):
            relative = source.relative_to(ROOT).as_posix()
            text = source.read_text(encoding="utf-8")
            lines = text.splitlines()
            for node in ast.walk(ast.parse(text, filename=str(source))):
                if not isinstance(node, ast.ExceptHandler):
                    continue
                caught = (
                    node.type.elts
                    if isinstance(node.type, ast.Tuple)
                    else ([] if node.type is None else [node.type])
                )
                blind = node.type is None or any(
                    isinstance(one, ast.Name) and one.id in _BLIND for one in caught
                )
                if not blind or isinstance(node.body[-1], ast.Raise):
                    continue
                if "BLE001" in lines[node.lineno - 1]:
                    continue
                if any(
                    isinstance(call.func, ast.Attribute | ast.Name)
                    and (call.func.attr if isinstance(call.func, ast.Attribute) else call.func.id)
                    in {"classify", "record"}
                    for call in ast.walk(node)
                    if isinstance(call, ast.Call)
                ):
                    continue
                offences.append(f"{relative}:{node.lineno}")
    return sorted(offences)


def test_every_blind_handler_that_answers_anyway_is_argued() -> None:
    """`BLE001` does not fire on the two shapes this fleet's own handlers are written in."""
    roots = sorted((ROOT / "packages").glob("*/src")) + sorted((ROOT / "servers").glob("*/src"))
    assert len(roots) > 1, "no source trees found; has the workspace layout changed?"
    offences = [
        one for one in _blind_handlers_that_answer(roots) if one not in BLIND_ANSWER_IS_ARGUED
    ]
    assert not offences, (
        "these blind handlers answer without re-raising and neither classify through "
        "`mcp_server_kit.degradation` nor carry a `# noqa: BLE001` reason:\n  "
        + "\n  ".join(offences)
        + "\nClassify it, or add it to BLIND_ANSWER_IS_ARGUED with the argument for why the "
        "answer it returns is whole."
    )


def test_the_argued_blind_handlers_are_still_there() -> None:
    """An allowlist entry that no longer names a handler is an argument about nothing.

    The same two-directions rule the rest of this file is built on: without it, moving `auth.py`'s
    body-cap guard would leave a line here asserting a property of a handler that had gone, and the
    next one added in its place would inherit the exemption.
    """
    roots = sorted((ROOT / "packages").glob("*/src")) + sorted((ROOT / "servers").glob("*/src"))
    found = set(_blind_handlers_that_answer(roots))
    stale = sorted(BLIND_ANSWER_IS_ARGUED - found)
    assert not stale, (
        f"BLIND_ANSWER_IS_ARGUED names handlers that are no longer there: {stale}. Delete the "
        "entry and its argument in the commit that moved them."
    )


def test_the_gate_runs_the_only_layer_that_covers_two_of_the_four_egress_channels() -> None:
    """`make check` has to reach `offline-run`, because nothing else reaches those two channels."""
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    prerequisites = re.search(r"^check:([^#\n]*)", makefile, re.MULTILINE)
    assert prerequisites, "no `check:` target in the Makefile; has the gate been renamed?"
    wired = prerequisites.group(1).split()
    assert "offline-guarded" in wired, (
        f"`make check` no longer reaches the offline lane (prerequisites: {wired}). A child "
        "process and a `ctypes` call into libc are covered by nothing else, so the gate would go "
        "green with two of the four egress channels unverified"
    )
    assert re.search(r"^offline-guarded:", makefile, re.MULTILINE), (
        "`check` names `offline-guarded` and the Makefile does not define it"
    )
    guard = makefile.split("offline-guarded:", 1)[1].split("\n.PHONY", 1)[0]
    assert "unshare" in guard and "offline-run" in guard, (
        "the guarded target no longer tries `offline-run` behind an `unshare` probe"
    )
    assert "SKIPPED offline-run" in guard, (
        "the guarded target no longer names the lane it did not run; silently omitting a layer is "
        "the defect it exists to prevent, and it reads exactly like having run it"
    )
