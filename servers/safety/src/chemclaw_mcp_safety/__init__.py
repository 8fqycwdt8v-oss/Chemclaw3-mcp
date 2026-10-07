"""`safety` — cited safety and impurity reference tables, served over MCP.

Layers, one-way: `engine/` (RDKit, SMARTS tables, transcribed ICH limits) <- `tools.py` <- `app.py`.

A replacement for Chemclaw3's in-tree `safety` connector: same manifest name, tools, arguments and
docstrings, so exactly one of the two answers (first directory on `CHEMCLAW_CONNECTORS_DIR` wins).
Every answer is advisory and cited, never a clearance or classification, and each result's `verdict`
says so in the payload. See `README.md`.
"""
