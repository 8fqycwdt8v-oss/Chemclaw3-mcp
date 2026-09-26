# `kinetics` — isothermal rate and ideal-reactor arithmetic

Six read-only tools on **port 8852**. Every input is a kinetic parameter the chemist already has;
every answer carries the equation it came out of and the idealisation that equation makes.

```sh
make run-kinetics      # 127.0.0.1:8852, dev token
```

## What it serves

| Tool | Answers |
| --- | --- |
| `rate_constant_at_temperature` | Carry a measured `k` to another temperature along Arrhenius |
| `activation_energy_from_two_rates` | `E_a` and `ln A` from two measured points — exact algebra |
| `batch_conversion_after` | Conversion in an ideal batch reactor after a given time, any order |
| `batch_time_to_reach` | The exact inverse: time to reach a given conversion |
| `continuous_reactor_conversion` | Ideal PFR **and** ideal CSTR at the same residence time, side by side |
| `semibatch_accumulation_profile` | Unreacted dosed reagent through a constant-rate addition |

## What it is not

- **It fits nothing.** No regression, no time-course data, no goodness of fit. "Here is my
  concentration-against-time data, what is the rate law" is a question this server refuses. See
  *The tool that is not here* below.
- **Every reactor is ideal and isothermal.** Perfect mixing, no dispersion, no residence-time
  distribution, no mass-transfer limitation, no energy balance. What comes back is what the
  *kinetics* predict, not what a vessel will do — and the difference is usually what a scale-up
  problem is made of.
- **It is not thermal safety.** No TMR, no T_D24, no criticality class, no adiabatic rise. That is
  `servers/thermalsafety`, and the boundary is narrow enough to be worth stating precisely: *that*
  server also extrapolates along Arrhenius, but of `q`, the specific heat-release rate of a
  **decomposition**, inside a TMR_ad inversion, returning a temperature. This one extrapolates `k`,
  the rate constant of the reaction being **run**, and returns a rate constant. A decomposition's
  apparent `E_a` from a DSC and a synthesis reaction's `E_a` from a kinetic study are different
  numbers about different processes, and keeping the tools apart is what stops one being quoted as
  the other. `tests/test_arrhenius.py` asserts this module exports no name containing *tmr*,
  *criticality*, *adiabatic* or *runaway*.

  The one honest bridge: `semibatch_accumulation_profile`'s peak is an **input** to a thermal
  question, not an answer to it. It says how much unreacted reagent is present; what temperature
  that would reach if cooling failed is calorimetry.

## Two things worth knowing before reading a number

**A CSTR is much worse than a PFR, and the size of it surprises people.** At 90% conversion in
first order an ideal stirred tank needs **3.9×** the residence time of an ideal plug-flow reactor.
That is structural, not a coefficient: a perfectly mixed tank sits entirely at its *outlet*
composition — the lowest concentration in the system, therefore the slowest rate — while a plug of
fluid in a PFR sees the whole concentration profile, exactly as a batch reactor does over time.
**A PFR is a batch reactor moving down a pipe**, and this server computes it that way rather than
re-deriving the integral, which is why the two can never disagree.

**Accumulation is why "add it slowly" is a safety decision.** The unreacted dosed reagent present at
any instant is the material a cooling failure would have to absorb all at once. The peak of that
profile is the case a dose time is chosen against, and it is the reason the semi-batch tool exists.

## The tool that is not here, and what it would cost

`MODULES.md` proposed this server with five tools and named its offline source as **"Cantera
(BSD-3) + SciPy, both installed at build time"**. It ships with four of those five, plus one the
proposal did not list (`batch_time_to_reach`), and with **neither dependency**.

The missing one is `fit_rate_law` — a regression over real time-course data — and splitting it out
is what let everything else stay at `math`:

- **The precedent is in the server next door.** `thermalsafety` hand-rolled a 200-step bisection
  rather than call `scipy.optimize.brentq`, with the reason in a comment beside its gas constant:
  a server is a dependency closure as much as a capability. `props` made the same call before it.
- **Only two servers in this fleet carry numpy or scipy**, and neither reason transfers: `calc`
  does real numerics behind a QM binary, and `pyexec` *is* a sandbox whose whole product is
  shipping that toolbox to the agent. Closed-form reactor algebra and a fixed-step RK4 over two
  state variables are not that.
- **The probes do not carry fitting data anyway.** Chemclaw3's `process-chemistry.yaml` asks three
  kinetics questions; one references a dataset without pasting it, and the other two supply a
  single number each. The tools that answer them are the closed-form ones.
- **Cantera was never the right dependency.** It is a gas-phase mechanism package, and this is
  liquid-phase process chemistry.

**What would reopen it:** a real time-course arriving through an attachment or an ELN record, which
is the shape the probes imply and which nothing in this family delivers yet. At that point the
question is whether `fit_rate_law` belongs here — making this image the third-heaviest closure in
the fleet — or in its own server, and whether the lockfile can express Cantera under
`uv export --frozen --require-hashes`, which `docs/BACKLOG.md` already records one image failing.

## What it reads

Nothing. There is no `data/` directory and no corpus to refresh. Every number is a physical
constant or a definition.

The readiness probe therefore checks **relations the implementation does not contain**, which is
what makes it a check rather than a restatement:

- the textbook `τ_CSTR/τ_PFR` = 3.909 at 90% first-order conversion, which falls out of the two
  reactor models being *different* and so cannot be satisfied by both being wrong the same way;
- the closed-form batch inverse round-tripping to machine precision at five orders;
- the semi-batch integrator's **convergence order**;
- the rate doubling per 10 °C at about 53 kJ/mol.

**The third one has already earned its place.** The first version of the integrator guarded its feed
term with `time < dose_time_seconds`, so the final RK4 step's `k4` stage alone saw the feed switched
off — one step of O(h) error inside an O(h⁴) scheme. It dropped the integrator to *first-order*
convergence and reported the peak accumulation **low** (0.047856 against 0.047940), which is the
dangerous direction for a number a dose time is chosen from. The answer was still right to three
significant figures, so no absolute tolerance would have caught it. Only the rate did.

## Concurrency

`semibatch_accumulation_profile` is gated by `engine/admission.py`
(`CHEMCLAW_KINETICS_MAX_CONCURRENT_INTEGRATIONS`, default 3) and runs off the event loop. This
section used to argue the server out of a ceiling at 836 µs per call, which held while the step
count was fixed; once `_steps_for_stability` derived it from the caller's rate and dose time the
worst legal call became seconds of pure-Python CPU, and the tool was also running *on* the event
loop, stalling `/healthz` for as long as it ran. The five closed-form tools stay ungated.
`D-2026-09-26-a-tool-that-runs-on-the-event-loop-cannot-be-gated` has the measurement and why the
integrator was not replaced instead.
A profile keeps at most `reactors.PROFILE_POINTS` points — evenly spaced samples, the end of the
dose and the exact peak — so an integration's memory does not grow with its step count.
