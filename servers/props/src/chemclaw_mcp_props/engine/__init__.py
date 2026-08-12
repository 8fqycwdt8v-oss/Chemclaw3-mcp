"""The `props` engine: pure computation over the vendored solvent table.

Nothing in this package imports FastAPI, MCP or a network client, and `tests/test_no_egress.py`
proves it. That is the same line Chemclaw3 draws between `science/` and `connectors/`, for the same
reason: the physics stays testable without a transport, and a transport dependency can never creep
into a correlation.
"""
