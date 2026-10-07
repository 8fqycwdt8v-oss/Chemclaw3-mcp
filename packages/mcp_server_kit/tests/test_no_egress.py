"""The kit's own code holds no way to call out — the scan every server runs, run on the kit.

Three file-level exemptions, each with the test it owes:

- `egress.py` imports `socket` to replace its calls with a refusal; asserted here that `socket`
  is the only forbidden module it names.
- `no_egress.py` names every forbidden module as data.
- `testing.py` imports `httpx` to drive a server in tests; declared under the `testing` extra.

The scan is about what we write: `import mcp_server_kit` pulls in `httpx` through the MCP SDK, so
the runtime guard and the NetworkPolicy are what make an outbound call impossible.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import mcp_server_kit
import pytest
from mcp_server_kit.no_egress import (
    FORBIDDEN_MODULES,
    _dynamic_import_target,
    assert_no_egress_sources,
    computed_imports,
    host_literals,
    network_imports,
)

PACKAGE = Path(mcp_server_kit.__file__).parent
EGRESS = PACKAGE / "egress.py"
NO_EGRESS = PACKAGE / "no_egress.py"
TESTING = PACKAGE / "testing.py"


def test_no_module_can_reach_the_network() -> None:
    """Everything but the three files that name a network module deliberately."""
    assert_no_egress_sources(PACKAGE, exempt=[EGRESS, NO_EGRESS, TESTING])


def test_the_guard_s_only_network_import_is_the_one_it_disables() -> None:
    """`egress.py` earns its exemption by importing `socket` and nothing else."""
    assert network_imports(EGRESS) == ["socket"]


def test_the_scanner_names_forbidden_modules_as_data_and_imports_none() -> None:
    """`no_egress.py` holds the list; holding it must not mean importing from it."""
    assert network_imports(NO_EGRESS) == []


def test_the_private_c_socket_type_is_flagged(tmp_path: Path) -> None:
    """`import _socket` is the runtime guard's blind spot, so the static scan must catch it.

    `egress.arm()` rebinds only the Python `socket.socket` subclass; the C type cannot be patched,
    so this scan is the only in-repo layer that sees it.
    """
    offender = tmp_path / "sneaky.py"
    offender.write_text("import _socket\n", encoding="utf-8")
    assert network_imports(offender) == ["_socket"]
    also = tmp_path / "sneaky_from.py"
    also.write_text("from _socket import socket\n", encoding="utf-8")
    assert network_imports(also) == ["_socket"]


def test_the_helper_s_only_network_import_is_httpx() -> None:
    """`testing.py` earns its exemption by driving a running server, and by nothing else."""
    assert network_imports(TESTING) == ["httpx"]


def test_an_http_client_is_importable_in_every_server_and_that_is_not_the_control() -> None:
    """`import mcp_server_kit` reaches `httpx` through `mcp.shared.session`.

    An assertion so the scan is not mistaken for keeping an HTTP client out of the image; the
    runtime guard and NetworkPolicy are the control. A subprocess, because this process already
    imported `httpx` through pytest plugins.
    """
    probe = subprocess.run(
        [sys.executable, "-c", "import sys, mcp_server_kit; print('httpx' in sys.modules)"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert probe.stdout.strip() == "True", (
        "the MCP SDK no longer imports httpx at module scope; the reasoning in this module's "
        "docstring about what the static scan can and cannot buy needs re-deriving"
    )


def test_the_helper_s_dependencies_are_declared_where_they_are_used() -> None:
    """`httpx` and `pyyaml` are declared where `testing.py` uses them.

    Every server's `test_server.py` imports `mcp_server_kit.testing`, so a per-package run needs
    them. An extra, because a serving image never imports `testing.py`.
    """
    import tomllib

    manifest = tomllib.loads((PACKAGE.parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    extras = manifest["project"]["optional-dependencies"]["testing"]
    declared = {name.split(">")[0].split("[")[0].strip().lower() for name in extras}
    assert {"httpx", "pyyaml"} <= declared, f"the testing extra declares {sorted(declared)}"
    runtime = {
        name.split(">")[0].split("[")[0].strip().lower()
        for name in manifest["project"]["dependencies"]
    }
    assert not ({"httpx", "pyyaml"} & runtime), (
        "a test helper's dependency became a runtime one; it belongs in the `testing` extra"
    )


def test_a_dynamic_import_with_a_literal_name_is_flagged(tmp_path: Path) -> None:
    """`__import__("httpx")` and `importlib.import_module("socket")` are flagged.

    They are `ast.Call` nodes, not `Import`/`ImportFrom`, and must be scanned too.
    """
    builtin = tmp_path / "builtin_import.py"
    builtin.write_text('def go():\n    return __import__("httpx")\n', encoding="utf-8")
    assert network_imports(builtin) == ["httpx"]

    module = tmp_path / "importlib_import.py"
    module.write_text(
        "import importlib\n\n\ndef go():\n    return importlib.import_module('socket')\n",
        encoding="utf-8",
    )
    assert network_imports(module) == ["socket"]

    unqualified = tmp_path / "from_importlib.py"
    unqualified.write_text(
        "from importlib import import_module\n\n\ndef go():\n"
        "    return import_module('urllib.request')\n",
        encoding="utf-8",
    )
    assert network_imports(unqualified) == ["urllib.request"]


def test_a_dynamic_import_of_a_computed_name_must_be_justified_at_its_site(tmp_path: Path) -> None:
    """A name the scan cannot read is an offence until the server argues it, by function.

    `exempt` skips a whole file; a computed import needs a justification naming the one scope it
    sits in, so a clean scan means there are none or each was argued.
    """
    plugins = tmp_path / "plugins.py"
    plugins.write_text(
        "import importlib\n\n\ndef load(name: str) -> object:\n"
        "    return importlib.import_module(name)\n\n\n"
        "class Loader:\n    def by_keyword(self, name: str) -> object:\n"
        "        return __import__(name=name)\n\n\n"
        'LITERAL = importlib.import_module("json")\n',
        encoding="utf-8",
    )
    # `network_imports` still has nothing to say about it — no module can be named for it — and
    # the literal import is neither forbidden nor computed.
    assert network_imports(plugins) == []
    assert computed_imports(plugins) == [("load", 5), ("Loader.by_keyword", 10)]

    with pytest.raises(AssertionError, match=r"named by a value in `load`"):
        assert_no_egress_sources(tmp_path)
    with pytest.raises(AssertionError, match=r"named by a value in `Loader.by_keyword`"):
        assert_no_egress_sources(
            tmp_path, justified_imports={(plugins, "load"): "loads names from a literal map"}
        )
    assert_no_egress_sources(
        tmp_path,
        justified_imports={
            (plugins, "load"): "loads names from a literal map",
            (plugins, "Loader.by_keyword"): "the same map, by keyword",
        },
    )


def test_a_justification_that_outlived_its_computed_import_is_refused(tmp_path: Path) -> None:
    """Held in both directions, because a justification with no site reads as a live argument.

    The same shape as every other allowlist in this repository: the entry has to name something
    that is still there, and it has to actually argue — a blank reason is a key with no claim.
    """
    plugins = tmp_path / "plugins.py"
    plugins.write_text(
        "import importlib\n\n\ndef load(name: str) -> object:\n"
        "    return importlib.import_module(name)\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match=r"`gone` is justified as a computed import"):
        assert_no_egress_sources(
            tmp_path,
            justified_imports={(plugins, "load"): "argued", (plugins, "gone"): "stale"},
        )
    with pytest.raises(AssertionError, match="blank reason"):
        assert_no_egress_sources(tmp_path, justified_imports={(plugins, "load"): "  "})


def test_a_host_split_across_string_literals_is_still_a_host(tmp_path: Path) -> None:
    """`"http://" + "example" + ".com"` is one address written in three pieces.

    Constant concatenation and implicit adjacency are folded through the AST. A name assembled at
    runtime is the runtime guard's business.
    """
    split = tmp_path / "split.py"
    split.write_text('URL = "http://" + "example" + ".com"\n', encoding="utf-8")
    assert host_literals(split) == ["http://example.com"]

    adjacent = tmp_path / "adjacent.py"
    adjacent.write_text('URL = "http://" "weights.example.org/model"\n', encoding="utf-8")
    assert host_literals(adjacent) == ["http://weights.example.org"]

    loopback = tmp_path / "loopback.py"
    loopback.write_text('URL = "http://127." + "0.0.1:8850/healthz"\n', encoding="utf-8")
    assert host_literals(loopback) == []


def test_a_grpc_channel_is_flagged_however_it_is_spelled(tmp_path: Path) -> None:
    """`grpc` is flagged however it is spelled.

    `grpcio` opens its sockets from C, outside the runtime guard, so this scan is the only in-repo
    layer that can see it arrive.
    """
    plain = tmp_path / "channel.py"
    plain.write_text("import grpc\n", encoding="utf-8")
    assert network_imports(plain) == ["grpc"]

    asynchronous = tmp_path / "aio.py"
    asynchronous.write_text("from grpc.aio import insecure_channel\n", encoding="utf-8")
    assert network_imports(asynchronous) == ["grpc.aio"]

    dynamic = tmp_path / "dynamic.py"
    dynamic.write_text('import importlib\nimportlib.import_module("grpc")\n', encoding="utf-8")
    assert network_imports(dynamic) == ["grpc"]

    # A name split across literals is one name. `_constant_string` was already in this module for
    # `host_literals`, so the fix was to call it — and until it was called, `"gr" + "pc"` was a
    # dynamic import of nothing at all.
    folded = tmp_path / "folded.py"
    folded.write_text('import importlib\nimportlib.import_module("gr" + "pc")\n', encoding="utf-8")
    assert network_imports(folded) == ["grpc"]


def test_ctypes_is_outside_both_in_repo_layers_and_has_exactly_one_caller() -> None:
    """`ctypes` stays off the forbidden list, and its importers stay at the one argued file.

    `ctypes` can reach libc's `connect` past the guard, but the one first-party importer is
    `servers/pyexec/.../engine/sandbox.py` (`prctl(PR_SET_DUMPABLE, 0)`), and exempting that file
    would be wider than this check. A second importer fails here, which is the moment to decide.

    Compared on the root package so `ctypes.util` and `importlib.import_module("ctypes")` count too.
    Only first-party `src/` roots are read; tests may import it.
    """
    assert "ctypes" not in FORBIDDEN_MODULES

    workspace = PACKAGE.parents[2].parent
    roots = sorted(workspace.glob("packages/*/src")) + sorted(workspace.glob("servers/*/src"))
    assert roots, f"no first-party source roots under {workspace}; has the layout changed?"
    importers = {
        str(source.relative_to(workspace))
        for root in roots
        for source in root.rglob("*.py")
        if _imports_root(ast.parse(source.read_text(encoding="utf-8")), "ctypes")
    }
    assert importers == {"servers/pyexec/src/chemclaw_mcp_pyexec/engine/sandbox.py"}, (
        f"{sorted(importers)!r} import ctypes; the module is off FORBIDDEN_MODULES because exactly "
        "one file needed it for `prctl`, and a second caller has to argue that again"
    )

    # The three spellings that used to walk past this check, and the one that never did.
    for source in (
        "import ctypes.util",
        "from ctypes.util import find_library",
        'import importlib\nimportlib.import_module("ctypes")',
        "import ctypes",
    ):
        assert _imports_root(ast.parse(source), "ctypes"), (
            f"{source!r} reads as not importing ctypes"
        )
    assert not _imports_root(ast.parse("import ctypeslike"), "ctypes"), (
        "a module whose name merely starts with the root is not that root"
    )


def _imports_root(tree: ast.Module, root: str) -> bool:
    """Whether `tree` imports `root` or anything under it, however the import is spelled."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[0] == root for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == root:
                return True
        elif isinstance(node, ast.Call):
            target = _dynamic_import_target(node)
            if target is not None and target.split(".")[0] == root:
                return True
    return False
