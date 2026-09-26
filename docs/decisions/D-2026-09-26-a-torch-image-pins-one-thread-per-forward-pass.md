# D-2026-09-26-a-torch-image-pins-one-thread-per-forward-pass — A torch image pins one thread per forward pass

**Status:** accepted · **Date:** 2026-09-26

## What was found

`torch.get_num_threads()` is sized from the machine's cores, not from the container's CPU limit, and
neither `servers/rxnpredict/Containerfile` nor `servers/rxnlabel/Containerfile` set
`OMP_NUM_THREADS`; only `servers/calc/Containerfile` pinned its stack. Both admission gates charge a
call `inference_threads()`, which reads that width
(`D-2026-09-12-one-tool-call-is-not-one-thread`), so an unpinned pod was safe — it went serial —
but the ceiling meant "one call at a time" on any node wider than the pod, and each call's threads
contended for the pod's two cores.

The row asked for the measurement before the pin, because it is a latency change to inference.
Measured on 2026-09-26 in `python:3.11-slim` containers limited with `--cpus=2` on an eight-core
Docker VM (a loaded host; each figure the median of five after a warm-up), torch 2.13.0 CPU:

| workload | unpinned (8 threads) | `OMP_NUM_THREADS=1` | `OMP_NUM_THREADS=2` |
| --- | --- | --- | --- |
| RXNMapper 0.4.3, 16 reactions, one batch (`rxnlabel`'s pass) | 4,014 ms | 395 ms | 224 ms |
| T5 forward, d_model 512, 6+6 layers, 8 × 128 tokens (a proxy for `rxnpredict`'s T5 predictors) | 3,993 ms | 1,359 ms | 860 ms |
| two processes at once, RXNMapper, each | 27,085 / 26,618 ms | 504 / 519 ms | — |

Unpinned is not parallelism on a two-core pod; it is oversubscription under a CFS quota, an order
of magnitude slower alone and fifty times slower two at a time. The T5 figure is a random-weight
model of the same shape family, not a shipped checkpoint: the `rxnpredict` image needs its weights
fetched and was not built here.

## What was decided

- **Both images pin `OMP_NUM_THREADS=1`, `MKL_NUM_THREADS=1` and `OPENBLAS_NUM_THREADS=1`** in their
  runtime stage, as `calc` does. One thread rather than two because a slot is then a core by
  construction and the shipped ceiling of `limits.cpu` admits that many forward passes — two
  one-thread passes at once ran in ~0.5 s each, against 224 ms for one two-thread pass alone, so
  the pin trades a single caller's latency for the pod serving two, and `rxnpredict`'s ensembles
  fan out over several predictors per call, where a one-thread pass is the unit.
- **`inference_threads()` still reads torch rather than assuming 1**, so a deployment that raises
  the pin on a larger pod is charged what it set, with no code change.
- **The rule is derived, not listed**: any server whose locked closure resolves torch must carry the
  pins in its final stage, so a third torch server owes them the day its closure grows one.

## What keeps it true

- `tests/test_fleet.py::test_every_torch_image_pins_its_inference_thread_width`
- `tests/test_fleet.py::test_the_env_reader_reads_the_runtime_stage_only`
- `servers/rxnpredict/tests/test_admission.py::test_a_prediction_is_charged_the_models_threads_rather_than_one_call`
- `servers/rxnlabel/tests/test_admission.py::test_a_batch_is_charged_the_mappers_threads_rather_than_one_call`
