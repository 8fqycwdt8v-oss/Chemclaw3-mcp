"""What `chem`'s published tool surface costs every model call that binds it, held as a ratchet.

Chemclaw3 sends every bound tool's name, description and argument schema on every model call, and
bounds the three bundles this fleet serves it (`chem`, `rxnpredict`, `safety`) with
`SERVED_ELSEWHERE_ALLOWANCE` in its `tests/test_context_floor.py`. Both of its compaction defaults
are derived from that allowance, so growth here is paid out of every chemist's thread there.

That repository measures it in its own CI, against this repository's `main` — so a breach used to
be heard there only after the merge that caused it (Chemclaw3-mcp#152: `chem` grew 2,027 tokens of
description across seven tools and nothing here went red). This is the same quantity measured on
this side, before the merge: the characters of each tool's `{name, description, inputSchema}`,
which is within a few percent of Chemclaw3's chars/4 estimate of the converted schema.
"""

from __future__ import annotations

import asyncio
import json

from chemclaw_mcp_chem import tools

#: The ratchet, in characters of the published surface. 19,455 after #152 narrowed the
#: descriptions to their rules; this is ~8% over that. Raising it is a decision about every
#: Chemclaw3 request, not a bump: say in the pull request what the new prose buys, and check it
#: against `SERVED_ELSEWHERE_ALLOWANCE` there.
PUBLISHED_SURFACE_MAX_CHARS = 21_000


def _published_chars() -> int:
    """Characters of every tool as `tools/list` publishes it."""
    listed = asyncio.run(tools.server.list_tools())
    entries = [
        {"name": t.name, "description": t.description, "inputSchema": t.inputSchema} for t in listed
    ]
    return sum(len(json.dumps(entry)) for entry in entries)


def test_the_published_surface_stays_inside_its_ratchet() -> None:
    """Growth in a description is a cost on every Chemclaw3 request, so it is red here first."""
    published = _published_chars()
    assert published <= PUBLISHED_SURFACE_MAX_CHARS, (
        f"chem's published tool surface is {published} characters (~{published // 4} tokens) "
        f"against a ratchet of {PUBLISHED_SURFACE_MAX_CHARS}. Chemclaw3 pays this on every model "
        "call: state the rule rather than the measurement behind it, or raise the ratchet and say "
        "what the prose buys."
    )
