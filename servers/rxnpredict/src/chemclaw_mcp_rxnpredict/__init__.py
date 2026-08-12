"""`rxnpredict` — forward reaction and reaction-condition prediction, served over MCP.

A meta-model: several open-source predictors run in parallel and their outputs are combined by
Borda-weighted rank voting, gated on a coarse reaction class. Forked from
`8fqycwdt8v-oss/chemclaw2_forward` (MIT) and adapted to this fleet's standards.

Three layers, one-way: `engine/` <- `tools.py` <- `app.py`.
"""
