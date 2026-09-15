# `thermalsafety` — runaway and thermal-hazard arithmetic · port 8851

The calculations behind a safe scale-up, done on numbers a chemist measured. Adiabatic temperature
rise, MTSR, TMR_ad and the temperature at which it reaches a target (T_D24), the Stoessel
criticality class, jacket heat-removal capacity, a Semenov critical-ambient estimate for a stored
package, and a stoichiometric oxygen-balance screen.

## What it serves

| Tool | Answers |
| --- | --- |
| `adiabatic_temperature_rise` | ΔT_ad = \|ΔH\|·n/(m·c_p) — where the batch goes with every joule retained. |
| `mtsr` | T_p + X_ac·ΔT_ad — where a cooling failure takes the batch. |
| `tmr_ad` | Townsend–Tou TMR_ad at a temperature, and the temperature at which it equals a target (T_D24 at 24 h). |
| `stoessel_criticality_class` | 1–5 from the ordering of T_p, MTSR, MTT and T_D24, with the ordering returned as evidence. |
| `heat_removal_capacity` | U·A·ΔT as a total duty and per kilogram, comparable against a specific heat release rate. |
| `semenov_critical_ambient` | The ambient at which a package's heat generation overtakes its heat loss. |
| `oxygen_balance_screen` | OB% to CO₂ from a molecular formula, with its screening band. |

## What data it reads

**None.** This is the only server in the fleet with no `data/` directory and no `dataset.json`, and
that is a property rather than an omission: every number here is closed-form arithmetic over the
standard library's `math` plus a seventeen-element atomic-weight table defined in
`engine/oxygen_balance.py`. There is nothing to refresh, nothing to checksum and nobody who owns a
corpus refresh for it.

Two consequences follow, and both are asserted rather than described:

- **`app.py` passes no `readiness=` callable**, so `/healthz` is a constant 200 here where it is a
  real load check everywhere else. A probe could only verify that a module imports, which the
  process proved by starting —
  `D-2026-09-12-a-readiness-check-that-does-not-run-the-thing-is-not-a-readiness-check` is about
  exactly that shape. `tests/test_server.py` asserts the *absence*, reading `app.py` as a syntax
  tree, so whoever adds a corpus here finds that test red and decides deliberately instead of
  inheriting a probe that always says yes.
- **`tests/test_no_egress.py` earns the positive half more cheaply than any other server.** There is
  nothing that *could* be fetched lazily, so running one of each kind of calculation with the guard
  armed is the whole proof.

## Who refreshes it

Nobody, and there is nothing to refresh. The formulas are textbook — Stoessel, *Thermal Safety of
Chemical Processes* (Wiley, 2008) for the criticality classification and MTSR; Townsend & Tou,
*Thermochimica Acta* 37 (1980) 1–30 for TMR_ad; Semenov's heat balance for the critical ambient;
Bretherick's *Handbook of Reactive Chemical Hazards* (8th ed., §2.3.3) for the oxygen-balance
screening bands. A change to any of them is a change to the code, in a reviewed pull request, with
the published values in `tests/test_oxygen_balance.py` and the hand-computed ones in
`tests/test_runaway.py` as the check.

## What it is not

This is the part worth reading before calling anything here.

**It measures nothing.** There is no calorimetry model, no kinetics fit, no corpus of measured
decomposition enthalpies and no way to obtain any of them. Every input is a number from a DSC, ARC
or RC1 report that a person ran. A tool that cannot be given one **refuses rather than defaulting
it** — `mtsr` will not assume an accumulation fraction, `tmr_ad` will not assume an activation
energy — because a default here is the safety argument being invented by the calculator instead of
made by the chemist.

**`semenov_critical_ambient` is not an SADT.** A Self-Accelerating Decomposition Temperature is
*defined* by UN Test Series H on a specific package in a specific size. What this returns is a heat
balance for deciding which test to book and at what temperature to start it. It must not be quoted
as an SADT or entered on a transport document. The tool is named for the model rather than for the
regulation on purpose, and its `basis` string says so beside every number it returns —
`tests/test_server.py` asserts that the disclaimer survives serialisation, because that is where the
model reads it.

**`oxygen_balance_screen` is not a hazard classification, in either direction.** OB% is
stoichiometry: it knows nothing about whether a compound decomposes, at what temperature, or how
much energy is released. TNT is −74% and is an explosive; glucose is −107% and is food. A near-zero
value is a reason to pursue the structural alerts and the calorimetry, never a finding of its own —
and a very negative value is not a clearance. For the structural half of that question, the `safety`
server screens a structure against cited hazard-alert tables.

**A Stoessel class is not a verdict.** Classes 3 and 4 both rest on evaporative cooling being the
barrier, and the classification does not check that it is sufficient: the condenser duty, the vent
line and the scrubber at the vapour rate MTSR produces are a separate calculation this server does
not do.

**`heat_removal_capacity` is a steady-state snapshot.** It says nothing about U falling as a batch
thickens, about fouling, about jacket inertia, or about whether the service loop can sustain the
duty.

## Why it is here rather than in Chemclaw3

It is a stateless request/response calculation over arguments the caller supplies — the fleet's own
test for where a capability belongs. Nothing here has a key that names its own output, nothing here
needs a job record, and a call that is interrupted is a call the caller makes again.

It complements `servers/safety/`, which screens *structures* for hazard alerts from cited tables and
needs RDKit. This one takes calorimetry numbers and answers "what happens if the cooling fails", and
needs nothing. Keeping them apart keeps `safety`'s dependency closure out of this image and this
server's arithmetic out of that one.

## Cost, and why there is no admission ceiling

Measured end to end over a real MCP session on loopback: 6.5 ms for `adiabatic_temperature_rise`,
7.8 ms for `tmr_ad`, 7.6 ms for `oxygen_balance_screen`. The *arithmetic* inside those is 0.25 µs,
63.7 µs and 4.5 µs respectively — so under 1% of a call is this server's own work and the rest is
the streamable-HTTP round trip every server in the fleet pays.

That is why there is no `engine/admission.py` here. `servers/calc` needs one because one admitted
call is a core for minutes across forked workers; this server has no subprocess, no thread to pin
and no computation worth gating, so a ceiling would be a control with nothing to control. The bound
that does exist is on an *input*: `MAX_FORMULA_CHARACTERS` in `tools.py`, because the formula parser
is linear in its argument's length and this fleet bounds unbounded inputs rather than arguing about
which ones are affordable. The session ceiling every server inherits from `mcp_server_kit`
(`MCP_MAX_SESSIONS`) applies here unchanged.

A tool added here that grows real work must revisit both paragraphs.

## Running it

```sh
make run-thermalsafety      # 127.0.0.1:8851, dev token
```

The manifest is symlinked from [`../../manifests/thermalsafety/`](../../manifests/thermalsafety/);
registering it with Chemclaw3 is one entry in `CHEMCLAW_CONNECTOR_URLS` and no code change on either
side.
