"""`safety` — cited safety and impurity reference tables, served over MCP.

Three layers, and the import direction is one-way: `engine/` (RDKit, the SMARTS tables and the
transcribed ICH limits) <- `tools.py` (the MCP surface the agent reads) <- `app.py` (the FastAPI
transport).

Ported from Chemclaw3's own in-tree `safety` connector. It is a **replacement for** that bundle
rather than a second implementation of it: the manifest here carries the same name, the same three
tools, the same argument names and the same model-facing docstrings, and Chemclaw3 resolves a
bundle-name collision by first directory on `CHEMCLAW_CONNECTORS_DIR`
(`connectors/registry.py::_bundle_dirs`), so putting this fleet's `manifests/` ahead of Chemclaw3's
own directory is what makes exactly one of the two answer. See `README.md`.

Every answer here is advisory and carries its citation. None is a clearance, a classification or a
risk assessment, and the tool docstrings plus every result's `verdict` say so in the payload rather
than only in prose — because the payload is what is in the context window when the answer is
written.
"""
