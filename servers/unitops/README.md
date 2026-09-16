# `unitops` — scale-up and unit-operation sizing

Seven read-only tools on **port 8853**. Every input is equipment or material data the chemist
already has; every answer carries the correlation it came out of and the assumption that
correlation makes.

```sh
make run-unitops       # 127.0.0.1:8853, dev token
```

## What it serves

| Tool | Answers |
| --- | --- |
| `agitation_scale_up` | P/V and tip speed at both scales, and the large-scale speed that matches **each** criterion |
| `just_suspended_speed` | Zwietering's `N_js`, and the tip speed and P/V running there implies |
| `heat_transfer_time_constant` | `τ = M·c_p/(U·A)` for a jacketed batch, the duty at the starting gap, and either inversion |
| `shortcut_distillation` | Fenske `N_min`, Underwood `R_min`, Gilliland `N` at a chosen reflux |
| `crystallisation_yield` | The maximum yield two measured solubilities permit, and the liquor loss |
| `filtration_time` | Constant-pressure cake filtration time, split into cake and medium |
| `drying_time` | Batch drying through the constant-rate and falling-rate periods |

## What it is not

- **It holds no data.** No vessel register, no VLE table, no solubility curve, no cake resistance,
  no drying curve, no impeller catalogue. Every tool refuses to default the measured number its
  answer is made of — `U`, `alpha`, `S`, `N_c`, the two solubilities, the specific cake resistance — and
  most of them will not run without it. That is the whole of what separates a useful answer here
  from a fabricated one, so a question naming only a vessel is one this server cannot answer.
- **Nothing here is a simulation.** The shortcut column is not stage-by-stage and assumes one
  relative volatility over the whole column; Zwietering's `N_js` is a 1958 correlation with a fitted
  geometry constant, quoted at roughly ±10% on its own data — a literature figure rather than
  one measured here; the crystallisation yield is an
  equilibrium **maximum** a real batch comes in under; the cake filtration assumes an
  **incompressible** cake, which fine organic solids routinely are not.
- **It is not thermal safety.** No TMR, no MTSR, no T_D24, no criticality class, no adiabatic rise.
  The boundary is narrow enough to state precisely: `heat_transfer_time_constant` returns a
  **capacity** — how fast this jacket moves heat at a stated driving force — and putting a process
  heat **load** against it needs calorimetry, which is `servers/thermalsafety`'s input and exists
  nowhere in this family. `tests/test_tools.py` asserts this module exports no name containing
  *tmr*, *mtsr*, *criticality*, *adiabatic*, *runaway* or *sadt*.
- **It computes no wash and predicts no solubility.** A wash volume needs a displacement efficiency
  that is a measured property of the cake; a solubility needs a curve nothing in this family holds,
  and Chemclaw3's own predictor is aqueous, neutral-species and temperature-free. Both absences are
  asserted rather than described.
- **It does not reintroduce a heavy tier.** Nothing here shells out to anything or dials anything.

## Why seven tools rather than the six the catalogue implied

`MODULES.md` named six capability areas and no tool names, so the surface was a choice. Five of the
six map one-to-one. **Mixing became two**, and the split is the one substantive design decision in
this server:

- `agitation_scale_up` is **definitions** — `P = N_p·rho·N³·D⁵` and `π N D` — exact given the power
  number, and it needs only geometry, speed, density and viscosity.
- `just_suspended_speed` is a **fitted correlation** needing four things the first one never asks
  for: a particle size, a crystal density, a solids loading and the geometry constant `S`.

