# D-2026-09-27-a-cpu-pod-locks-the-cpu-torch — The lock resolves PyTorch's CPU build on Linux, still by hash

**Status:** accepted · **Date:** 2026-09-27

## What was found

`D-2026-09-26-the-labeller-s-torch-is-the-lock-s-torch` moved `rxnlabel`'s models onto the hashed
lock export and accepted the price that came with it: `uv.lock` resolved PyPI's torch 2.13.0, whose
linux wheel depends on the `nvidia-*`, `cuda-*` and `triton` wheels. `rxnpredict` already shipped
the same closure. No pod in this fleet has a GPU. That record left two questions for later, and
this one answers both.

- **Image size, measured.** The CI `images` job now prints `docker image inspect`'s size. Every
  image was built at `64201bd`, which is `main` plus that step (run 36297650767): **`rxnlabel`
  8,792,004,407 bytes and `rxnpredict` 9,594,829,103 bytes**, uncompressed.
- **Whether `pip-audit` audits a `+cpu` local version.** It does not. On 2026-09-27, in the Linux
  gate image, `make deps-audit` over the re-keyed lock printed `Skipping torch: Dependency not
  found on PyPI and could not be audited: torch (2.13.0+cpu)`, then `No known vulnerabilities
  found`, and exited 0. Linux is the platform every image is built for, so switching the source
  alone would have taken torch out of the supply-chain gate with the gate still green.

## What was decided

- **A `pytorch-cpu` index (`https://download.pytorch.org/whl/cpu`, `explicit = true`) and a
  Linux-only `torch` source in the root `pyproject.toml`.** Workspace members inherit it. Because
  the index is explicit, nothing else can resolve from it. On macOS the lock keeps PyPI's wheel,
  which is already a CPU build, so `make install` is unchanged on macOS arm64 and the dev
  environment installs no torch on either platform. The lock was re-keyed with
  `uv lock -P torch==2.13.0`: a plain `uv lock` took the new fork to 2.14.0+cpu. Holding the version
  means the only change in the images is CUDA against CPU. The CUDA runtime entries (`cuda-*`,
  `nvidia-*` and `triton`) left the lock.
- **`rxnlabel`'s `models` extra names `torch` directly.** uv binds a source only to a direct
  requirement, and `rxnlabel` reaches torch through `rxnmapper`. Measured: with that line removed
  the export is unchanged today, but only because `rxnpredict` names torch and a universal lock
  resolves one torch per platform. That comes from the workspace, not from this server.
- **Hashing is unchanged.** `uv export` (0.9.x) has no option that writes an index into the
  requirements file, so the hashed `pip wheel` pass in both Containerfiles carries
  `--extra-index-url https://download.pytorch.org/whl/cpu`. It gives pip no choice:
  `--require-hashes` refuses any artefact whose digest `uv.lock` did not record, whichever index
  offered it. `unhashed_installs` now accepts an index flag only in that shape: an
  `--extra-index-url` on a `--require-hashes -r` pass, naming a registry `uv.lock` records as a
  source. An `--index-url` that replaces PyPI, a `--trusted-host`, an index the lock never
  resolved from, or the CPU index on any other install is still refused, and the bite test drives
  each of those.
- **`make deps-audit` audits `2.13.0+cpu` as `2.13.0`.** Advisories are filed against the public
  release, and the `+cpu` build comes from the same source tag, so a `sed` strips the local segment
  before `pip-audit` reads the export. A `Dependency not found on PyPI` skip now fails the gate, so
  the next local version cannot drop out the same way. Re-run after the change in the gate image,
  the only skips left are the workspace's twelve editable members, under a different reason.

## What it bought

Measured by the same `images` step on this change's pull request (#132, at `b812a29`):
**`rxnlabel` 2,261,081,578 bytes and `rxnpredict` 3,063,906,221 bytes**, down from 8,792,004,407
and 9,594,829,103 — about 6.5 GB less per image, uncompressed. The offline `/healthz` smoke passed
on both, and `rxnlabel`'s build-time load check mapped a reaction on the CPU torch. `chem` and
`calc` came out byte-identical in size to the baseline, as expected, since neither installs torch.

## What keeps it true

- `tests/test_fleet.py::test_the_lock_resolves_a_cpu_torch_for_the_cpu_only_pods`
- `tests/test_fleet.py::test_no_image_installs_what_the_lock_did_not_hash`
- `tests/test_fleet.py::test_the_unhashed_install_check_refuses_the_shapes_it_was_written_for`
- The `deps-audit` target's not-found failure is Makefile shell and no test drives it, because it
  needs the advisory database's network. What holds it is the `deps` CI job on every push.
