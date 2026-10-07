"""What this server must be able to do before it takes traffic: fork, run, and answer.

The probe is a real run through the same `sandbox.run` a tool call uses, with the same limits: an
import check cannot see a child that fails to start or fails to return a result.

Cached per process because it checks an image property and each probe forks a process and would
increment the runs counter. `lru_cache` does not cache exceptions, so a pod that starts broken
stays 503 until replaced.
"""

from __future__ import annotations

import json
from functools import lru_cache

from mcp_server_kit import Dataset

from chemclaw_mcp_pyexec.engine.sandbox import run

__all__ = ["verify_sandbox"]

# No imports and no allocation, so a failure is the sandbox's, not the program's.
_PROBE = "result = {'probe': 1 + 1}"
_EXPECTED = {"probe": 2}


@lru_cache(maxsize=1)
def verify_sandbox() -> tuple[Dataset, ...]:
    """Run one trivial program in the child and check what came back.

    Returns:
        An empty tuple: no corpus, so `/healthz` reports `datasets: []`.

    Raises:
        RuntimeError: the child could not start, was killed, or returned the wrong value;
            `connector_app` turns this into a 503.
    """
    outcome = run(_PROBE)
    if outcome.error is not None:
        raise RuntimeError(f"the pyexec sandbox could not run its readiness probe: {outcome.error}")
    if outcome.result_json is None or json.loads(outcome.result_json) != _EXPECTED:
        raise RuntimeError(
            "the pyexec sandbox ran its readiness probe and returned "
            f"{outcome.result_json!r} rather than {json.dumps(_EXPECTED)}"
        )
    return ()
