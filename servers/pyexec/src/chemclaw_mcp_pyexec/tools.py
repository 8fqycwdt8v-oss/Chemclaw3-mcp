"""The `pyexec` MCP tool surface: one tool, because the capability is one thing.

**This docstring and the one below it are the prompt.** They are what the agent reads before
deciding whether to reach for this tool and what to send it, so they say what the sandbox offers,
what it refuses, and — the part a tool docstring usually omits and this one must not — what a
program cannot do, so a model does not spend three attempts discovering it.

The tool is `async` and hands the work to a thread. A run is up to `wall_seconds` of another
process's CPU time, and `subprocess.communicate` blocks: leaving it on the event loop would stall
every other MCP session this process is serving. That is the same reasoning the `chem` server
applies to RDKit's coordinate generation, reached from the other direction — there the work is in
this process and here it is in a child, and either way it must not sit on the loop.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from chemclaw_mcp_pyexec.engine import sandbox
from chemclaw_mcp_pyexec.engine.limits import Limits
from chemclaw_mcp_pyexec.engine.runner import ALLOWED_IMPORTS

server = FastMCP("pyexec")

_LIMITS = Limits()


class RunResult(BaseModel):
    """What one analysis produced."""

    ok: bool = Field(
        description="False when the program raised, was killed, or exceeded a limit. When False, "
        "`error` says which — and the answer must not report a number the program did not return."
    )
    stdout: str = Field(
        description="Everything the program printed, standard error included, oldest first."
    )
    result: Any = Field(
        default=None,
        description="Whatever the program assigned to `result`, decoded from JSON. `null` when it "
        "assigned nothing — which is not the same as a computation that produced null. A raw "
        "`bytes`/`bytearray` value anywhere inside it — top-level or nested in a dict or list — "
        'comes back as {"__b64__": "<base64 text>"}; decode with base64.b64decode(...) to get '
        "the bytes back.",
    )
    error: str | None = Field(
        default=None,
        description="The traceback, or the reason the run was stopped. `null` on success.",
    )
    truncated: bool = Field(
        description="True when output or result hit its cap and was cut. What came back is a "
        "prefix, not a summary: re-run printing less rather than reasoning over the remainder."
    )
    source: str = Field(
        description="Provenance. Always this server and its bounds — a number without it is not "
        "something anybody can put in a report."
    )


def _decode(result_json: str | None) -> Any:
    """Decode the child's JSON result, degrading to the raw text if the cap cut it mid-document."""
    if result_json is None:
        return None
    try:
        return json.loads(result_json)
    except json.JSONDecodeError:
        return result_json


@server.tool()
async def run_python(code: str, data: dict[str, Any] | None = None) -> RunResult:
    """Run a short Python analysis in a bounded, offline sandbox, and return what it produced.

    Use it for the arithmetic between tool calls — fitting a curve to points another tool returned,
    aggregating a table, converting units, canonicalising a SMILES, computing a descriptor, checking
    a mass balance, rendering a plot. It is a calculator with a scientific library on it, not a
    persistent workspace.

    **How data gets in and out.** Whatever you pass as `data` is bound in the program's namespace as
    a dict called `data`. Whatever the program assigns to `result` is returned.

    A third channel exists for this one call only: `open(path, mode)` reads and writes files, but
    every path — relative or absolute — is confined to this call's own throwaway scratch directory,
    which is also the program's working directory. `open("plot.png", "wb")` and
    `open("./plot.png", "rb")` both land there; `open("/etc/passwd")` or `open("../elsewhere")` is
    refused with a `SandboxPathError` naming the sandbox, not a crash. **Nothing written there
    survives the call** — the directory and everything in it is destroyed the moment this tool
    returns, so it is scratch space for one analysis, never a place to hand off a file to a later
    call. A binary file you read back — a saved plot, for instance — should be assigned into
    `result`; see the note on `result` below for how bytes cross back out.

    **Available:** numpy, pandas, scipy, matplotlib, sympy, scikit-learn (`import sklearn`), rdkit,
    openbabel (`from openbabel import pybel`), and the arithmetic, container, text and date parts of
    the standard library. Anything else raises rather than being silently unavailable — the error
    lists what there is.

    **Not available, and do not plan around them:** no network, no `subprocess`, no `os`, no
    `pathlib`, and no state between calls — a variable set in one call does not exist in the next,
    and neither does a file written to the scratch directory. Send the whole analysis in one
    program. `open` is real but jailed to this call's own scratch directory (see above); it is not a
    way to reach the rest of the container or to persist anything.

    **What it is not evidence of.** This tool runs *your* arithmetic; it is not a data source and it
    knows nothing. A number it returns is only as good as the numbers you put into `data` — cite
    those, not this. It has no access to the knowledge graph, the ELN, or any other tool's results
    except what you copy into `data` yourself.

    **Bounds.** Roughly 20 s of wall clock and 15 s of CPU, which includes importing whatever the
    program imports — pandas costs about 0.4 s, `scipy.stats` about 1.6 s, and `matplotlib.pyplot`
    about 1.4 s, so a program that only needs `math` starts in about 10 ms. Memory is capped,
    printed output is truncated at 10,000 characters, and a returned table or result is cut to 200
    rows / 20,000 characters — a large plot should be kept small (low DPI, small figure size) or it
    will come back truncated rather than as a valid image. Exceeding any of them is reported, never
    silent.

    Args:
        code: The program. Runs top to bottom in its own namespace, with `data` and `result` already
            bound. Assign to `result` to return a value; `print` for anything you want to read.
        data: JSON-serialisable values to bind as `data`. Omit it and `data` is an empty dict, so a
            program can always subscript it without checking.

    Returns:
        The captured output, the decoded `result`, and whether the program failed or was cut short.
        A failed run carries the traceback in `error` — read it and fix the program rather than
        reporting a number the run did not produce.
    """
    outcome = await asyncio.to_thread(sandbox.run, code, data, _LIMITS)
    return RunResult(
        ok=outcome.error is None,
        stdout=outcome.stdout,
        result=_decode(outcome.result_json),
        error=outcome.error,
        truncated=outcome.truncated,
        source=(
            f"pyexec sandbox: offline child process, {_LIMITS.wall_seconds:g}s wall / "
            f"{_LIMITS.cpu_seconds}s CPU, modules {', '.join(sorted(ALLOWED_IMPORTS))}"
        ),
    )
