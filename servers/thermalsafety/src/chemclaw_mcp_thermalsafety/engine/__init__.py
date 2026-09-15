"""Pure computation: no FastAPI, no MCP, no network, no corpus.

The import direction is one-way — `engine/` knows nothing about `tools.py` or `app.py` — which is
what keeps the arithmetic testable with no transport installed. `tests/test_runaway.py`,
`test_semenov.py` and `test_oxygen_balance.py` import no transport at all.
"""
