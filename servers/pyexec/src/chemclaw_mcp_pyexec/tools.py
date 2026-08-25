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
        "assigned nothing — which is not the same as a computation that produced null.",
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
    a mass balance. It is a calculator with a scientific library on it, not a workspace.

    **How data gets in and out.** Whatever you pass as `data` is bound in the program's namespace as
    a dict called `data`. Whatever the program assigns to `result` is returned. Those are the only
    two channels: there is no filesystem, so nothing can be read from disk or left behind for a
    later call.

    **Available:** numpy, pandas, scipy, rdkit, and the arithmetic, container, text and date parts
    of the standard library. Anything else raises rather than being silently unavailable — the error
    lists what there is.

    **Not available, and do not plan around them:** no network, no files, no `open`, no
    `subprocess`, no `os`, and no state between calls. Each call is a fresh process, destroyed when
    it returns, so a variable set in one call does not exist in the next. Send the whole analysis
    in one program.

    **What it is not evidence of.** This tool runs *your* arithmetic; it is not a data source and it
    knows nothing. A number it returns is only as good as the numbers you put into `data` — cite
    those, not this. It has no access to the knowledge graph, the ELN, or any other tool's results
    except what you copy into `data` yourself.

    **Bounds.** Roughly 20 s of wall clock and 15 s of CPU, which includes importing whatever the
    program imports — pandas costs about 0.4 s and `scipy.stats` about 1.6 s, so a program that
    only needs `math` starts in about 10 ms. Memory is capped, printed output is truncated at
    10,000 characters, and a returned table is cut to 200 rows. Exceeding any of them is reported,
    never silent.

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
