"""The `safety` engine: RDKit and the vendored tables, with no transport.

Imports no FastAPI, MCP or network client; `tests/test_no_egress.py` checks it. Import direction:
`chem` <- `screen` <- {`genotox`, `ich`}, with `reagents` on `chem` and read by `ich`.

- `chem` — the strict parse and canonical form.
- `screen` — process safety ("is this safe to run today"); also `read_table` and `SafetyRulesError`.
- `genotox` — genotoxicity alerts ("will this need a control strategy"); a separate table so a
  hazard screen is never reported as an ICH M7 assessment.
- `ich` — the transcribed Q3C and Q3D limits, with an honest miss.
- `reagents` — abbreviations and structures resolved to the names the ICH tables use.
"""
