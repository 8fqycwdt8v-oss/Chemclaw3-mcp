# D-2026-09-16-a-dependency-with-no-wheel-builds-under-whatever-pip-fetches-that-day — A dependency with no wheel builds under whatever pip fetches that day

**Status:** accepted · **Date:** 2026-09-16 · **Commit:** the `calc` build-backend pin. Closes a hole
in `D-2026-09-13-an-audit-of-a-lockfile-no-image-reads-audits-nothing` that opened the moment
`servers/calc` took its first sdist-only dependency.

## What was measured

`uv.lock` resolves `geometric==1.1.1` with an `sdist` block and **no** `wheels` block. It is the only
such entry in the lock — measured over every registry package, in both directions. PyPI has never
published a wheel for it, for any release back to 0.9.3.

So the first pass of `servers/calc/Containerfile` *builds* it. Driven on the exported requirement
line, hashes and all:

```
$ pip wheel --no-cache-dir --no-deps --require-hashes -r geo.txt
Collecting geometric==1.1.1 ...
  Installing build dependencies: started
  ...
```

`Installing build dependencies` is pip resolving **its own** requirements from PyPI at that moment.
The sdist has no `pyproject.toml` at all — a legacy `setup.py` beside a bundled `versioneer.py` — so
pip falls back to its defaults (`setuptools`, `wheel`), picks whatever versions the index offers that
day, installs them without hashes, and then executes `setup.py` and `versioneer.py` under them. None
of it is in `uv.lock` and none of it is anything `make deps-audit` read.

That the build is not merely unaudited but also **not fixed** was measured the same way: the same
sdist built under the backend pip fetched and under the locked `setuptools==80.10.2` produced wheels
of different sizes — **408,349** bytes against **408,351**, twice each, consistently. So what the
image ships depends on what pip happened to install that morning.

The obvious stronger claim is not available and is worth saying so: these wheels are **not**
byte-reproducible run to run under *either* backend — two builds under one setuptools already differ
in digest, because the zip carries timestamps. The size is the part that is attributable to the
backend, and the digest is not. A first draft of this record said "two different setuptools produced
two different wheel digests", which is true and does not isolate anything.

Two sentences in `servers/calc/Containerfile` were false as a result. "Every third-party version and
hash comes from `uv.lock` … so what this image installs is what `make deps-audit` read" was true of
what is *installed* and silent about what is *executed*; "RDKit, tblite, scipy and numpy all ship
manylinux wheels, so nothing is compiled here" omitted the one dependency that ships none.

## The fix, and the two that were weighed against it

Three forms were available:

1. **Pre-build the wheel in its own stage.** Moves the problem rather than solving it: the stage
   still needs a backend from somewhere.
2. **A constraints file for the build stage.** Hashes maintained by hand, beside a lock that already
   knows how to produce them — a second declaration of one fact.
3. **A `build` dependency group in the lock, installed first, with `--no-build-isolation`.** The
   backend becomes an ordinary locked, hashed dependency; `uv export --only-group build` produces the
   requirements file; `pip install --require-hashes` puts it in the build stage; `pip wheel
   --no-build-isolation` then uses that one and fetches nothing. Measured: `Installing build
   dependencies` is gone.

The third is the one taken. `setuptools` **alone** is in the group: it is already in the closure
`make deps-audit` reads, through `rxn-insight` — which is also what caps it below 81 — so the group
adds no dependency the supply-chain gate cannot see. `wheel` is not needed; setuptools has carried
`bdist_wheel` since 70.1, measured against this sdist.

`--no-build-isolation` applies to the whole requirements file, which is safe because every other
entry ships a wheel and is therefore never built. That is derived from the lock rather than written
down, so a second sdist-only dependency arriving next year is a red test rather than a silent build.

## What is still outside it, named rather than implied

The **second** `pip wheel` pass — the one that builds `mcp_server_kit` and this server from source —
still takes `hatchling` from pip's build isolation, unhashed, at build time. That is the fleet-wide
shape rather than anything `calc` introduced: twelve Containerfiles have it, no runtime artefact of
it reaches the final image, and closing it is twelve edits and a second group. `docs/BACKLOG.md`
carries the row.

Neither gap ever put an unaudited package *in* a shipped image: the final stage installs
`--no-index --find-links=/wheels`, so what runs is exactly the locked closure. What was at stake is
arbitrary unpinned code executing in the build, and a build artefact that is not reproducible.

## What keeps it true

- `tests/test_fleet.py::test_a_sdist_only_dependency_builds_under_a_pinned_backend` — derives the
  sdist-only set from `uv.lock`, requires the whole shape of any server whose closure holds one, and
  refuses it of any server whose closure does not.
- `tests/test_fleet.py::test_the_build_group_is_what_the_calc_image_exports` — the other end of the
  pipe: an `--only-group build` against a group the lock does not carry exports nothing, and
  `pip install` of nothing succeeds.
- `tests/test_fleet.py::test_every_image_installs_the_closure_the_audit_read` — the check this one
  extends, unchanged.
