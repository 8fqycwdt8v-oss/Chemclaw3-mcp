# D-2026-09-19-a-bound-on-the-site-count-prices-half-the-work — A bound on the site count prices half the work

**Status:** accepted · **Date:** 2026-09-19 · **Commit:** `servers/chem`'s microstate bound moves
from the ionisable-site count to `sites x heavy atoms`, its refusal stops asserting a cost it has
never timed, `describe_topology` stops advertising itself as free, and `servers/calc`'s atom-ceiling
refusal gains the way forward its own docstring already named.

Revisits `D-2026-09-18-an-output-cap-is-not-a-bound-on-the-work` on purpose. That record's argument
— that a cap on the answer cannot bound the work, so the bound has to be on the input — stands
whole and is not reopened. What is wrong with it is one line of arithmetic: it bounded the wrong
input.

## What the site-only bound did, measured on the shipped entry point

`MAX_IONISABLE_SITES = 32` compared `len(acidic) + len(basic)` against a constant. The work it was
standing in for is one `_shift`, one `SanitizeMol` and one `_canonical` **per site, over the whole
graph** — so the cost is the product of the site count and the molecule size, which is what
`servers/chem/README.md` said in the sentence beside the bound while the bound read one factor of
it. The consequence is not a tuning error, it is an ordering error: a bound monotone in one factor
of a product necessarily refuses something cheaper than what it admits.

| molecule | heavy atoms | sites | product | measured | answer | site-only bound |
| --- | --- | --- | --- | --- | --- | --- |
| PAMAM G3 dendrimer | 484 | 62 | 30,008 | **128 ms** | 6 species | **refused** |
| PAMAM G4 dendrimer | 996 | 126 | 125,496 | **587 ms** | 7 species | **refused** |
| polyamine chain | 1,891 | 31 | 58,621 | **413 ms** | 32 species | admitted |

The third row is there to be read against the first: the site-only bound admitted a 413 ms call and
refused a 128 ms one, and the only thing separating them was a factor it did not read.

**PAMAM dendrimers are catalogue items**, sold by the gram and routine in formulation and delivery
work, and they are symmetric — so their microstates collapse and the answer is six or seven
structures. Degeneracy is the **normal** case for a symmetric real molecule (dendrimer, star
polymer, symmetric macrocycle, symmetric chelator). `D-2026-09-18` met it first as
`"C1" + "CNC" * 659 + "CN1"` and generalised from the adversarial instance: its "by contract"
derivation reasoned that a molecule with more sites than `MAX_MICROSTATES` can only come in under
the cap *by degeneracy*, and treated that as the pathological case. It is the ordinary one, and the
same sentence is what made 32 look safe.

## The bound

`MAX_SITE_ATOM_PRODUCT`, default **150,000**, `CHEMCLAW_CHEM_MAX_SITE_ATOM_PRODUCT`, checked in the
same place — after both `_sites` passes, before any `_shift` — against
`(len(acidic) + len(basic)) * mol.GetNumHeavyAtoms()`. The atom count is a descriptor read on a
molecule already parsed, so the check still costs nothing.

### Why the product and not a per-call deadline

`servers/calc/engine/budget.py::Deadline` is the other shape available and is the right instrument
there, for reasons that do not carry here. It exists because a single SCF is uninterruptible and
seconds long, so no cheap function of the *input* predicts what a relaxation will cost — the cycle
count is discovered, not derivable. `enumerate_microstates` is the opposite: its work is exactly
`sites` units of a known shape, all of them derivable from the molecule before the first one runs.

Three things follow, and each is why the deadline is the worse instrument here:

- **A deadline refuses after paying.** It would spend the budget and *then* raise, which is
  precisely the defect `D-2026-09-18` was written to end — its first measured case was 48 s spent
  before a `ValueError`.
- **A deadline is machine-dependent.** The same molecule would be answered on an idle pod and
  refused on a loaded one, so a chemist's second attempt is a different tool.
- **A deadline cannot name the overrun.** The refusal below states the cost as a factor of the
  bound, which is exact for every input and needs no wall clock; a deadline knows only that it ran
  out.

### Why 150,000

Cost per site-atom is not flat across molecular shapes, and that is the fact the number is derived
against. Measured on this container, best of two runs each, with the site bound lifted:

