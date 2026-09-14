# D-2026-09-14-thirteen-ignored-is-duplication-not-aliasing — `13 ignored` is duplication, not aliasing

**Status:** accepted · **Date:** 2026-09-14 · **Commit:** post-merge fix pass over Wave 30, on top
of `24b50ec` (PR #66).

## Context

Wave 30's headline supply-chain correction was that `--ignore-vuln` matches an advisory by its id
**or any alias**, and that this is *why* eight suppression rows silence the 13 findings the closure
reports without them. It shipped in four places: `Makefile`, `pyproject.toml`'s
`[tool.chemclaw.deps-audit]` header, `tests/test_deps_suppressions.py`'s module docstring, and §4.4
of `D-2026-09-14-what-this-fleet-enforces-bounds-measures-and-accepts` — the document a deployment
team reads before putting this fleet in front of chemists.

The alias half is true. The *because* is not.

### Measured

Re-measured 2026-09-14 against this lock, network reachable, export
`uv export --all-packages --all-extras --no-hashes --no-dev` (557 lines).

With **no** suppressions passed:

```
$ uvx pip-audit --no-deps --disable-pip -r req.txt
Found 13 known vulnerabilities in 4 packages
accelerate   1.14.0  PYSEC-2026-3804
diskcache    5.6.3   PYSEC-2026-2447          <- twice
setuptools   80.10.2 PYSEC-2026-3447 83.0.0   <- twice
transformers 4.57.6  PYSEC-2025-217
transformers 4.57.6  PYSEC-2026-2288 5.0.0rc3 / 5.0.0   <- twice
transformers 4.57.6  PYSEC-2026-2289 5.3.0    <- twice
transformers 4.57.6  PYSEC-2026-2290 / 5.5.0  <- twice
transformers 4.57.6  PYSEC-2026-3929 5.10.0
```

Thirteen findings over **eight distinct advisory ids**: five are returned twice, three once.
10 + 3 = 13, and eight rows suppress eight advisories.

The duplication is the advisory service's, not the export's. The export carries one
`diskcache==5.6.3` line (`grep -n '^diskcache==' → 83`), and a requirements file containing that one
line reproduces it:

```
$ printf 'diskcache==5.6.3\n' > one.txt
$ uvx pip-audit --no-deps --disable-pip -r one.txt
Found 2 known vulnerabilities in 1 package
diskcache 5.6.3 PYSEC-2026-2447
diskcache 5.6.3 PYSEC-2026-2447
```

So an exact-id row, with no alias anywhere in play, already silences two:

```
$ uvx pip-audit --no-deps --disable-pip --ignore-vuln PYSEC-2026-2447 -r req.txt
Found 11 known vulnerabilities, ignored 2 in 3 packages
```

And aliasing, measured on its own, accounts for exactly two findings — one per alias row:

```
$ uvx pip-audit --no-deps --disable-pip \
    --ignore-vuln GHSA-xrqw-3rrv-vx5w --ignore-vuln CVE-2026-69112 -r req.txt
Found 11 known vulnerabilities, ignored 2 in 3 packages
```

**Aliasing explains 2 of the 13 and none of the 8→13 gap.**

### Why this is worth a record rather than a word swap

The two stories teach opposite rules to the reader the register is written for. "A suppression can
silence more findings than it names, because an id carries aliases" means *a row can quietly cover
advisories nobody listed* — a real and alarming property to design a review around. What actually
happens is duplicate records under ids this table **does** list, which is a property of the advisory
service and carries no such reach. The wave whose subject was what this fleet can and cannot claim
shipped the more alarming of the two explanations, by measuring one arm (drop a row, watch its other
spelling reappear) and generalising it to the whole number.

That is the same shape as the arithmetic errors this family keeps recording: one confirming
measurement, and the alternative explanation never driven. Two sufficient-looking causes were
available and only one was tested.

## Decision

The three unmerged sites now state the decomposition rather than a mechanism: eight rows, thirteen
findings, five advisories returned twice, aliasing responsible for two — each with the command that
shows it. The alias fact keeps its own paragraph, because it is the reason `aliases` is a field in
the table at all and the reason two rows name an id this closure never reports under.

**`13` is recorded as a dated reading of a third-party service, and nothing in the suite pins it.**
It cannot be: the suite runs with the egress guard armed, so the only thing that could ask is
`make deps-audit` itself, and the number will move on the advisory database's schedule rather than
this repository's. What *is* pinned is local and is the pair the argument rests on — eight rows,
each naming a package and the version `uv.lock` still resolves.

### Two corrections to a merged record

`D-2026-09-14-what-this-fleet-enforces-bounds-measures-and-accepts` is merged and is not edited.

- **§4.4's first paragraph** attributes `13 ignored` to alias matching. Read it as: eight
  declarations, thirteen findings, because five of the eight advisories are reported twice; and two
  of the eight rows are matched by alias, which is why they are written under a GHSA/CVE id.
- **§4.4's expiry paragraph opens "They expire: `--ignore-vuln` matches by id"** — the exact phrase
  the same commit corrected two paragraphs above. The expiry argument is unaffected (it is about a
  dependency being *fixed*, where id-or-alias makes no difference), but the sentence is the
  stale-phrase genre that record names as its own reason to exist.

## What keeps it true

- `tests/test_deps_suppressions.py::test_the_makefile_argues_exactly_the_suppressions_that_are_declared`
  — the prose and the table still hold each other in both directions after the rewrite. Driven: a
  `# PYSEC-9999-0001` paragraph appended to the `Makefile` fails it (`argued - declared`), and a
  ninth row in `pyproject.toml` fails it the other way (`declared - argued`).
- `tests/test_deps_suppressions.py::test_a_suppression_expires_when_its_package_moves` and
  `test_a_suppression_names_a_package_the_lock_still_resolves` — the local half that *is* pinned,
  unchanged.
- Nothing holds the number `13`, deliberately, and the three rewritten comments say so where a
  reader meets it. The only thing that can ask is `make deps-audit`, which needs the network the
  rest of this repository refuses.
