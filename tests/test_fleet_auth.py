"""Authentication: every server proves its bearer check and its manifest against a running listener
from a collected test, and published dev tokens stay out of the logs.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


SERVERS = ROOT / "servers"


def server_dirs() -> list[Path]:
    """Every server directory — a subdirectory of `servers/` holding a `connector.yaml`."""
    return sorted(path for path in SERVERS.iterdir() if (path / "connector.yaml").is_file())


def test_every_published_dev_token_default_is_in_the_redaction_exemption() -> None:
    """`_PUBLISHED_VALUES` is a literal, so something has to hold it against what is published.

    `mcp_server_kit.logging` refuses to redact the credentials this repository commits, because a
    value anybody can read is not a secret and scrubbing it only corrupts logs. The set that says
    which ones those are is written out by hand; the `Makefile` is where they are actually
    published. A default added there and not here is silently redacted out of every `make run-*`
    log, and a value left here after the Makefile stops using it is a real credential this fleet
    has quietly exempted — so both directions are the same check, run against the file rather than
    against a memory of it.
    """
    from mcp_server_kit.logging import _PUBLISHED_VALUES

    makefile = (Path(__file__).resolve().parents[1] / "Makefile").read_text()
    defaults = set(re.findall(r"\$\$\{CHEMCLAW_[A-Z_]+_TOKEN:-([^}]+)\}", makefile))
    assert defaults, "no `CHEMCLAW_*_TOKEN` default found in the Makefile; has the pattern changed?"
    unexempted = defaults - _PUBLISHED_VALUES
    assert not unexempted, (
        f"the Makefile publishes {sorted(unexempted)!r} as a token default and "
        "mcp_server_kit.logging does not exempt it, so every local log line mentioning it is "
        "rewritten to ***"
    )
    stale = _PUBLISHED_VALUES - defaults
    assert not stale, (
        f"{sorted(stale)!r} is exempted from redaction and is no longer a "
        "published default; an exemption that outlives its reason is a credential this fleet has "
        "decided not to hide"
    )


def _called_name(node: ast.Call) -> str:
    """The bare name a call names, whatever it hangs off — `os.getenv` and `getenv` read the
    same."""
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else ""


# Marks that make a `test_*` body no evidence about anything: it may not run, or it may run and be
# allowed to fail. `xfail` is here for the second reason and is the least obvious — an xfailing test
# is *collected*, executes, and reports success for the run whatever it asserts.
_INERT_MARKS = frozenset({"skip", "skipif", "xfail"})


def _is_inert(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether a decorator makes this test's body no proof — a skip, a skipif or an xfail.

    Matched on the mark's bare name, so `@pytest.mark.skip`, `@mark.skipif(...)` and a bare
    `@skip` all read the same, and a parametrisation carrying `pytest.param(..., marks=...)` is
    deliberately *not* matched: that suppresses one case of a test that still runs for the others,
    where these three suppress the whole function.
    """
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
        if name in _INERT_MARKS:
            return True
    return False


def _calls_from_collected_tests(module: Path) -> set[str]:
    """Every function name called from inside a `test_*` body that actually proves something."""
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    return {
        _called_name(call)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name.startswith("test_")
        and not _is_inert(node)
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
    }


def test_a_suppressed_test_is_not_a_proof(tmp_path: Path) -> None:
    """The bite test for `_calls_from_collected_tests`: a marked-out body counts for nothing.

    Written on a synthetic module rather than on a real server's, because no server here ships a
    suppressed test — which is exactly why the gap was invisible, and why asserting it against the
    tree would assert nothing. Four shapes in one file: the collected test is the only one whose
    call may be seen.
    """
    module = tmp_path / "test_sample.py"
    module.write_text(
        "import pytest\n"
        "def helper() -> None:\n"
        "    uncollected_call()\n"
        "def test_collected() -> None:\n"
        "    real_call()\n"
        "@pytest.mark.skip(reason='x')\n"
        "def test_skipped() -> None:\n"
        "    skipped_call()\n"
        "@pytest.mark.skipif(True, reason='x')\n"
        "def test_conditionally_skipped() -> None:\n"
        "    skipif_call()\n"
        "@pytest.mark.xfail\n"
        "def test_expected_to_fail() -> None:\n"
        "    xfail_call()\n",
        encoding="utf-8",
    )
    found = _calls_from_collected_tests(module)
    assert "real_call" in found
    for suppressed in ("uncollected_call", "skipped_call", "skipif_call", "xfail_call"):
        assert suppressed not in found, f"{suppressed} was read out of a body that proves nothing"


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_every_server_proves_its_bearer_check_against_a_running_server(server: Path) -> None:
    """`assert_bearer_is_enforced` is called from every server's own `test_server.py`."""
    tests = server / "tests" / "test_server.py"
    assert tests.is_file(), f"{server.name} has no tests/test_server.py"
    assert "assert_bearer_is_enforced" in _calls_from_collected_tests(tests), (
        f"{tests.relative_to(ROOT)} never calls assert_bearer_is_enforced from a collected test, "
        "so nothing drives this server's bearer check against a running listener. The helper is "
        "in `mcp_server_kit.testing`; see any other server's test_server.py."
    )


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_every_server_proves_its_manifest_against_a_running_server(server: Path) -> None:
    """`assert_manifest_matches` is called from every server's own `test_server.py`, too."""
    tests = server / "tests" / "test_server.py"
    assert tests.is_file(), f"{server.name} has no tests/test_server.py"
    assert "assert_manifest_matches" in _calls_from_collected_tests(tests), (
        f"{tests.relative_to(ROOT)} never calls assert_manifest_matches from a collected test, so "
        "nothing compares this server's manifest with what it serves. An undeclared tool is served "
        "while looking deleted, and an unclassified one fails open at Chemclaw3's plan gate."
    )
