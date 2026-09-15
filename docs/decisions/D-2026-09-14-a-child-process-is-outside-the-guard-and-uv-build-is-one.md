# D-2026-09-14-a-child-process-is-outside-the-guard-and-uv-build-is-one — A child process is outside the guard, and `uv build` is one

**Status:** accepted · **Date:** 2026-09-14 · **Commit:** fix pass over the adversarial review of
`1c1c2d6` (#70). No hash is written for the fix commits themselves, for the reason
`D-2026-09-12-a-bypass-that-is-not-in-the-suite-is-not-closed` §5 gives.

## Context

`CLAUDE.md` states the no-egress posture as four independent layers, and layer 3 is the one that
does not trust this repository's own code:

> **The whole suite runs with the guard armed** (root `conftest.py`). A test that only passes by
> reaching the internet fails instead, which is what makes a vendored dataset *proven* sufficient.
> `make offline-run` goes further and takes the network away entirely.

`tests/test_fleet.py::test_every_server_builds_a_wheel_that_carries_its_data` shells out to
`uv build`, which resolves each server's `build-system.requires` — `hatchling` — and, given the
chance, resolves it from PyPI. That is a **child process**, the first of the four channels the same
file names as outside `mcp_server_kit.egress` by construction: `arm()` rebinds a Python socket
object's methods in *this* interpreter and reaches nothing a fork does. Layer 2 cannot see it either
— `no_egress.py` reads imports, and `subprocess` is how `pyexec` and `calc` do their work, so it is
deliberately not on the list.

### Measured

Driven at HEAD, with the network namespace taken away and the uv cache emptied (a scratch
`UV_CACHE_DIR`, so the real cache was never touched):

```
$ UV_CACHE_DIR=<empty> unshare --user --map-root-user --net -- \
    python scripts/offline_check.py -q \
    tests/test_fleet.py::test_every_server_builds_a_wheel_that_carries_its_data
E  AssertionError: calc cannot be packaged, so it cannot be deployed:
E    ├─▶ Failed to fetch: `https://pypi.org/simple/hatchling/`
E    ╰─▶ failed to lookup address information: Temporary failure in name resolution
1 failed in 3.52s
```

The same command with the cache as `make check` leaves it: `1 passed in 2.18s`. So `make offline-run`
was green *because the online lane had already fetched the thing it was supposed to prove was
unnecessary*. Layer 3's claim — the strongest one in the posture, and the only one that is not an
assertion about this repository — was false at HEAD, and had been since the test was added.

The direction matters more than the failure. A cold-cache run reds loudly and reads as a network
problem in an offline lane, which is survivable. The shipped behaviour was the other one: a warm
cache made the offline lane *agree* with a claim it had not checked.

## Decision

The build is `uv build --wheel --offline`. With the flag uv may read registry packages only from the
cache, so the child process cannot reach an index in either lane and a cold cache fails identically
in both. Nothing else about the test changes.

Two facts make the flag free rather than a new fragility, and both are measured:

- **The cache is warm before the suite runs, by `make install`.** `uv sync` installs all eight
  workspace members editable, and an editable install of a hatchling-backed package fetches the
  backend. Driven against a scratch cache: `uv pip install --no-deps -e servers/calc` alone put
  `hatchling-1.32.0` into `wheels-v5/pypi/hatchling`, after which `uv build --wheel --offline`
  succeeded from that cache.
- **`hatchling` is not in `uv.lock`**, which is why this could not have been caught by the lockfile
  controls `D-2026-09-13-an-audit-of-a-lockfile-no-image-reads-audits-nothing` built. A build
  dependency is resolved per build, outside the resolution any of those tests read. It is also
  therefore outside `make deps-audit`, and this record does not close that — see below.

### What this does not do

It does not put build-time dependencies under the supply-chain audit. `uv export --frozen` exports
the locked closure, and `hatchling` is not in it; what this repository now knows about the backend it
builds wheels with is that it is whatever version the cache holds. That is a real gap and it is not
this change's to close — the wheels this test builds are never shipped (an image installs from the
lock, `--require-hashes`), so the exposure is a test-time build host rather than a published
artefact. Recorded in `docs/BACKLOG.md`.

It also does not extend to the other child processes this suite runs. `xtb`, `crest` and `pyexec`'s
sandboxed interpreter are the ones the four-channel paragraph is actually about, and they are
covered by `make offline-run` taking the network away — which is exactly the lane this record found
was answering about a warmed cache.

## What keeps it true

- `tests/test_fleet.py::test_every_server_builds_a_wheel_that_carries_its_data` — now `--offline`.
  Driven three ways at the fix: warm cache online `1 passed`, warm cache inside the namespace
  `1 passed`, **empty cache online `1 failed`**. That third lane is the whole change: with the flag
  removed as a mutation (`git diff --numstat` → `13 0`, the argv line alone restored to its old
  spelling) the same empty-cache online lane read `1 passed in 2.49s`, having silently reached PyPI.
- `scripts/offline_check.py` under `make offline-run` — unchanged, and now measuring what its own
  module docstring says it measures.
