"""How much of this pod may be predicting at once — shed at admission, never queued.

One tool call is not one thread: the ensemble tools `asyncio.gather` over every enabled predictor,
each offloading separately, and each torch forward pass may itself use `torch.get_num_threads()`
cores (the image pins `OMP_NUM_THREADS=1`, but a deployment can raise it). So `acquire` takes a cost
in slots, where a slot is a core:

- an ensemble call costs `enabled predictors x inference_threads()`, from the deployment's enabled
  list rather than the caller's `models` argument, so a caller cannot lower it;
- a single-model call costs `inference_threads()`;
- a cost above the ceiling is clamped, so a wide ensemble runs exclusively rather than never.

`list_available_models` and `classify_reaction` are ungated plain `def`s: neither offloads, and a
caller must be able to ask what is available while the pod is full. `tests/test_admission.py` checks
the gated set against the served surface. A full pod refuses with a `ValueError` (passed verbatim)
rather than queueing past `request_timeout`. This is admission control, not a clock: cancellation
would not stop the worker thread.
"""

from __future__ import annotations

from mcp_server_kit.limits import Admission as KitAdmission

__all__ = ["ADMISSION_MARKER", "DEFAULT_MAX_CONCURRENT_PREDICTIONS", "Admission"]

# The attribute `tools._admitted` stamps on a gated tool; the coverage test reads it instead of a
# hand-kept list.
ADMISSION_MARKER = "__admission_gated__"

#: Cores' worth of inference that may be in flight; equals `deploy/deployment.yaml`'s `limits.cpu`,
#: which `tests/test_admission.py` checks. Overridable with
#: `CHEMCLAW_RXNPREDICT_MAX_CONCURRENT_PREDICTIONS`, read in `tools.py`.
DEFAULT_MAX_CONCURRENT_PREDICTIONS = 2


class Admission(KitAdmission):
    """A budget of concurrent inference slots, refused rather than queued past it.

    The mechanics are `mcp_server_kit.limits.Admission`'s; this subclass words the refusal.
    """

    unit = "prediction"
    server = "rxnpredict"

    def acquire(self, what: str, cost: int = 1) -> int:
        """Take `cost` slots, or refuse in terms the caller can act on.

        Args:
            what: The tool being asked for, named in the refusal.
            cost: Slots this call occupies, clamped into `1..limit` by the base class.

        Returns:
            The slots actually taken, which `release` must be given back.

        Raises:
            AtCapacityError: The budget has no room.
        """
        taken = self.take(cost)
        if taken.charged is None:
            raise self.refuse(
                f"this server has {taken.free} of its {self.limit} inference slots free and "
                f"{what} needs {min(max(cost, 1), self.limit)}, so it was refused rather than "
                "queued: a slot is one core, an ensemble runs every enabled predictor at once, "
                "and a queued prediction would come back after the caller had stopped waiting "
                "for it. Retry once one finishes, or ask a single model with "
                "predict_forward_single_model / predict_conditions_single_model, which costs "
                "this pod less. Raising CHEMCLAW_RXNPREDICT_MAX_CONCURRENT_PREDICTIONS only "
                "helps on a pod with more cores"
            )
        return taken.charged
