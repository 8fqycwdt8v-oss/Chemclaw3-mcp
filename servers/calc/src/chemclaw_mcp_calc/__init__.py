"""`calc` — the semiempirical calculators (GFN2-xTB, pKa, solubility, logD, descriptors), over MCP.

Layers: `engine/` <- `tools.py` (the MCP surface) <- `app.py` (the transport), one-way.

A **backend, not a connector**: Chemclaw3 keeps its own `calc` bundle (cache, calibration ledger,
artifact store, durable jobs) and calls this server from `cached_compute` on a cache miss. Hence
it is registered in `manifests-internal/` with `mount: backend`, so it can never win the `calc`
name collision on the agent's surface.

It serves request/response compute tools and keyed primitives (`relax_structure`,
`compute_hessian`, `scan_point`, the CREST searches, ...) — never a composite whose key names its
own output, such as thermochemistry, which Chemclaw3 assembles from parts. `calculation_key`
returns a calculation's identity before it runs, because only this server can derive the version
string (backend versions, `xtb --version`, calibration settings); Chemclaw3 never derives a key, so
the two `CALCULATION_EPOCH` constants compose in `remote_key` rather than having to agree.
"""
