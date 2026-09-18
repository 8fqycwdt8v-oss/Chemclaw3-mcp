# D-2026-09-18-a-backend-that-writes-the-metadata-is-a-dependency-of-the-wheel — A backend that writes the metadata is a dependency of the wheel

**Status:** accepted · **Date:** 2026-09-18 · **Commit:** `hatchling` joins the `build` dependency
group, eleven Containerfiles build `--no-build-isolation`, and `make deps-audit` reads that group.
Closes two `docs/BACKLOG.md` rows — §4's "every image still takes its *build backend* from pip's
isolation" and §1's "the build backend every wheel is built with is outside `uv.lock`" — which turn
out to be one subject asked from two ends. Extends
`D-2026-09-16-a-dependency-with-no-wheel-builds-under-whatever-pip-fetches-that-day`, which closed
the same hole for the one sdist-only dependency in the lock.

## The measurement the row asked for first

The §4 row named the close (a group entry plus two lines per Containerfile) and refused to take it
without one number: **can a `hatchling` in the build environment change what `hatchling.build` puts
in a wheel?**

Driven on this workspace, `servers/props` and `packages/mcp_server_kit`, built twice from the same
source with `pip wheel --no-deps --no-build-isolation` in two venvs holding **hatchling 1.21.1** and
**hatchling 1.32.3** — eleven minor versions apart:

| | props | mcp_server_kit |
| --- | --- | --- |
| size under 1.21.1 | 27,633 B | 113,832 B |
| size under 1.32.3 | 27,631 B | 113,832 B |
| members | 12 / 12, identical set | 19 / 19, identical set |
| payload members differing | **0** | **0** |
| dist-info members differing | `WHEEL`, `METADATA`, `RECORD` | `WHEEL`, `METADATA`, `RECORD` |

The content of the difference, both packages:

```
--- WHEEL
-Generator: hatchling 1.21.1
+Generator: hatchling 1.32.3
--- METADATA
-Metadata-Version: 2.1
+Metadata-Version: 2.5
```

`RECORD` differs because it carries those two files' digests. No member's *timestamp* differs, so
this is not the zip-timestamp non-determinism that `D-2026-09-16` had to rule out for `geometric`.

**So the answer is yes, and narrower than the headline.** A backend swap does not silently drop a
corpus here — every payload byte is identical across eleven minors, which is a real thing to know
before writing eleven edits. What it does decide is the metadata grammar the wheel advertises
itself with, and the version stamped into it. That is enough on its own: `Metadata-Version` is what
a consumer parses, it is chosen by whichever version pip fetched that morning, and the code that
chooses it is arbitrary third-party code *executing* inside the build either way.

The third reason is not in the table and is the one that decided it. File **selection** is a
hatchling behaviour, not a pyproject fact: `test_every_server_builds_a_wheel_that_carries_its_data`
exists because `packages = [...]` plus a redundant `force-include` made four of five servers
unbuildable, and that interaction is the backend's. An unbounded `build-system.requires =
["hatchling"]` means the next image build picks up whatever semantics the latest release has.

## What changed

- `hatchling` joins `[dependency-groups] build` beside `setuptools`, with no lower bound — the
  twelve `build-system.requires` lines state none, this group's job is to make the *resolution*
  reproducible rather than to add a floor nobody measured, and the lock pins the exact version.
  `uv lock` resolved `hatchling 1.32.3` and brought `tomlkit` and `trove-classifiers` with it.
- **Eleven Containerfiles**, not twelve — the row's own count was of a tree with one more image than
  this one has. Ten gain the `uv export --only-group build` + `pip install --require-hashes` pair
  `servers/calc` already had, and all eleven gain `--no-build-isolation` on the second `pip wheel`
  pass, the one that builds `mcp_server_kit` and the server.
- `make deps-audit` exports `--group build`. Measured: 4 packages the audit had never seen
  (`hatchling`, `tomlkit`, `trove-classifiers`, `pathspec` — `setuptools` and `pluggy` were already
  in through `rxn-insight`), and `No known vulnerabilities found, 13 ignored` both with and without
  the flag. A group in `uv.lock` that the audit export omits is `D-2026-09-13`'s own defect one
  group over.

## Where a build dependency belongs, which is the §1 row's question

That row asked whether a build dependency is worth pinning here at all, and if so whether the honest
place is a `[tool.uv] constraint` the export can carry "rather than a second lock nothing reads".

**Worth pinning: yes**, for the reason above — it is executing code, in eleven builds.

**The place is the `build` dependency group, and the row's dichotomy was the wrong one.** A
dependency group is not a second lock: `uv.lock` carries it (`build = [{ name = "setuptools",
specifier = ">=70.1" }]` was already there, and `hatchling` is there now), and `uv export
--only-group build --format requirements-txt` emits it **with hashes**. There is one declaration and
the lock resolves it.

**`[tool.uv]` constraints are rejected on a fact rather than a preference: uv reads them and every
image builds with pip.** `uv build-constraint-dependencies` would pin the one build path whose
artefacts nothing ships and leave all eleven image builds exactly as they were — the opposite of
where the exposure is.

## What is still outside it, measured rather than implied

`tests/test_fleet.py::test_every_server_builds_a_wheel_that_carries_its_data` runs `uv build
--offline`, which resolves `build-system.requires` from the uv cache. Constraining it was tried and
**breaks the offline lane**, which is why it is not done:

```
$ uv export --frozen --only-group build ... -o b.txt
$ uv build --offline --wheel --build-constraints b.txt servers/props
  ├─▶ Failed to install requirements from `build-system.requires`
  ├─▶ Failed to download `trove-classifiers==2026.6.1.19`
  ╰─▶ Network connectivity is disabled, but the requested data wasn't found in the cache
```

The cache is warmed by `uv sync`, which installs the workspace editable and therefore fetches *a*
backend — not the one the lock's build group names. Making the two agree would mean `uv sync
--group build` in `make install`, which changes every contributor's environment to pin a wheel that
is deleted three lines later in the only test that builds it. The exposure is bounded and stated:
that wheel is never shipped, and the payload-identity measurement above is what says a backend swap
would not change the corpus assertion it exists to make.

## What keeps it true

- `tests/test_fleet.py::test_every_image_builds_its_workspace_wheels_under_the_locked_backend` —
  per server, both directions: the group is exported, it is installed with `--require-hashes`, and
  the `--no-deps` wheel pass carries `--no-build-isolation`. This is what a twelfth Containerfile
  owes.
- `tests/test_fleet.py::test_the_build_group_names_every_backend_this_workspace_declares` — derives
  the backend set from every `pyproject.toml`'s `build-system.requires` and requires the group to
  name each one, and `uv.lock` to resolve it. A group entry the lock does not carry exports nothing,
  and `pip install` of nothing succeeds.
- `tests/test_fleet.py::test_a_sdist_only_dependency_builds_under_a_pinned_backend` — narrowed to
  the *third-party* wheel pass, which is the only half specific to a server's closure. Its old
  `else` branch asserted the **absence** of a pin on any server with no sdist in its closure; that
  became false the moment the backend was pinned fleet-wide, and a test asserting an absence the
  fix removes is a test that must be rewritten rather than deleted.
- `tests/test_fleet.py::test_the_build_group_is_what_the_calc_image_exports` — unchanged.
