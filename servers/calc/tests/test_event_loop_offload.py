"""Synchronous numerical work must not run on the event loop that serves other requests.

**This guard matters more here than on any other server in the fleet.** `chem`'s hop exists because
RDKit's 2D-coordinate generation holds the GIL for tens of milliseconds. The work behind these nine
tools is *seconds to minutes*: a GFN2 SCF is tens of milliseconds, a geometry optimization is dozens
of SCFs, and a finite-difference Hessian is 6N of them — so a single `compute_thermochemistry` on
the event loop would stop every other connected turn on this process for the duration.

The assertion is the property directly — the blocking call happens on a **different thread** than
the coroutine that awaited it — rather than a wall-clock measurement, which would be flaky and would
not distinguish "fast" from "off the loop". Every one of the nine tools is covered, in one
parametrised test, because a per-tool test is a per-tool opportunity to add a tenth tool and forget.

The spies patch the *engine* function each tool calls, which is what makes the test fail if a hop is
removed: without `asyncio.to_thread` the engine runs inline on the loop's own thread and the
recorded identifier matches.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from chemclaw_mcp_calc import tools
from chemclaw_mcp_calc.engine import xtb_props

# (tool name, the module attribute whose call must land off the loop, a zero-argument coroutine).
#
# Two of the nine — `compute_electronic_properties` and `predict_site_reactivity` — build their
# structure and run their SCFs inside a local closure, so the spy goes on the engine module they
# reach through rather than on a name bound in `tools`. That is the honest target: it is the call
# that actually blocks.
CASES: list[tuple[str, Any, str, Callable[[], Awaitable[Any]]]] = [
    ("compute_xtb_energy", tools, "run_xtb", lambda: tools.compute_xtb_energy("CCO")),
    (
        "compute_electronic_properties",
        xtb_props,
        "compute_properties",
        lambda: tools.compute_electronic_properties("CCO"),
    ),
    (
        "predict_site_reactivity",
        xtb_props,
        "compute_fukui",
        lambda: tools.predict_site_reactivity("CCO"),
    ),
    ("optimize_geometry", tools, "optimize_structure", lambda: tools.optimize_geometry("O")),
    (
        "compute_thermochemistry",
        tools,
        "relax_to_minimum",
        lambda: tools.compute_thermochemistry("O", symmetry_number=2),
    ),
    ("predict_pka", tools, "_predict_pka", lambda: tools.predict_pka("CC(=O)O")),
    ("predict_solubility", tools, "_predict_solubility", lambda: tools.predict_solubility("CCO")),
    ("predict_logd", tools, "_predict_logd", lambda: tools.predict_logd("CC(=O)O", ph=1.0)),
    (
        "predict_developability_profile",
        tools,
        "compute_descriptor_profile",
        lambda: tools.predict_developability_profile("CCO"),
    ),
]


def _thread_recording(target: Any, seen: list[int]) -> Any:
    """Wrap `target` so every call records the thread it ran on, then delegates unchanged."""

    def _spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(threading.get_ident())
        return target(*args, **kwargs)

    return _spy


@pytest.mark.parametrize(
    ("name", "module", "attribute", "call"), CASES, ids=[case[0] for case in CASES]
)
def test_the_calculation_runs_off_the_event_loop(
    name: str,
    module: Any,
    attribute: str,
    call: Callable[[], Awaitable[Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One tool, one hop. Removing its `asyncio.to_thread` fails exactly this row."""
    seen: list[int] = []
    monkeypatch.setattr(module, attribute, _thread_recording(getattr(module, attribute), seen))

    async def _run() -> tuple[int, Any]:
        return threading.get_ident(), await call()

    loop_thread, result = asyncio.run(_run())

    assert result is not None
    assert seen, f"{name}: the calculation never ran — the spy was not reached"
    assert seen[0] != loop_thread, (
        f"{name} ran its calculation on the event loop thread: the asyncio.to_thread hop is gone, "
        "and one call now stops every other request on this process for its whole duration"
    )


def test_every_served_tool_is_covered() -> None:
    """A tenth tool added without a row here would ship its blocking call unguarded.

    The list this checks against is the *served* surface rather than a hand-kept list, for the same
    reason `test_calc_version.py` does it: the thing that must not be forgotten is exactly the thing
    a forgetful change adds.
    """
    served = {tool.name for tool in asyncio.run(tools.server.list_tools())}
    assert {case[0] for case in CASES} == served
