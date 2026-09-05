"""What this server has to be able to do before it may take traffic: fork, run, and answer.

Every other server in the fleet is unready when a *file* will not load. This one is unready when a
*process* will not run, and that is a harder thing to be sure of from inside the parent: nothing
here touches the sandbox at import, so the whole apparatus that decides whether `run_python` can
answer — the interpreter on the image, `runner.py` beside it, the `prctl(PR_SET_DUMPABLE, 0)` seal
the parent applies before it forks, and the resource limits the child sets on itself — is first
exercised by whichever caller happens to arrive first. Until this existed `/healthz` was a constant
`{"status": "ok"}`, so a pod with a broken interpreter or an unwritable scratch directory passed the
kubelet probe, took traffic, and failed every call — the precise gap `connector_app`'s `/healthz`
docstring says it closed, and the one every `deploy/deployment.yaml` in this repository claims in a
comment.

**The probe is a real run**, through the same `sandbox.run` a tool call uses, with the same limits.
An import check would prove nothing: the two failures worth catching are a child that cannot be
started and a child that starts and cannot return a result, and neither is visible from this
process without actually forking one.

**Cached, deliberately, and this is a different argument from `safety`'s.** There the loaders
underneath are `lru_cache`d anyway; here a fresh probe forks a process every time. Two costs: about
66 ms of CPU on a 10-second probe interval, and — the one that would have been quietly corrosive —
one increment of `chemclaw_mcp_pyexec_runs_total{outcome="ok"}` per probe, roughly 8,600 a day,
which would bury the caller activity that counter exists to describe under the server watching
itself. What the probe checks is an *image* property, so proving it once per process is the honest
scope. `lru_cache` does not cache exceptions, so a pod that starts broken is re-probed on every
request and stays 503 (bounded by `connector_app`'s own five-second failure memo) until it is
replaced.
"""

from __future__ import annotations

import json
from functools import lru_cache

from mcp_server_kit import Dataset

from chemclaw_mcp_pyexec.engine.sandbox import run

__all__ = ["verify_sandbox"]

# A program with no imports, no allocation and one arithmetic fact, so that a failure is a failure
# of the *sandbox* rather than of anything the program did. It exercises the whole round trip:
# payload written, child forked and sealed, limits applied, `result` assigned, JSON written back.
_PROBE = "result = {'probe': 1 + 1}"
_EXPECTED = {"probe": 2}


@lru_cache(maxsize=1)
def verify_sandbox() -> tuple[Dataset, ...]:
    """Run one trivial program in the child and check what came back.

    Returns:
        An empty tuple. This server vendors no corpus, so `/healthz` reports `datasets: []` — the
        field is present and empty rather than absent, which is what says the check ran.

    Raises:
        RuntimeError: the child could not be started, was killed, or did not return the value the
            probe program assigned. `connector_app` turns that into a 503 naming the reason, which
            is the point: a pod whose sandbox does not work must not be sent a program to run.
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
