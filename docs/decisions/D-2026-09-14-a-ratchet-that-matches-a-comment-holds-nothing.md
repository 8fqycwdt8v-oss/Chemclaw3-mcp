# D-2026-09-14-a-ratchet-that-matches-a-comment-holds-nothing — A ratchet that matches a comment holds nothing

**Status:** accepted · **Date:** 2026-09-14 · **Commit:** fix pass over W26/W27/W29, on top of
`f3f3c9c`. No hash is written for this pass, for the reason
`D-2026-09-12-a-bypass-that-is-not-in-the-suite-is-not-closed` §5 gives.

## Context

`D-2026-09-13-an-audit-of-a-lockfile-no-image-reads-audits-nothing` is the largest claim in that
wave: every image installs the closure `uv export --frozen` produces, `--require-hashes`, so what
ships is what `make deps-audit` read. What holds it is
`tests/test_fleet.py::test_every_image_installs_the_closure_the_audit_read`, and that test read the
Containerfile as **one string** and substring-matched four literals.

A Containerfile is mostly prose. All seven here carry a comment block explaining the export, the
`--frozen` and the `--require-hashes` — in the words the test matches — directly above the `RUN`
that does it. So the literals exist twice, and the test could not tell which copy it was reading.

### Measured

`servers/safety/Containerfile`'s real install was replaced by an unpinned
`RUN python -m pip wheel --wheel-dir /wheels chemclaw-mcp-safety`, with the deleted phrases moved
into a `#` line above it (`git diff --stat` → `1 file changed, 4 insertions(+), 6 deletions(-)`):

```
$ uv run pytest tests/ -q
207 passed
```

The whole root suite, green, with the supply-chain control of the wave that wrote it deleted from a
shipped image. The reviewer's independent run reported the same 207.

This is the shape this family has now shipped repeatedly and named twice —
`D-2026-09-12-a-ratchet-measures-what-it-parses`, `D-2026-09-12-a-test-that-re-types-the-expression-under-test-asserts-nothing`.
The new instance of it is narrower and worth stating on its own: **an assertion over a file's text
is satisfied by any copy of that text in the file, and a build file's comments are the copy most
likely to be there**, because a good comment quotes the command it explains.

A second, quieter hole in the same test: `--require-hashes -r /build/requirements.txt` and
`uv export --frozen --package …` were each required *somewhere* in the file. Two literals in two
unrelated instructions are not a pipeline — `RUN echo --require-hashes` satisfies half of it.

## Decision

The check reads what the builder reads. `tests/test_fleet.py::containerfile_instructions` turns a
Containerfile into its logical instructions: a line whose first non-blank character is `#` is
dropped, a trailing `\` joins the next line, and whitespace collapses so an assertion can name a
phrase without pinning its indentation. Every assertion in both image tests is then made against
those instructions.

Two consequences beyond removing the comment:

- **The export and the hashed install must be in the same `RUN`.** The file is selected by
  `uv export --frozen --package` and the `--require-hashes` install is required *in that
  instruction*. Exactly one such `RUN` may exist, because a check that reads the first of two is a
  coin toss about which one ships.
- **`COPY pyproject.toml uv.lock /build/` is matched as a whole instruction**, not as a substring,
  so the same comment trick does not restore it either.

`test_an_image_that_installs_from_the_index_pins_what_the_audit_read` scans the same instruction
list rather than the raw text, so a stale `"foo==1.2"` quoted in a comment neither satisfies nor
fails that pin check. It has no live consequence today; the two tests reading one file two ways
would be a defect waiting for its first comment.

### What this does not do

It still asserts what a Containerfile **declares**. Whether a build installs that closure is a
measurement — W29's numbers, re-run — and nothing in this suite can run Docker. The correction is
to the control, not to the claim: the claim was independently re-measured at `f3f3c9c` and holds
(four servers built, 0 version differences against the lock in both directions).

## What keeps it true

- `tests/test_fleet.py::test_every_image_installs_the_closure_the_audit_read` — now over the
  instruction list, with the export and the hashed install required in one `RUN`. Driven: the
  mutation above, which the shipped form passed, now fails on `safety` alone.
- `tests/test_fleet.py::test_the_image_install_check_reads_instructions_and_not_the_text` — the bite
  test, on a synthetic file rather than the seven real ones, because every real one carries each
  phrase in prose *and* in the `RUN` and so cannot distinguish the two parsers. Driven: deleting the
  two-line comment skip from `containerfile_instructions` turns it red while the other 98 stay green.
- `tests/test_fleet.py::test_an_image_that_installs_from_the_index_pins_what_the_audit_read` —
  unchanged in behaviour, reading the same instruction list.
