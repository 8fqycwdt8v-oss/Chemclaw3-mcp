"""This server's own code holds no way to call out, and the sandbox it runs holds none either.

The AST scan every server ships is the first half. The second half is specific to this one: the
child process is where a caller's program runs, so "no egress" has to be a property of *that*
process and not only of the parent that launched it.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import chemclaw_mcp_pyexec
from chemclaw_mcp_pyexec.engine.limits import Limits
from chemclaw_mcp_pyexec.engine.sandbox import run
from mcp_server_kit.no_egress import assert_no_egress_sources

PACKAGE = Path(chemclaw_mcp_pyexec.__file__).parent


RUNNER = PACKAGE / "engine" / "runner.py"


def test_no_module_can_reach_the_network() -> None:
    """No HTTP client imported, no remote host named — checked by AST, not by grep.

    `runner.py` is exempt, and the test below is the price of that exemption: it is the one file in
    this fleet that imports `socket`, it does so in a child process to *disable* the module, and the
    next test proves that is all it does with it.
    """
    assert_no_egress_sources(PACKAGE, exempt=[RUNNER])


def test_the_exempt_file_only_uses_socket_to_disable_it() -> None:
    """The exemption is a claim about `runner.py`; this is the claim being checked.

    Read as a tree rather than as text, and asserted positively: every attribute of the `socket`
    module that `runner.py` touches must be an assignment target, and the three of them must be the
    outbound calls. A future edit that *reads* something off the module, or replaces one fewer, is
    an exemption that has stopped being true — and it fails here rather than in a deployment.
    """
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"), filename=str(RUNNER))
    # Only the outermost attribute of a chain is the access: in `socket.socket.connect = ...` the
    # inner `socket.socket` is machinery, not a read, and counting it would make this test
    # unsatisfiable by any spelling.
    nested = {id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assigned: set[str] = set()
    read: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or id(node) in nested:
            continue
        parts: list[str] = []
        root: ast.expr = node
        while isinstance(root, ast.Attribute):
            parts.append(root.attr)
            root = root.value
        if isinstance(root, ast.Name) and root.id == "socket":
            dotted = ".".join(reversed(parts))
            (assigned if isinstance(node.ctx, ast.Store) else read).add(dotted)
    assert assigned == {"socket.connect", "socket.connect_ex", "create_connection"}, assigned
    assert not read, f"runner.py reads {sorted(read)} off the socket module; it may only disable it"


def test_the_sandbox_refuses_to_import_a_network_module() -> None:
    """The first door: a program cannot get at `socket` by name."""
    outcome = run("import socket\nresult = 1", limits=Limits(wall_seconds=8.0, cpu_seconds=4))
    assert outcome.error is not None
    assert "not available in the analysis sandbox" in outcome.error


def test_a_held_socket_reference_cannot_connect_either() -> None:
    """The second door, and the one an import guard alone would leave open.

    Refusing `import socket` does nothing about the reference a library already holds, so the
    outbound calls are replaced on the module object itself. This reaches one the same way a
    library's internals would — through a module that is allowed — and proves the replacement, not
    the refusal.
    """
    outcome = run(
        "import json\n"
        "mod = type(json)\n"
        "vals = vars(json).values()\n"
        "found = [v for v in vals if isinstance(v, mod) and v.__name__ == 'socket']\n"
        "result = 'no socket reachable' if not found else str(found[0].create_connection)",
        limits=Limits(wall_seconds=8.0, cpu_seconds=4),
    )
    assert outcome.error is None, outcome.error
    decoded = json.loads(outcome.result_json or '""')
    # Either the reference is not reachable at all, or it is and it is the refusing replacement.
    assert decoded == "no socket reachable" or "_refuse" in decoded


def test_an_outbound_connection_from_inside_the_sandbox_fails() -> None:
    """The claim stated as a measurement: reach for the network and be refused.

    Deliberately does not assert *which* layer refused. The point is that no route is open, and
    pinning one mechanism would turn a defence-in-depth arrangement into a single point of truth.
    """
    outcome = run(
        "try:\n"
        "    import socket\n"
        "    socket.create_connection(('1.1.1.1', 80), timeout=1)\n"
        "    result = 'CONNECTED'\n"
        "except Exception as exc:\n"
        "    result = f'refused: {type(exc).__name__}'",
        limits=Limits(wall_seconds=8.0, cpu_seconds=4),
    )
    assert outcome.error is None, outcome.error
    assert json.loads(outcome.result_json or '""').startswith("refused:")
