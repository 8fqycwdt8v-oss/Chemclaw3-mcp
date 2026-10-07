"""SMARTS matching must not run on the event loop that serves every other request.

Pair rules are a cross-product, so work grows with the square of a small request, and RDKit
matching is synchronous C++: even a bounded screen (`MAX_COMPONENTS`) would stall every other
request. Asserted directly, by the blocking call running on a different thread than the awaiting
coroutine, not by timing. `ich_impurity_limit` is a dictionary lookup with no hop and is not
tested here.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest
from chemclaw_mcp_safety import tools as safety_tools
from chemclaw_mcp_safety.engine import genotox, screen


def _thread_recording(target: Any, seen: list[int]) -> Any:
    """Wrap `target` so every call records the thread it ran on, then delegates unchanged."""

    def _spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(threading.get_ident())
        return target(*args, **kwargs)

    return _spy


def test_a_single_structure_screen_runs_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one-molecule path takes its own hop — it is a different call from the reaction path."""
    seen: list[int] = []
    monkeypatch.setattr(
        safety_tools, "screen_structure", _thread_recording(screen.screen_structure, seen)
    )

    async def _run() -> tuple[int, Any]:
        return threading.get_ident(), await safety_tools.screen_hazards(["CCCN=[N+]=[N-]"])

    loop_thread, result = asyncio.run(_run())

    assert [flag.rule_id for flag in result.flags] == ["organic-azide"]
    assert seen, "the screen never ran — the spy was not reached"
    assert seen[0] != loop_thread, (
        "screen_structure ran on the event loop thread: the asyncio.to_thread hop is gone, and one "
        "screen now stops every other request on this process"
    )


def test_a_reaction_screen_runs_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reaction screen, whose cost is quadratic in a caller-supplied list, runs off the loop."""
    seen: list[int] = []
    monkeypatch.setattr(
        safety_tools, "screen_reaction", _thread_recording(screen.screen_reaction, seen)
    )

    async def _run() -> tuple[int, Any]:
        return threading.get_ident(), await safety_tools.screen_hazards(
            ["[K+].[O-][Mn](=O)(=O)=O", "[Li+].[AlH4-]"]
        )

    loop_thread, result = asyncio.run(_run())

    assert [flag.rule_id for flag in result.flags] == ["oxidizer-with-reductant"]
    assert seen and seen[0] != loop_thread, (
        "screen_reaction ran on the event loop thread; its pair rules are a cross-product over the "
        "caller's list, so this is the call that was measured blocking a loop for 2.48 s"
    )


def test_the_genotoxicity_screen_runs_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sibling screen has the identical shape, and a hop in one place is not a hop in both."""
    seen: list[int] = []
    monkeypatch.setattr(
        genotox,
        "screen_genotoxic_alerts",
        _thread_recording(genotox.screen_genotoxic_alerts, seen),
    )

    async def _run() -> tuple[int, Any]:
        return threading.get_ident(), await safety_tools.screen_genotoxic_alerts(["CN(C)N=O"])

    loop_thread, result = asyncio.run(_run())

    assert [alert.alert_id for alert in result.alerts] == ["n-nitroso"]
    assert seen and seen[0] != loop_thread, (
        "screen_genotoxic_alerts ran on the event loop thread; it was measured at 640 components "
        "producing 102,400 alerts in 933 ms"
    )
