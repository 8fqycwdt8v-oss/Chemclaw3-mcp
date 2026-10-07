"""`rxnpredict` — forward reaction and reaction-condition prediction, served over MCP.

Several open-source predictors run in parallel and are combined by Borda-weighted rank voting, gated
on a coarse reaction class. Forked from `8fqycwdt8v-oss/chemclaw2_forward` (MIT). Layers, one-way:
`engine/` <- `tools.py` <- `app.py`.
"""
