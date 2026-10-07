"""Synchronous RDKit work must not run on the event loop that serves other requests.

This server is one uvicorn process on one event loop, and the RDKit calls behind these tools are
synchronous C++: on the loop, one depiction stops every other request. The assertion is that the
blocking call happens on a different thread than the awaiting coroutine (not a wall clock), and
each test fails if its `asyncio.to_thread` hop is removed.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest
from chemclaw_mcp_chem import tools as chem_tools
from chemclaw_mcp_chem.engine.depiction import render_svg
from chemclaw_mcp_chem.engine.reagents import resolve_compound_name


def _thread_recording(target: Any, seen: list[int]) -> Any:
    """Wrap `target` so every call records the thread it ran on, then delegates unchanged."""

    def _spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(threading.get_ident())
        return target(*args, **kwargs)

    return _spy


def test_render_structure_draws_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Depiction — 2D coordinate generation plus SVG rasterisation — is the heaviest call here."""
    seen: list[int] = []
    monkeypatch.setattr(chem_tools, "render_svg", _thread_recording(render_svg, seen))

    async def _run() -> tuple[int, str]:
        return threading.get_ident(), await chem_tools.render_structure("CCO")

    loop_thread, svg = asyncio.run(_run())

    assert "<svg" in svg
    assert seen, "the depiction never ran — the spy was not reached"
    assert seen[0] != loop_thread, (
        "render_svg ran on the event loop thread: the asyncio.to_thread hop is gone, and one "
        "depiction now stops every other request on this process"
    )


def test_resolve_compound_looks_up_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not the dictionary lookup it looks like: an unknown name falls through to RDKit."""
    seen: list[int] = []
    monkeypatch.setattr(
        chem_tools,
        "resolve_compound_name",
        _thread_recording(resolve_compound_name, seen),
    )

    async def _run() -> tuple[int, Any]:
        return threading.get_ident(), await chem_tools.resolve_compound("c1ccccc1")

    loop_thread, resolved = asyncio.run(_run())

    assert resolved.structuredContent["result"] is not None
    assert seen and seen[0] != loop_thread, (
        "resolve_compound_name ran on the event loop thread; its unknown-name path canonicalises "
        "through RDKit, so this is real synchronous work and not a dict lookup"
    )
