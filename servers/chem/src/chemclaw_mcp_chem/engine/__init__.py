"""The `chem` engine: RDKit and the vendored reagent table, with no transport.

Nothing here imports FastAPI, MCP or a network client (`tests/test_no_egress.py`), so the chemistry
is testable without a transport. Import direction: `chem` <- `reagents` <- `stoichiometry`, with
`depiction` on `chem`.
"""