| shape | heavy atoms | sites | product | measured | us / site-atom |
| --- | --- | --- | --- | --- | --- |
| PAMAM G3 | 484 | 62 | 30,008 | 129 ms | 4.31 |
| PAMAM G4 | 996 | 126 | 125,496 | 591 ms | 4.71 |
| macrocycle, 223 amines | 669 | 223 | 149,187 | 841 ms | 5.64 |
| chain, 250 amines | 500 | 250 | 125,000 | 693 ms | 5.55 |
| chain, 100 amines | 1,500 | 100 | 150,000 | **1,266 ms** | **8.44** |
| chain, 130 amines | 910 | 130 | 118,300 | 943 ms | 7.97 |
| macrocycle, 387 amines | 1,161 | 387 | 449,307 | 3,146 ms | 7.00 |
| chain, 660 amines | 1,980 | 660 | 1,306,800 | 12,479 ms | 9.55 |

So the product predicts cost to within a factor of two across every shape driven, where the site
count alone is wrong by a factor of thirteen between the first and the sixth row. The bound is
priced at the worst of the shapes, not the mean.

150,000 is then fixed by one requirement and checked against two:

- **It must admit PAMAM G4** (125,496), which is the largest PAMAM this server can see at all:
  G5 is 2,004 heavy atoms and `MCP_MAX_MOLECULE_ATOMS` refuses it at the parse. 150,000 is 19.5%
  above it. It is also 75 ionisable sites at the largest molecule the parse bound admits.
- **The worst call it admits is 1,266 ms**, which is 2.4x inside `PROBE_TIMEOUT_SECONDS` (3 s) and
  24x inside `connector.yaml`'s `request_timeout` (30 s).
- **The 660-site polyamine is refused at 8.7x the bound**, i.e. by a wide margin rather than by a
  hair, on both of its shapes.

### The cost band this is measured against, and why the old one did not survive

`D-2026-09-18` derived 32 against "the 0.1-0.62 s band this server's four *other* enumerators
already occupy at that size". **That band is an artefact of the fixture.** It was measured on
1,900-atom linear alkanes, where a tautomer, stereoisomer or degradant enumeration finds almost
nothing. Re-measured on a molecule a caller may now send — PAMAM G4, 996 heavy atoms, branched and
amide-rich — with one call each:

| enumerator | PAMAM G4 | PAMAM G3 | 1,900-atom alkane | input bound |
| --- | --- | --- | --- | --- |
| `enumerate_tautomer_set` | **2,801 ms** | 836 ms | 260 ms | none |
| `describe_molecule` | **2,793 ms** | 839 ms | 407 ms | none |
| `enumerate_degradant_candidates` | **1,823 ms** | 277 ms | 66 ms | none |
| `enumerate_microstates` | 587 ms | 128 ms | 76 ms | this one |
| `enumerate_stereoisomer_set` | 18 ms | 8 ms | 213 ms | none |

1,266 ms therefore does not make `enumerate_microstates` the expensive one; it leaves it the cheapest
of the three that do real work on a large molecule, and it is still the only one of the five with an
input bound at all. `docs/BACKLOG.md`'s ceiling row carries these numbers now, because the four
figures it was written with describe the fixture rather than the tools.

**Stating what this costs**, since it is a real loosening as well as a real fix: the worst admitted
call roughly doubles, from about 0.56 s to about 1.27 s.

## The refusal

The sentence that shipped was false about every molecule the old bound actually refused:

> `<smiles>` has 62 ionisable sites, above the limit of 32. Each one is toggled, sanitised and
> canonicalised separately, so enumerating them all would hold this server for tens of seconds —
> past the timeout you are waiting on — to produce a set this tool would then refuse as too large,
> or a handful of structures if the sites are equivalent. Ask about the fragment whose protonation
> you care about, or resolve the ionisation you already know.

For PAMAM G3, which is the molecule that text was quoted about above, the truth is **128 ms** and
**six structures**. `CLAUDE.md`'s "docstrings are the prompt" rule applies to a refusal more sharply
than to a docstring: `connector_app` passes a `ValueError` to the model verbatim, so this sentence
is the whole of what a chemist is told, and it asserted a cost nothing had timed for the input in
front of it.

Three things are wrong with it and each is fixed:

1. **A cost it has not measured.** The new wording states the overrun as a **factor** of the bound.
   A factor is exact for every input, derived from the same quantity the bound prices, and cannot go
   stale on a faster pod — which a seconds figure in prose would, and which is the argument
   `docs/BACKLOG.md` rule 3 and `CLAUDE.md`'s port section already make about numbers in prose.
