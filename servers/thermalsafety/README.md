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

**No vendored corpus.** There is no `data/` directory and no `dataset.json`: every number here is
closed-form arithmetic over the standard library's `math`, and the atomic weights behind the oxygen
balance come from the `molmass` distribution (BSD-3, no required dependencies) pinned in `uv.lock`.
There is nothing to refresh on a schedule.

It still has a readiness probe, and the probe **runs the arithmetic**: `engine/selftest.py` puts
published explosives oxygen balances and hand-computed runaway cases through the real public
functions and refuses if any answer has moved — a transposed digit in a band boundary or a weight
table is exactly the failure a corpus checksum exists to catch, and being in Python rather than a CSV
makes it harder to corrupt, not impossible. `/healthz` names what it verified as
`thermalsafety-constants@<revision>+molmass-<version>`, so two pods built against different
`molmass` releases are told apart. `tests/test_server.py` drives the probe with a wrong weight and
reads the 503 back.

`tests/test_no_egress.py` earns the positive half of the offline claim cheaply here: there is
nothing that *could* be fetched lazily, so running one of each kind of calculation with the guard
armed is the whole proof.

## Who refreshes it

Nobody, and there is nothing to refresh. The formulas are textbook — Stoessel, *Thermal Safety of
Chemical Processes* (Wiley, 2008) for the criticality classification and MTSR; Townsend & Tou,
*Thermochimica Acta* 37 (1980) 1–30 for TMR_ad; Semenov's heat balance for the critical ambient;
Bretherick's *Handbook of Reactive Chemical Hazards* (8th ed., §2.3.3) for the oxygen-balance
screening bands. A change to any of them — or a `molmass` bump, which `uv.lock` makes deliberate —
is a change to the code, in a reviewed pull request, with
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

## Operating it

Build, deploy, wiring and the fleet-wide variables are in
[`docs/operations.md`](../../docs/operations.md); what is particular to this server:

| | |
| --- | --- |
| Port / Service | 8851 / `chemclaw-mcp-thermalsafety` |
| Token | `CHEMCLAW_THERMALSAFETY_TOKEN` |
| Chemclaw3 | connector `thermalsafety`, declared there with `default_enabled: false` — enable it with `connectors.thermalsafety.enabled: true` |
| Pod | requests 250m / 256Mi, limits 1 CPU / 512Mi; 2 → 4 replicas on CPU |
| Own knobs | none |
| Readiness | `/healthz` runs the self-test above; a moved answer is a 503 |
| Admission | none (see "Cost") |
