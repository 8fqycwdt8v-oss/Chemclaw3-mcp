"""The `chem` engine: RDKit and the vendored reagent table, with no transport anywhere in sight.

Nothing in this package imports FastAPI, MCP or a network client, and `tests/test_no_egress.py`
proves it. That is the same line Chemclaw3 draws between `science/` and `connectors/`, for the same
reason: the chemistry stays testable without a transport, and a transport dependency can never
creep into a structure-handling helper.

Four modules, and the import direction inside is one-way too — `chem` <- `reagents` <-
`stoichiometry`, with `depiction` beside them on `chem`:

- `chem` — "is this the same structure?", the strict parse and the canonical form.
- `reagents` — the name a chemist wrote to the structure every tool needs.
- `stoichiometry` — the charge table, and the green metrics computed from its masses.
- `depiction` — the SVG.
"""
