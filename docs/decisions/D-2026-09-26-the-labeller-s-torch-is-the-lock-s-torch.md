# D-2026-09-26-the-labeller-s-torch-is-the-lock-s-torch — rxnlabel's models install from the hashed lock, and ship the lock's torch

**Status:** accepted · **Date:** 2026-09-26

## What was found

`D-2026-09-13-an-audit-of-a-lockfile-no-image-reads-audits-nothing` put every Containerfile on
`uv export --frozen ... --require-hashes`, with one exception it named: `servers/rxnlabel`'s
runtime stage ran a third `pip install` of `"rxnmapper==0.4.3" "rxn-insight==0.1.3"` straight from
PyPI through the CPU-torch `--extra-index-url`. The two named packages were version-pinned to the
lock by a test; their transitive closure — torch, transformers, tokenizers and the rest — was
resolved by pip on the day of each build, with no hashes. So it was the one image whose closure
`make deps-audit` did not describe, and nothing in the suite refused a second install of that shape
in any image: `test_every_image_installs_the_closure_the_audit_read` required the hashed export to
exist and never asked whether it was the only way in.

Folding the pair into the export decides which torch ships. `uv.lock` resolves PyPI's torch 2.13.0,
whose linux wheel depends on the `nvidia-*`, `cuda-*` and `triton` wheels; the old install took the
CPU index's. Summed from `uv.lock`'s recorded wheel sizes for linux x86_64 / cp311, the exported
`models` closure is ~2.9 GB of wheels, ~2.2 GB of them the CUDA runtime a CPU-only pod never loads.

## What was decided

- **The models are the `models` extra, exported from the lock and fetched `--require-hashes`**, in
  the same build-stage pass as every other dependency, and the runtime stage installs
  `chemclaw-mcp-rxnlabel[models]` `--no-index` from that wheelhouse. Nothing in the image resolves.
- **The torch that ships is the lock's**, CUDA closure and all. That is what `servers/rxnpredict`'s
  image, installing its extras from the same lock, already ships, so the fleet has one answer
  rather than two; and the alternative — a CPU torch source in `pyproject.toml` — re-locks both servers and
  raises a question this change should not answer in passing: whether `pip-audit` audits a `+cpu`
  local version or skips it. That is queued in `docs/BACKLOG.md` with the figures above. The price
  accepted here is image size; the price refused was an unaudited, unhashed closure.
- **Any image installing outside a hashed pass fails the suite.** `unhashed_installs` accepts a pip
  command in exactly three shapes — `--require-hashes -r <export>`, `--no-index` from the wheelhouse,
  `--no-deps` over local paths — plus the build-stage bootstrap of pip and `uv`, verbatim; any
  `--index-url`, `--extra-index-url` or `--trusted-host` is refused whatever else the line carries.

Verified by building: the `rxnlabel` build stage completed on 2026-09-26 — the whole `models`
closure, torch and the CUDA wheels included, fetched and hash-checked from the export, and both
workspace wheels built. The runtime stage then failed with `ENOSPC` on the local Docker VM's disk
while unpacking that closure, so the bake of the RXNMapper weights was not re-run here; CI has no
image job.

## What keeps it true

- `tests/test_fleet.py::test_no_image_installs_what_the_lock_did_not_hash`
- `tests/test_fleet.py::test_the_unhashed_install_check_refuses_the_shapes_it_was_written_for`
- `tests/test_fleet.py::test_every_image_installs_the_closure_the_audit_read`
- `tests/test_fleet.py::test_an_image_that_installs_from_the_index_pins_what_the_audit_read`