2. **A claim about the answer.** "a set this tool would then refuse as too large" is not known at
   the point of refusal and is false for every symmetric molecule. The new wording says so:
   *how many microstates that would yield is not known and may well be small; this refuses the cost,
   not the answer*.
3. **No way forward.** `describe_topology` was named only in `tools.py`'s docstring and never in the
   `ValueError` the caller receives, and "ask about the fragment whose protonation you care about"
   is not a way forward for a dendrimer or an oligonucleotide, where the protonation question *is*
   about the whole molecule. The new wording names three: `describe_topology`, the repeat unit where
   the sites repeat by symmetry, and `CHEMCLAW_CHEM_MAX_SITE_ATOM_PRODUCT` on the deployment — the
   last being the only one that works when the question really is about the whole thing, and the
   same shape `servers/calc`'s refusals already use.

## `describe_topology` is not free

It was advertised as "Free and structural — no calculation runs", and the refusal pointed at it as
the cheap tool to ask first. Measured, it is the **more expensive** of the two on every molecule
where the choice matters: 839 ms against the enumeration's 128 ms on PAMAM G3, 2,793 ms against
587 ms on G4, 346 ms against 6 ms on a 301-atom peptide. The cost is `tautomer_count`, which calls
`enumerate_tautomer_set` unconditionally — an enumeration rather than a descriptor read.

It stays unbounded and stays the tool to ask first, because it answers for a molecule the
enumeration refuses and that is the property its callers rely on. What changes is the claim: three
tool docstrings and this server's README now say what it costs. The word "free" is removed from
`tools.py` wherever it described a tool whose cost was measured here.

`D-2026-09-18`'s own §"What keeps it true" repeats the free claim in a test name
(`test_the_free_tool_still_answers_for_a_molecule_the_enumeration_refuses`). **That record is merged
and is not edited**, and the test keeps its name so the citation still resolves — this record is
where the correction lives, which is the mechanism `docs/decisions/README.md` prescribes.

## Two findings beside the bound

**The `@cache`'s stated reason for being safe is the wrong reason.** `_compiled`'s docstring said
sharing one compiled query is safe because "`GetSubstructMatches` does not mutate it". That is false
for a **recursive** SMARTS, and two of the eleven patterns are one — the aliphatic-amine pattern
with its four `!$(...)` guards, and the pyridine-type pattern. RDKit caches a recursive query's
match set against the current target *on the query object*, which is why RDKit has an
`RDK_BUILD_THREADSAFE_SSS` build flag at all. (The review that found this said six of eleven were
recursive; measured, it is two. The count was wrong and the finding was not.) What makes the sharing
safe is that flag, and it is load-bearing rather than incidental: every tool body here runs in
`asyncio.to_thread`, so two concurrent calls match against one shared query by construction. The
cache is what introduced that dependency and nothing asserted it — so a wheel built without the flag
would have been a silent data race under concurrency rather than a red test. `rdBase._multithreadedEnabled`
is `True` in the wheel this lockfile resolves, and it is now asserted; driven beside it, 16 threads
x 400 matches over the shared queries give zero wrong answers.

**A SMILES that does not parse was quoted in three places as if it did.** `"C1" + "CNC" * 659` is an
unclosed ring — `Chem.MolFromSmiles` returns `None`. The molecule the measurements were taken on is
`"C1" + "CNC" * 659 + "CN1"`, which is what the test fixture builds and what the two in-tree copies
now say.

## `servers/calc`, in the same commit

**The atom ceiling's refusal named no way forward.** `Structure._normalize_and_validate`'s own
docstring said what the options are — "a smaller system or a deployment configured for a larger one"
— and the message named neither, while the same server's budget refusal names three ("run a smaller
system, relax it first, or raise `CHEMCLAW_XTB_INLINE_TIMEOUT_SECONDS`"). The docstring is not in
the loop: `connector_app` passes the `ValueError` verbatim and the docstring reaches nobody. The
message now names `CHEMCLAW_XTB_MAX_ATOMS`, which is the one remedy a caller cannot guess.

