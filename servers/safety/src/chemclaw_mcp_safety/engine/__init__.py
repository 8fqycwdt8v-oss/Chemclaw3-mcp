"""The `safety` engine: RDKit and four vendored tables, with no transport anywhere in sight.

Nothing in this package imports FastAPI, MCP or a network client, and `tests/test_no_egress.py`
proves it. That is the same line Chemclaw3 draws between `science/` and `connectors/`, for the same
reason: the chemistry stays testable without a transport, and a transport dependency can never creep
into a rule table.

Five modules, and the import direction inside is one-way — `chem` <- `screen` <- {`genotox`, `ich`},
with `reagents` on `chem` and read by `ich`:

- `chem` — the strict parse and the canonical form, copied from Chemclaw3 (see its docstring for the
  bound on that copy).
- `screen` — the process-safety question: "is this safe to run today". Also the home of
  `read_table`, the one loader every corpus here goes through, and of `SafetyRulesError`, the one
  exception type this engine raises.
- `genotox` — the regulatory-toxicology question: "will this need a control strategy". A separate
  table on purpose; conflating the two is how a hazard screen gets reported as an ICH M7 assessment.
- `ich` — "what is the number": the transcribed Q3C and Q3D limits, and an honest miss.
- `reagents` — the abbreviation or structure a chemist writes, resolved to the name the ICH tables
  are keyed by.
"""
