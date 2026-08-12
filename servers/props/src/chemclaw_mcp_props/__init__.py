"""`props` — solvent and pure-component properties for process R&D, served over MCP.

Three layers, and the import direction is one-way: `engine/` (pure computation over the vendored
table) <- `tools.py` (the MCP surface the agent reads) <- `app.py` (the FastAPI transport).
"""
