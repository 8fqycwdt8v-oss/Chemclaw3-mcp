"""The `rxnpredict` engine: predictors, the meta-aggregator, and the pure helpers around them.

Nothing in this package imports FastAPI, MCP or an HTTP client, and `tests/test_no_egress.py`
proves it. Forked from `chemclaw2_forward` (MIT, same owner); see the server README for what
changed and why.
"""
