# `suitability` — USP &lt;621&gt; chromatographic system suitability

Seven read-only tools on **port 8892**. Every input is a number a chromatogram already reported;
every answer carries the formula and the convention it came out of.

```sh
make run-suitability      # 127.0.0.1:8892, dev token
```

## What it serves

| Tool | Answers |
| --- | --- |
| `replicate_precision` | RSD over a replicate injection series, **and** whether &lt;621&gt; accepts that many injections for that limit |
| `plate_count` | N, by the tangent or half-height form, with plates per metre if a column length is given |
| `peak_symmetry` | The USP tailing factor at 5% height, and whether the peak tails or fronts |
| `peak_resolution` | Rs between two adjacent peaks, by either form, flagging the Gaussian assumption |
| `retention_factor` | k for a peak, and α against a second one |
| `permitted_method_adjustment` | Whether a proposed change to an isocratic method needs revalidation |
| `system_suitability_report` | A whole injection sequence against a method's declared criteria |

## The thing this server is shaped around

**USP defines two forms of the plate count and two of the resolution, and they do not agree on a
real peak.**

```
N  = 16 (t_R/W)^2          tangent (baseline) width
N  = 5.54 (t_R/W_0.5)^2    width at half height

Rs = 2 (Δt)/(W1 + W2)             tangent widths
Rs = 1.18 (Δt)/(W_0.5,1 + W_0.5,2)  half-height widths
```

The second of each pair was *derived* from the first by substituting the Gaussian relation between
the two widths, so they agree exactly on a Gaussian peak and diverge as it tails — and they diverge
in the unhelpful direction. Measured in `tests/test_peaks.py`: the same tailing peak reads **39%
more efficient** under the half-height convention, which is the one a data system reports without
being asked.

So `convention` is a required argument with no default on every tool where USP gives two forms, and
every answer repeats which it used. Nothing here converts between them, because the conversion is
only valid under the Gaussian assumption the divergence already violates.

That property is also what makes the readiness probe strong. Since 5.54 came from 16 and 1.18 from
2, the constants over-determine each other: on a Gaussian peak the two forms must agree to the
rounding in the published constants — 0.09% and 0.21% respectively. `engine/selftest.py` checks
exactly that, so a transposed digit (5.54 → 5.45, off by 1.7%) refuses the pod, where a probe that
merely called each function and found a float would pass it.

## What it is not

- **It does not integrate a chromatogram.** No peak finding, no baseline assignment, no shoulder
  resolution, no instrument file. A retention time it is handed is taken as given.
- **It is not method validation.** Passing suitability says the system performed at the moment of
  the run. Whether the method is fit for purpose is an ICH Q2 exercise over separate preparations —
  linearity, accuracy, LOD/LOQ, intermediate precision — and it is deliberately **not** here. That
  is a different question with a different data shape and, if it is ever built, it is a different
  server: the first tool that wants a t-distribution wants a dependency this image does not carry.
- **It is not a monograph.** `permitted_method_adjustment` reports what the general chapter permits.
  The section opens "unless otherwise specified in the individual monograph", and a monograph
  overrides it. Every answer says so, because the model reading it will not know otherwise.
- **`failures: []` does not mean a run is suitable.** It means every criterion that was *declared*
  passed. A criterion nobody passed in cannot be checked, and the common way a suitability review
  goes wrong is that the one limit nobody typed is the one that failed.

## Why it exists

Chemclaw3's eval corpus has measured this gap for as long as it has existed:
`data/evals/probes/analytical.yaml` carries a dozen probes whose only honest answer was a refusal.
One of them pastes a real failure:

> Six replicate injections … Area RSD 2.4%, tailing limit is ≤ 2.0. It passed last week on the
> same column. What is going on?

Six seven-digit areas, summarised in prose. Fed to this server, the sample standard deviation gives
**2.476%** — and the population form gives 2.260%, so the pasted summary matches neither. That is
realistic fixture data rather than a defect in the probe, and it is exactly the point: hand
arithmetic over six seven-digit numbers, or a model doing it in prose, is the wrong instrument. The
same call also separates the two failures the chemist has conflated — the tailing factor is out of
spec, the RSD is over its limit, and the injection count is *sufficient* for a 2.0% limit — three
verdicts where "suitability failed" is one.

`tests/test_precision.py` pins that case, so the number in this README is checked rather than
recalled.

## What it reads

Nothing. There is no `data/` directory and no corpus to refresh. The USP formulas and the
adjustment allowance table are first-party constants in `engine/`, versioned by
`selftest.CONSTANTS_VERSION` and digested into `/healthz` — the digest covers the allowance
*values*, not the source file, so a comment edit does not move it and a changed limit always does.

**Who refreshes it:** whoever transcribes a revision of USP &lt;621&gt;. That is a pull request that
changes `engine/adjustments.py` and bumps `CONSTANTS_VERSION`, reviewed by a person, never an
automated fetch — the no-egress posture forbids one and a regulatory table is not a thing to
re-download unattended.

## What it costs to advertise

Chemclaw3 pays every bound tool's schema on every model call, and its
`tests/test_context_floor.py` bounds what this fleet publishes. Measured there on 2026-09-15, this
server is **4,471 tokens over 7 tools** — 639 a tool, above the band its siblings occupy
(`thermalsafety` 538, `safety` 544, `props` 489, `chem` 465).

The server is not uniformly verbose; it has one composite. Six of the seven tools cost 458–614,
squarely in that band. `system_suitability_report` costs **1,171** — 334 of description and 837 of
schema, of which 309 is the nested `PeakInput` model and the rest is nine named criteria.

That one is kept at that price deliberately. Its alternative is the model decomposing a pasted
table itself across the single-peak tools, which it can do — but that means pairing widths with
retention times across rows and calling resolution on *adjacent* pairs, and a resolution computed
between the wrong two peaks looks exactly like a correct one. The bookkeeping is the part a model
gets wrong silently, which is the case for spending the tokens rather than the case against.

## Concurrency

No `engine/admission.py`, and the exemption is argued with its measurement in
`tests/test_fleet.py::CEILING_IS_ARGUED_ABSENT` — 1.9 µs to 4.9 µs for the six single-peak tools
and 29.8 µs for the report, most of which is pydantic building the result model. No subprocess, no
pinned thread. The two list inputs are bounded (`MAX_INJECTIONS`, `MAX_PEAKS`) so the cost cannot
run away unpriced, which is a different bound from a concurrency ceiling and the one this server
actually needs.
