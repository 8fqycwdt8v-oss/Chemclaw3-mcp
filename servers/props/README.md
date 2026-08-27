# `props` — solvent and pure-component properties

The reference server for this repository, and a working capability in its own right: the physical,
safety and regulatory numbers a process decision turns on, for 44 solvents used in pharmaceutical
and fine-chemical process R&D.

It exists because those numbers are otherwise *recalled* rather than looked up. "What is the flash
point of 2-MeTHF", "what boils off at 100 mbar", "what can replace dichloromethane" are questions
with settled answers, and the useful thing an agent can do with them is have them to hand, cited, in
the same turn as the decision that depends on them.

## Tools

| Tool | Answers |
| --- | --- |
| `list_solvents` | What this server can and cannot be asked about. |
| `solvent_properties` | Everything recorded for one solvent — physical, safety, regulatory. |
| `vapour_pressure` | Vapour pressure at a temperature, with the method that produced it. |
| `boiling_point_at_pressure` | The rotovap and distillation question, in the useful direction. |
| `solvent_swap_candidates` | A filtered, Hansen-ranked replacement shortlist. |
| `compare_solvent_properties` | A named set side by side. |

All six are `read_only`: pure functions of their arguments and a read of a read-only table. Nothing
here writes, spends real compute, or has an effect worth gating — which matters, because "what is
the flash point of the solvent you are proposing" has to be answerable *before* a plan is approved,
not after.

### Why `compare_solvent_properties` is not called `compare_solvents`

It was, and that name belongs to something else. Chemclaw3's `calc` bundle declares a durable job
called `compare_solvents` — the same reaction computed in each solvent and ranked by ΔG — and a
deployment that registers this fleet's `manifests/` alongside core's connectors, which is the
documented wiring, has both names live at once. Measured with `calc` and `props` enabled: 21
endpoint tools plus 9 jobs is 30 declared names and 29 distinct ones.

Nothing caught it. `registry.job_tools()` refuses a job-vs-*job* collision because the name is an
authorization key, but no check compares a job name against an endpoint tool name, and
`connector_tool_names()` is a set union, so the two collapse into one entry silently. Chemclaw3 now
refuses that configuration at build time
(`D-2026-08-26-a-tool-name-is-one-capability-or-it-is-neither`); this rename is what makes the
configuration loadable again, and the one to move because `calc.compare_solvents` is named by
string in two `SKILL.md` files, the agent's system prompt, `durable/connector_job.py` and two
live-test probes, against six references to this one, all inside this repository.

The two are not near-duplicates that could be merged: this reads measured numbers out of a table in
microseconds, that one runs a semiempirical calculation per species per solvent as a durable job.

## Running it

```sh
make run-props                            # from the repository root; 127.0.0.1:8850
curl -s localhost:8850/healthz            # {"status":"ok","server":"props"}
```

The bearer token is `CHEMCLAW_PROPS_TOKEN`, and the same variable name is read on both sides —
Chemclaw3 to send it, this server to verify it. It is enforced even on the loopback dev URL: a
manifest whose auth mode changes with its address is one whose serving side gets it wrong.

## The data

`src/chemclaw_mcp_props/data/records.csv`, described and checksummed by `dataset.json` beside it.

- **What it is:** 44 solvents × 26 columns — identity (name, aliases, CAS, SMILES, formula, MW),
  physical (bp, mp, density, dielectric constant, Hansen parameters, Antoine constants *and the
  temperature range they were fitted over*, ΔHvap),
  handling (flash point, water miscibility, peroxide formation), and regulatory (ICH Q3C class and
  residual-solvent limit, an indicative greenness band, GHS hazard flags).
- **Licence:** CC0-1.0. First-party content, hand-compiled in this repository from publicly
  published reference values.
- **Refreshed by:** whoever changes it, in a pull request. There is no ingestion job and no
  upstream to drift from — which is exactly why this server was the right one to build first.

**Verify against a primary source before any value here enters a regulatory filing, a safety case,
or equipment sizing.** It is a working table for an agent's reasoning, not a certified reference,
and every tool says so through the `source` field it returns.

### How the table checks itself

`tests/test_dataset.py` validates the corpus against itself rather than against anything external.
Five of the checks compare numbers that were written down independently, so a typo in one of them
cannot survive:

- **CAS check digits** — a CAS number carries its own checksum.
- **Molecular weight against formula** — computed from the formula, compared to the tabulated MW.
- **Antoine constants against the boiling point** — the fit must put 1 atm at the tabulated bp,
  within 2 °C. This is what makes it safe to carry Antoine constants for only 16 of the 44 rows: a
  bad set fails here rather than answering a distillation question.
- **Flash point against the lower flammable limit** — a closed-cup flash point *is* the temperature
  at which the vapour first reaches its LFL, so the modelled vapour there must land on a real one
  (0.8–15 vol%; formic acid's genuine 18% is named). This is what caught acetic acid: its ΔHvap is
  the dimerisation-suppressed boiling-point value, extrapolating it to ambient read 5.6× high, and
  the screen saw 15.8 vol% against an LFL of 4.0%.
- **The Hansen polar term against Beerbower** — `δP = 37.4·μ/√Vm`, with the dipole moment held in
  the test rather than in the corpus, so the screen shares no input with the column it checks. The
  Hansen triple is otherwise transcribed as a unit and has no internal check at all; this caught
  dimethyl carbonate at δP = 8.6 where the published value is 3.9.

The rest are structural — closed vocabularies, melting point below boiling point, ICH limits
present exactly when a class is, and no name or alias resolving two ways.

## Two things the tools are careful about

**Absent is not zero.** `flash_point_c` is `null` for dichloromethane, chloroform and water because
they *have* no flash point. Reporting 0 °C there would be a fire-safety error rather than a rounding
one, so the field stays `None` the whole way to the wire.

**Two routes to a vapour pressure, and they are not equally good.** `antoine` is a fit from the
table, worth about a percent, and is returned *only inside the temperature range that fit was made
over* — the table carries that range beside the constants, the caveat quotes it, and outside it the
answer falls back to the boiling-point route and says so. (It did not, until 2026-08: there was no
range in the corpus and no check in the code, so water at 200 °C came back as 13.775 bar against a
steam-table 15.549 bar, labelled `antoine` and carrying the "good to about a percent" caveat.)
`clausius_clapeyron` extrapolates from the normal
boiling point and is exact only there. `clausius_clapeyron_trouton` additionally *estimates* ΔHvap
by Trouton's rule, which underestimates it for alcohols, acids and water — so it reads high below
the boiling point. Every answer carries `method` and a `caveat` naming what that method is good for,
and the docstrings tell the model to quote them. A chemist sizing a condenser needs to know which of
the three they were given.

## What it is not

It knows nothing about reactivity. A Hansen shortlist is a *solubility* argument: it has no opinion
on whether the replacement is inert to the chemistry, dissolves the base, survives the temperature,
or lets the product crystallise, and it will happily rank a solvent that reacts with the substrate
as "close". The shortlist is candidates for a chemist to judge, and the tool says so in its own
output.
