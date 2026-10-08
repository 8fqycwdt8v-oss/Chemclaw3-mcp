"""`safety` — cited safety and impurity reference tables, served over MCP.

Layers, one-way: `engine/` (RDKit, SMARTS tables, transcribed ICH limits) <- `tools.py` <- `app.py`.

Chemclaw3 installs this server's manifest with `chemclaw-contracts` and runs none of its code.
Every answer is advisory and cited, never a clearance or classification, and each result's `verdict`
says so in the payload. See `README.md`.
"""
