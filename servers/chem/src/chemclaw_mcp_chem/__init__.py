"""`chem` — bench chemistry over RDKit, served over MCP.

Three layers, and the import direction is one-way: `engine/` (RDKit and the vendored reagent
table) <- `tools.py` (the MCP surface the agent reads) <- `app.py` (the FastAPI transport).

Ported from Chemclaw3's own in-tree `chem` connector. It is a **replacement for** that bundle
rather than a second implementation of it: the manifest here carries the same name, the same four
tools and the same argument names, and Chemclaw3 resolves a bundle-name collision by first
directory on `CHEMCLAW_CONNECTORS_DIR` (`connectors/registry.py::_bundle_dirs`), so putting this
fleet's `manifests/` ahead of Chemclaw3's own directory is what makes exactly one of the two
answer. See `README.md`.
"""
