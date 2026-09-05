"""The kit's own code holds no way to call out — the scan every server runs, run on the kit.

**This package was the one place the static half of the rule did not look**, and it is the package
installed into every server image. The exemption was documented and its stated reason is real —
`egress.py` must import `socket` to patch it — but it was granted to the *package* rather than to
the file, which is the shape `no_egress.py`'s own docstring rejects for a server: "a server may name
files whose network import is the disabling one, and owes a test proving that is all it is."

So: three exemptions, each with the test it owes.

- `egress.py` imports `socket` in order to replace `connect`, `sendto`, `getaddrinfo` and their
  siblings with a refusal. Proven by `test_egress.py` in full; asserted here as *shape* — that the
  only forbidden module it names is `socket`.
- `no_egress.py` names every forbidden module as data: it is the list being scanned for.
- `testing.py` imports `httpx` to drive a running server over ASGI in a *test*. That one is the
  finding worth carrying: `httpx` and `pyyaml` were not dependencies of this package at all, and
  the workspace only worked because they are root dev dependencies — so a server installed from its
  own wheel could not run its `test_server.py`. They are declared now, under a `testing` extra —
  which is where the requirement is, since a serving image never imports `testing.py`.

**And one thing this scan cannot buy, measured rather than assumed.** "No HTTP client in the
serving process" is not achievable and never was: `import mcp_server_kit` pulls in `httpx` through
`mcp.shared.session`, which is a *runtime* dependency of every server here. So the static scan's
value is about what **we** write, exactly as `no_egress.py` says — what makes an outbound call
impossible is `egress.py`'s runtime guard and the NetworkPolicy, neither of which cares which
clients are importable. Asserted below so the stronger claim cannot be reintroduced as an
assumption.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import mcp_server_kit
from mcp_server_kit.no_egress import assert_no_egress_sources, host_literals, network_imports

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

    `socket.socket` subclasses `_socket.socket`; `egress.arm()` rebinds only the Python subclass,
    so `_socket.socket().connect(...)` reaches the network with the guard armed (measured: a real
    TCP connection completed). The C type cannot be monkeypatched, which makes this scan the only
    in-repo layer that can see the import — so it must.
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
    """Measured: `import mcp_server_kit` reaches `httpx` through `mcp.shared.session`.

    Written as an assertion rather than a comment because the tempting conclusion from the scan
    above is the wrong one — that keeping `httpx` out of this package's runtime dependencies keeps
    an HTTP client out of the image. It does not: the MCP SDK is a runtime dependency of every
    server and imports one at module scope. What makes an outbound call impossible is `egress.py`,
    armed on this very import, and the NetworkPolicy behind it. If a future SDK stops importing
    `httpx`, this test goes red and the paragraph it defends gets rewritten deliberately.

    A subprocess rather than an in-process check, because this module has already imported `httpx`
    through pytest's own plugins — `sys.modules` here would describe the test runner.
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
    """`httpx` and `pyyaml` are `testing.py`'s, and were declared by nobody.

    The whole workspace resolved anyway because both are root *dev* dependencies, so nothing was
    red — and `uv run --package chemclaw-mcp-props pytest` could not work, because every server's
    `test_server.py` imports `mcp_server_kit.testing`. An extra rather than a runtime dependency,
    because that is where the requirement actually is: a serving image never imports `testing.py`.
    (It is not a claim about what the image *contains* — see the test above.)
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
    """`__import__("httpx")` is an `ast.Call`, and the scan walked only `Import`/`ImportFrom`.

    This module's own docstring named `__import__("urllib")` as one of the three spellings AST
    catches and text does not — while `network_imports` could not see it at all. Measured before
    the fix: `assert_no_egress_sources` returned **clean** on a file whose whole body was
    `h = __import__("httpx"); h.get(...)`, and on `importlib.import_module("socket")`. `socket` is
    on the forbidden list precisely because "a module that imports it can also un-patch the
    guard", and that is the spelling that reaches it.
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


def test_a_dynamic_import_of_a_computed_name_is_deliberately_not_flagged(tmp_path: Path) -> None:
    """A name the scan cannot read is not an offence here, and the reason is a real caller.

    `servers/rxnpredict/engine/predictors/__init__.py` loads its optional predictor plug-ins with
    `importlib.import_module(modname)` over a discovered list — a legitimate dynamic import whose
    argument no static reader can evaluate. Flagging the *shape* would make that server's own
    no-egress test fail on correct code, and an exemption granted to work around a false positive
    is how a scan stops being read.

    So the boundary is stated rather than assumed: what a computed import loads is the runtime
    guard's job and `make offline-run`'s, exactly as it is for a child process or a `ctypes` call.
    """
    dynamic = tmp_path / "plugins.py"
    dynamic.write_text(
        "import importlib\n\n\ndef load(name: str) -> object:\n"
        "    return importlib.import_module(name)\n",
        encoding="utf-8",
    )
    assert network_imports(dynamic) == []


def test_a_host_split_across_string_literals_is_still_a_host(tmp_path: Path) -> None:
    """`"http://" + "example" + ".com"` is one address written in three pieces.

    `host_literals` is a regex over the file's text, so a concatenation — and Python's implicit
    adjacency, which is the same thing without the operator — read as three harmless fragments.
    The literals are folded through the AST now, which covers exactly the constant case: a name
    assembled at runtime is not visible to any static reader, and is the runtime guard's business.
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
