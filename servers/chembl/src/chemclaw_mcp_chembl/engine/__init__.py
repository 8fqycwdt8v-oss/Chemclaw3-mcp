"""The `chembl` engine: a read-only SQLite subset of ChEMBL and RDKit, with no transport.

Imports no FastAPI, MCP or network client; `tests/test_no_egress.py` checks it. Import direction:
`chem` and `store` <- `attribution` <- {`lookup`, `bioactivity`}, with `readiness` on all of them.

- `chem` — the strict SMILES parse and canonical form.
- `store` — the checksummed subset file, its schema version and the connection every query uses.
- `attribution` — the CC BY-SA 3.0 notice every answer carries.
- `lookup` — a compound by ChEMBL id, name or structure, never by a nearest match.
- `bioactivity` — the aggregated activities of one compound, per target, type and unit.
"""
