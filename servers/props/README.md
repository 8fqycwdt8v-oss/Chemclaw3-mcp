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
| `compare_solvents` | A named set side by side. |

All six are `read_only`: pure functions of their arguments and a read of a read-only table. Nothing
here writes, spends real compute, or has an effect worth gating — which matters, because "what is
the flash point of the solvent you are proposing" has to be answerable *before* a plan is approved,
not after.

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

- **What it is:** 44 solvents × 24 columns — identity (name, aliases, CAS, SMILES, formula, MW),
  physical (bp, mp, density, dielectric constant, Hansen parameters, Antoine constants, ΔHvap),
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
Three of the checks compare pairs of numbers that were written down independently, so a typo in one
of them cannot survive:

- **CAS check digits** — a CAS number carries its own checksum.
- **Molecular weight against formula** — computed from the formula, compared to the tabulated MW.
- **Antoine constants against the boiling point** — the fit must put 1 atm at the tabulated bp,
  within 2 °C. This is what makes it safe to carry Antoine constants for only 15 of the 44 rows: a
  bad set fails here rather than answering a distillation question.

The rest are structural — closed vocabularies, melting point below boiling point, ICH limits
present exactly when a class is, and no name or alias resolving two ways.

## Three things the tools are careful about

**Absent is not zero.** `flash_point_c` is `null` for dichloromethane, chloroform and water because
they *have* no flash point. Reporting 0 °C there would be a fire-safety error rather than a rounding
one, so the field stays `None` the whole way to the wire.

**Two routes to a vapour pressure, and they are not equally good.** `antoine` is a fit from the
table, worth about a percent near its range. `clausius_clapeyron` extrapolates from the normal
boiling point and is exact only there. `clausius_clapeyron_trouton` additionally *estimates* ΔHvap
by Trouton's rule, which underestimates it for alcohols, acids and water — so it reads high below
the boiling point. Every answer carries `method` and a `caveat` naming what that method is good for,
and the docstrings tell the model to quote them. A chemist sizing a condenser needs to know which of
the three they were given.

**Out of range is an error, not a number.** Both correlations stop at 400 °C, and
`boiling_point_at_pressure` refuses a pressure the solvent does not reach below it rather than
returning the end of its own search — which it used to do, answering "400.0 °C" for toluene at
200 bar and at 10 000 bar alike. The realistic way to ask an unreachable pressure is a unit slip, so
the refusal says which unit the argument is in. What the table still cannot check is each solvent's
critical point: it carries no `tc_c` column, so an answer between the boiling point and the ceiling
can be past it.

## What it is not

It knows nothing about reactivity. A Hansen shortlist is a *solubility* argument: it has no opinion
on whether the replacement is inert to the chemistry, dissolves the base, survives the temperature,
or lets the product crystallise, and it will happily rank a solvent that reacts with the substrate
as "close". The shortlist is candidates for a chemist to judge, and the tool says so in its own
output.
