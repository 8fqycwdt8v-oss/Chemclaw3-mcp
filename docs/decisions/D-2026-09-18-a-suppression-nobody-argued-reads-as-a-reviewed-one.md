# D-2026-09-18-a-suppression-nobody-argued-reads-as-a-reviewed-one — A suppression nobody argued reads as a reviewed one

**Status:** accepted · **Date:** 2026-09-18 · **Commit:** the three `# type: ignore` comments
`6df6eb19` added without an argument carry one at the site. A merged record is never edited, so the
accounting it got wrong is corrected here.

## What the record claimed

`D-2026-09-18-a-gate-that-does-not-read-the-tests-does-not-read-the-ratchets` says **"Two things are
`# type: ignore` on purpose, and each is load-bearing rather than decorative"**, and names
`servers/unitops/tests/test_isolation_ops.py` (`call-arg`, the omitted argument *is* the assertion)
and `packages/mcp_server_kit/tests/test_sessions.py` (`dict-item`, `SimpleNamespace` stand-ins in a
mapping upstream types as transports).

Counted against the diff, that commit adds **12** `# type: ignore` comments to code. Nine are
argued in that record: the two named above, three `import-untyped` on `jsonschema` and four
`attr-defined` on `lowlevel.jsonschema`, both covered by its "nothing in `pyproject.toml` changed"
paragraph. **Three are argued nowhere:**

Line numbers are as `6df6eb19` has them; this record's own commit adds the comments above them.

| where | code | what it suppresses |
| --- | --- | --- |
| `servers/chem/tests/test_sites.py:123` | `no-untyped-call` | `Call to untyped function "CanonSmiles"` |
| `servers/chem/tests/test_sites.py:233` | `no-untyped-call` | the same call |
| `servers/calc/tests/test_calculation_key.py:289` | `[misc]` widened to `[assignment, misc]` | `Cannot assign to a type` **and** `Incompatible types in assignment` |

A suppression with no reason beside it is indistinguishable, to the next reader, from one somebody
weighed — which is the shape this repository keeps deleting, most recently as a `# noqa: BLE001`
that had to carry its reason at the site
(`D-2026-09-13-the-rule-that-would-have-caught-it-was-not-the-one-asked-for`). The third is worse
than unargued: it is a **widening**, and a widening of a suppression is a decision about what else
may now pass silently.

## The decision

All three stay and each carries its reason at the site. None of them is a check this repository
declines.

- **`CanonSmiles`** is a gap in one third-party stub rather than a judgement here, and that is
  checkable: `rdkit-stubs` ships and annotates the compiled entry points — `MolFromSmiles` and
  `AddHs`, both `.so`-backed, need nothing on the very next line — while
  `rdkit-stubs/Chem/__init__.pyi:142` declares `def CanonSmiles(smi, useChiral = 1):` with no types
  at all. `warn_unused_ignores` is what makes writing it safe: the day rdkit annotates that stub,
  the comment goes red rather than outliving its reason.
- **`xtb_engine.Calculator = _explode`** is a substitution that *is* the assertion — a key
  derivation that touched the SCF would call it and raise `AssertionError`. mypy reports one
  substitution under two codes, and both describe the thing being done on purpose. The line carried
  `[misc]` alone while the test tree was outside the gate; `[assignment]` is what reading the file
  for the first time added, not a second decision.

Removing them was considered and declined for the third: rewriting the substitution as
`monkeypatch.setattr` would delete the suppression, but it rewrites the mechanics of a test about
`calc` key derivation to quiet a type checker, which is relaxing a test to serve a tool rather than
the other way round — the same direction that record rejected when it reverted an
`implicit_reexport` for `mcp.*`.

**No tree-wide ratchet is added, and that is deliberate.** Measured on 2026-09-18,
`grep -rn '# type: ignore' --include=*.py . | grep -v '^./.venv' | wc -l` answers **125** — the
command is here rather than only the number, for the reason this repository writes down about every
other count. Requiring an argument beside each would be a retrofit of that many sites taken as a
side effect of a fix round, which is how an allowlist nobody reads gets born. What the gate does
hold is that none of them is inert.

## What keeps it true

- `tests/test_fleet.py::test_the_type_gate_narrows_no_check_it_was_argued_out_of` — `strict = true`
  with no `disable_error_code` and no `warn_unused_ignores = false`, in the configuration *and* on
  the recipe. `warn_unused_ignores` is the half that matters here: it is what turns each of these
  three red the moment the error it names stops being reported, so a suppression cannot outlive its
  reason in silence.
- `tests/test_fleet.py::test_the_type_gate_reads_the_test_tree_and_not_only_the_source` — the
  file those three live in is read at all. That was the whole finding of the record being corrected.
