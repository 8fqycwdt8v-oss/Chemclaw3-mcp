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
from mcp_server_kit.no_egress import assert_no_egress_sources, network_imports

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
