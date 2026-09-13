# D-2026-09-13-an-audit-of-a-lockfile-no-image-reads-audits-nothing — An audit of a lockfile no image reads audits nothing

**Status:** accepted · **Date:** 2026-09-13 · **Commit:** wave W29, on top of `0fb12537`.
No hash is written for this pass, for the reason
`D-2026-09-12-a-bypass-that-is-not-in-the-suite-is-not-closed` §5 gives.

## Context

`make deps-audit` exports `uv.lock` and runs `pip-audit` over it, blocking in CI. **No Containerfile
in this repository read that lock**: none copied it, none passed `--constraint`, none ran
`uv sync`. All seven re-resolved independently with pip at build time from the open lower bounds in
each server's `pyproject.toml`. The Makefile's own comment said so, and
`tests/test_fleet.py::test_an_image_that_installs_from_the_index_pins_what_the_audit_read` said in
its docstring that the gap "cannot be closed from here" because closing it is a delivery change.

So the supply-chain gate proved a property of a *file* and not of anything shipped, and it was this
repository's largest self-declared open item.

### Measured, on the lightest server in the fleet

`props` is a dict lookup and a bisection with 37 third-party packages — the server least likely to
drift. Both forms were built in this sandbox with Docker and `pip freeze`d, against
`uv export --frozen --package chemclaw-mcp-props`:

| form | packages differing from `uv.lock` |
| --- | --- |
| `main`'s re-resolving Containerfile | **11 of 37** |
| the form this record adopts | **0**, in both directions |

The eleven: `mcp` 1.29.0 → **1.30.0**, `pydantic` 2.13.4 → 2.13.5, `pydantic-core` 2.46.4 → 2.46.5,
`pyjwt` 2.13.0 → 2.14.0, `uvicorn` 0.52.1 → 0.52.4, `sse-starlette` 3.4.8 → 3.4.11, `websockets`
17.0.1 → 17.1, `cryptography` 50.0.0 → 50.0.1, `anyio` 4.14.2 → 4.15.1, `click` 8.4.2 → 8.5.0,
`idna` 3.18 → 3.19, `python-dotenv` 1.2.2 → 1.2.3.

The first is worth naming twice: `CLAUDE.md` says the `mcp` SDK "is pinned to the **1.x** line
deliberately … matching its generation keeps `connector_app` line-for-line comparable with
`chemclaw.connectors.server`". The audited lock held 1.29.0 and the image shipped 1.30.0. Nothing
was wrong with that particular bump; nothing had decided it either.

The row this record answers quoted 11 of 100 for `rxnpredict`, measured 2026-08-28. The
proportion is worse on the smallest server, which is the opposite of what one would guess.

## Decision

Every Containerfile copies `pyproject.toml` and `uv.lock`, and installs the third-party closure
`uv export --frozen` produces, with `--require-hashes`:

```dockerfile
COPY pyproject.toml uv.lock /build/
...
RUN python -m pip install --no-cache-dir --upgrade pip "uv>=0.8.17,<1" \
    && uv export --frozen --package chemclaw-mcp-<name> --no-dev --no-emit-workspace \
         --format requirements-txt -o /build/requirements.txt \
    && python -m pip wheel --no-cache-dir --wheel-dir /wheels \
         --require-hashes -r /build/requirements.txt \
    && python -m pip wheel --no-cache-dir --no-deps --wheel-dir /wheels \
         ./packages/mcp_server_kit ./servers/<name>
```

Four things in that are decisions rather than syntax.

**Two `pip wheel` passes.** The first resolves nothing — every version and hash comes from the lock.
The second builds the two workspace packages `--no-deps`, because their dependencies are already in
the wheelhouse and letting pip look again is exactly the re-resolution this replaces.

**`--require-hashes`, not just pinned versions.** A lock without hashes cannot see an artefact
rewritten under a version that did not change, and that is the attack a supply-chain gate is for.

**`--frozen`.** A lock that has drifted from `pyproject.toml` fails the build rather than silently
re-resolving to fix itself.

**`uv` bounded by a range rather than pinned.** `--frozen` means the resolution cannot drift
whichever version runs, and a `uv` that changed this CLI fails the build loudly. A seventh copy of
an exact version across seven files is a constant nothing reconciles.

The `build` distribution was dropped from the same line: nothing in this repository invokes
`python -m build`, `pip wheel` does not need it, and the image built without it.

### `uv sync --frozen` was the alternative and is not taken

It would replace the wheelhouse with a venv and change what a runtime stage contains. The two-stage
`pip wheel` → `--no-index --find-links` shape is what keeps the runtime image free of a compiler,
and it already worked; the defect was where the *versions* came from, not how they were installed.

### What is still unlocked, named rather than implied

`rxnlabel`'s runtime stage installs `"rxnmapper==0.4.3" "rxn-insight==0.1.3"` straight from PyPI
through the CPU-torch index, which the lock does not carry. Those two are held to the lock by
`test_an_image_that_installs_from_the_index_pins_what_the_audit_read`, which is a version pin and
not a hash, and their transitive closure re-resolves. Moving them into the export means deciding
which torch build ships, which is a separate change.

## What keeps it true

- `tests/test_fleet.py::test_every_image_installs_the_closure_the_audit_read` — for all seven:
  `uv.lock` is in the build context, the export is `--frozen`, the `--package` name matches that
  server's own `pyproject.toml` (a copy-paste between two of the seven would otherwise build fine
  and install another server's dependencies), and the install is `--require-hashes`. Driven by
  mutation in all four directions.
- `tests/test_fleet.py::test_an_image_that_installs_from_the_index_pins_what_the_audit_read` —
  unchanged in behaviour and corrected in prose: its docstring opened with "and no image consumes
  it", which this commit falsifies.
- The build itself is a measurement, not a test: this suite cannot run Docker, and the numbers above
  are what a reader should re-run rather than trust.
