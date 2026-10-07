"""The `props` engine: pure computation over the vendored solvent table.

Nothing here imports FastAPI, MCP or a network client (`tests/test_no_egress.py`), so the physics is
testable without a transport.
"""