Merged, a chemist who wants a P/V would have to supply a particle size distribution to get one, or
the suspension half would have to be optional — which means half-answering silently. Worse, one
answer would put an exact number beside a ±10% one with nothing saying which was which. Chemclaw3's
`pc-12` asks for all three in a single sentence ("what tip speed and P/V should I match, and will
the solids stay suspended?"), and the honest way to serve that question is two tools whose separate
input lists say what each one actually needs.

## Two things worth knowing before reading a number

**The two scale-up criteria disagree, and this server will not choose between them.** Equal `P/V`
and equal tip speed cannot both be held at a new diameter: scaled up, matching P/V always asks for
the higher tip speed and matching tip speed always gives away P/V. Which to hold depends on what is
limiting — dispersion, mass transfer and heat transfer follow P/V; shear-sensitive crystals and
gas-liquid surfaces follow tip speed. Both come back, with the ratio between them.

**Suspension gets *cheaper* per unit volume on scale-up, which is not the intuition.** `N_js ∝
D^-0.85`, so at geometric similarity the P/V needed to keep a solid off the base falls as `D^-0.55`.
A scale-up argued on equal P/V is therefore conservative for suspension — and the same argument says
nothing reassuring about attrition, since running well above `N_js` breaks crystals, which is a
common reason a filtration slows down between batches.

## What it reads

Nothing. There is no `data/` directory and no corpus to refresh. Every number is a physical
constant, a definition, or one of five published exponents.

The readiness probe therefore checks **relations the implementation does not contain**, which is
what makes it a check rather than a restatement:

- a column at **total reflux reducing to Fenske**, which ties Gilliland's empirical correlation to
  algebra it shares no line of code with;
- Underwood's **bisected** root reproducing the closed-form binary minimum reflux
  `R_min = [x_D/z - alpha(1-x_D)/(1-z)]/(alpha-1)`, which is written only in the probe;
- Gilliland monotonic in reflux, and never below `N_min`;
- a crystallisation yield against the saturated-charge closed form `1 - (S_cold/S_hot)(1 - E/W)`;
- the **63.2%** first-order thermal approach at `t = τ`, and the `τ·ln 2` half-life;
- the `N₂ = N₁(D₁/D₂)^(2/3)` similarity rule, which `mixing.py` deliberately does not use;
- Zwietering's exponents being **dimensionally homogeneous** — the metre exponents cancelling and
  the second exponents summing to -1 — *and* exhibited by the function as log-log slopes;
- a cake filtration reporting its own derivative, checked by a central difference;
- the two drying periods meeting at the critical moisture.

**The dimensional check is the one that earns its place.** The only table this server holds is five
exponents transcribed from a paper, and the realistic failure is two of them swapped. That returns a
plausible `N_js` in the right order of magnitude — no tolerance on the value would catch it — and it
stops the correlation being a frequency, which the probe does catch.
`tests/test_server.py::test_the_readiness_probe_refuses_when_zwieterings_exponents_are_transposed`
drives exactly that and reads the status back.

## What was validated, and what could not be

Measured on 2026-09-16, and each expected value written from the published form **before** it was
compared with this code:

- **Fenske, Underwood and Gilliland** on a benzene-toluene-style binary — alpha = 2.5, 95/5 split,
  equimolar saturated-liquid feed. By hand: `N_min = ln 361/ln 2.5 = 6.4269`, `θ = 1.42857`,
  `R_min = 1.100` (agreeing to 1.3e-15 with the closed-form binary shortcut, which the module does
  not contain), and `N = 14.42` at 1.3·R_min. The arithmetic is in `tests/test_distillation.py`.
- **Impeller power and tip speed** against hand arithmetic, and the P/V match against the
  similarity rule to 1e-12.
- **Crystallisation, filtration and drying** against hand-computed balances and against the closed
  forms in `tests/test_isolation_ops.py`.
- **The heat-transfer identities** to 1e-9 or better.

**Zwietering could not be checked against a published worked example**, and saying so is more useful
than an assertion nobody can trace: no such example exists in this repository, and the geometry
constant `S` is supplied by the caller rather than held here. What *is* checked is everything a
published correlation must satisfy whatever its constants are — dimensional homogeneity, each
exponent exhibited by the function, and the `D^-0.55` scale-up consequence the literature states —
which is structure rather than an absolute speed.

## Concurrency

No `engine/admission.py`, and the exemption is argued with its measurement in
`tests/test_fleet.py::CEILING_IS_ARGUED_ABSENT`. Measured 2026-09-16, engine CPU per call:
**1.4 µs** (`crystallisation_yield`) to **12.2 µs** (`shortcut_distillation`, whose 200-step
Underwood bisection is the widest thing here), and 3.4-14.0 µs through the pydantic result models.
A whole call over a real MCP session on loopback is **8.49 ms** median, so about 0.1% of a call is
this server's own work. There is no subprocess, no pinned thread, no list input and no unbounded
loop — the one solver runs at a fixed step count — so there is nothing for a ceiling to bound.

## The tool that is not here

**A compressible cake filtration.** Real organic cakes compress, so `alpha` rises with pressure and the
incompressible model overstates what pushing harder buys. Modelling it means `alpha = alpha₀·ΔPˢ` with `s`
**fitted over filtration tests at several pressures** — a regression over data nobody in this family
has, and a server that shipped a default `s` would be inventing the compressibility rather than
measuring it. The honest version is a tool that takes `s` as an input alongside `alpha₀`, and that is
worth building the day a site's filtration tests start arriving through an ELN. Until then the
docstring says what the assumption costs and in which direction.
