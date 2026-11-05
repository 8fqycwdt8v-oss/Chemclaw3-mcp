"""A query against the subset file must not run on the event loop that serves every other request.

Asserted directly, by the blocking call running on a different thread than the awaiting coroutine,
not by timing.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest
from chemclaw_mcp_chembl import tools
from chemclaw_mcp_chembl.engine import bioactivity, lookup


def _recording(target: Any, seen: list[int]) -> Any:
    def _spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(threading.get_ident())
        return target(*args, **kwargs)

    return _spy


@pytest.mark.parametrize(
    ("tool", "attribute", "module", "arguments"),
    [
        ("chembl_lookup_compound", "lookup", lookup, {"identifier": "CHEMBL25"}),
        (
            "chembl_bioactivity_summary",
            "bioactivity_summary",
            bioactivity,
            {"identifier": "CHEMBL25"},
        ),
    ],
)
def test_a_tool_runs_its_query_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    attribute: str,
    module: Any,
    arguments: dict[str, Any],
) -> None:
    seen: list[int] = []
    monkeypatch.setattr(tools, attribute, _recording(getattr(module, attribute), seen))

    async def _run() -> int:
        await getattr(tools, tool)(**arguments)
        return threading.get_ident()

    loop_thread = asyncio.run(_run())
    assert seen, "the query never ran — the spy was not reached"
    assert seen[0] != loop_thread, (
        f"{tool} queried the subset on the event loop thread: the asyncio.to_thread hop is gone, "
        "and "
        "one slow read now stops every other request on this process"
    )