**The ceiling stays a single ceiling, and the basis is now stated as the worst path.**
`xtb_max_atoms` gates eight tools and `D-2026-09-18-a-ceiling-is-derived-from-the-pod-it-protects`
derives it entirely from geomeTRIC's coordinate build, which only `relax_structure` and `scan_point`
run: a GFN2 single point at 509 atoms peaks at 312 MiB against that build's 974 MiB, so the
property, Fukui, `combine_structures` and CREST paths are capped on a basis **3.1x** above what they
cost. Two answers were available — a second, looser global ceiling with an optimizer-specific one
inside it, or one ceiling whose basis is declared. This takes the second, for three reasons: the
band it would buy (451-500 atoms) is well outside the declared workload, since `Chemclaw3`'s D-100
puts it at 200-800 Da, about 120 atoms; raising a global ceiling means re-deriving the pod
arithmetic for CREST, whose subprocess memory nothing here has measured; and `xtb_hessian_max_atoms`
is the precedent for a per-tool bound and it goes the other way, *tightening* inside the global one,
which is the direction that needs no new pod arithmetic. What was not acceptable was the previous
state, where the cost paragraph justified the narrowing for relaxation only. That is now said at
the enforcement site.

**The defect that ADR called "a real defect" is queued.** It measured one optimizer cycle at 99.17 s
for 509 atoms against a 780 s inline budget — about eleven cycles at the 450-atom ceiling, against
D-100's measured 177 steps at 76 atoms — and set it aside as not that decision's basis. `grep` found
no row for it. There is one now, with the arithmetic and with what it is not (a request to lower the
ceiling, which is a memory bound).

## What keeps it true

- `servers/chem/tests/test_microstate_bound.py::test_a_pamam_dendrimer_is_answered_rather_than_refused`
  — the regression itself, on the three dendrimer generations the site-only bound refused, with
  their atom counts, site counts and species counts written out. Red before this commit with
  `ValueError` on G3 and G4.
- `servers/chem/tests/test_microstate_bound.py::test_the_bound_prices_the_work_and_not_the_site_count`
  — the inequality that makes a site-only bound wrong rather than merely tight: the 62-site
  dendrimer is a cheaper call than the 31-site chain, asserted as an ordering so that a shape whose
  cost stops tracking the product reds it.
- `servers/chem/tests/test_microstate_bound.py::test_the_refusal_states_no_cost_it_has_not_timed`
  — "tens of seconds", "seconds" and "too large" are absent from the refusal, because it has timed
  none of them for the input in front of it.
- `servers/chem/tests/test_microstate_bound.py::test_the_refusal_names_a_way_forward_the_caller_can_act_on`
  — `describe_topology`, the repeat unit and `CHEMCLAW_CHEM_MAX_SITE_ATOM_PRODUCT` in the
  `ValueError` a caller actually receives, not in a docstring beside it.
- `servers/chem/tests/test_microstate_bound.py::test_the_refusal_names_the_sites_rather_than_the_species_it_did_not_count`
  — both factors of the product and the overrun factor, and no species count. The name is
  `D-2026-09-18`'s and stays so that record's citation resolves.
- `servers/chem/tests/test_microstate_bound.py::test_the_worst_call_the_bound_admits_stays_inside_the_probe_budget`
  — re-derived against the worst *shape* at the product rather than the largest molecule, driven
  against `PROBE_TIMEOUT_SECONDS` rather than against the 1,266 ms measured here.
- `servers/chem/tests/test_microstate_bound.py::test_the_topology_tool_is_not_the_cheap_substitute_its_docstring_claimed`
  — `describe_topology` measured against the enumeration it is offered as an alternative to, as an
  ordering, so a slower runner cannot red it and a `describe_molecule` that stopped enumerating can.
- `servers/chem/tests/test_microstate_bound.py::test_the_cache_rests_on_a_threadsafe_rdkit_build_and_not_on_an_immutable_query`
  and `::test_the_shared_queries_answer_correctly_under_concurrency` — the build flag the `@cache`
  actually depends on, asserted and then driven over 16 threads.
- `servers/chem/tests/test_microstate_bound.py::test_the_bound_is_read_from_the_environment_and_is_not_a_constant`
  and `::test_a_bound_that_would_refuse_every_ionisable_molecule_stops_the_import` — the two halves
  `D-2026-09-12-a-bound-that-can-be-set-to-zero-has-to-say-what-zero-means` asks of every bound
  here, under the new variable's name.
- `servers/calc/tests/test_cost_bounds.py::test_the_ceiling_s_refusal_names_the_way_forward_its_own_docstring_states`
  — `CHEMCLAW_XTB_MAX_ATOMS` in the message rather than in the docstring above it.
