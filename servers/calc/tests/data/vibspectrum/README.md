# `vibspectrum` fixtures — real files, from the pinned binary

`engine/xtb_cli.py::_read_vibspectrum` is the only producer of the IR intensities Chemclaw3
publishes, and until these files existed **nothing on either side of the wire pinned the order they
come out in**. The intensities cross as a bare list, one per Cartesian mode; the consumer
(`science/calc/thermo.py::_align_intensities` over there) drops the leading `3N - n_vib` entries and
pairs the rest with its *own* projected modes by position. Two orderings satisfy the only check
anybody makes — the count — and they differ band for band:

```
zeros first        [0,0,0,0,0,0,-500,1595,3756]  ->  (-500, i1) (1595, i2) (3756, i3)
strictly ascending [-500,0,0,0,0,0,1595,3756]    ->  (-500, i2) (1595, i3) (3756, 0)
```

So the ordering is a *measurement*, not a reading of the Fortran, and these are the files it was
measured on. Every one of them was produced by the binary this image pins — `XTB_VERSION=6.7.1` in
`servers/calc/Containerfile`, `xtb version 6.7.1 (edcfbbe)` — on 2026-09-09, from the `.xyz` beside
each one:

| File | Command | What it is for |
| --- | --- | --- |
| `water-minimum.vibspectrum` | `xtb water-minimum.xyz --ohess --gfn 2` | The ordinary case: non-linear, no imaginary mode. |
| `ammonia-planar-transition-state.vibspectrum` | `xtb ammonia-planar-transition-state.xyz --hess --gfn 2` | Planar NH3, the inversion saddle: **one imaginary mode**, which is where the two candidate orderings disagree. Not a relaxed saddle — it does not need to be, because the question is where xtb writes a negative wavenumber, not what its value is. |
| `carbon-dioxide-linear.vibspectrum` | `xtb carbon-dioxide-linear.xyz --ohess --gfn 2` | **Linear**, so xtb writes *five* external modes rather than six — the other half of the pairing, and the one a `3N - 6` assumption gets wrong. |
| `carbon-dioxide-bent-one-degree.vibspectrum` | `xtb carbon-dioxide-bent-one-degree.xyz --hess --gfn 2` | The same molecule at an O-C-O angle of 179.0 degrees, where xtb stops calling it linear and writes **six** external modes. The count is not a property of the formula. |
| `water-with-raman-columns.vibspectrum` | `xtb water-minimum.xyz --raman --ptb` | The wider row format, which carries two more numeric columns before the selection rules. |

The `.xyz` files are the geometries as given to `xtb` (`--ohess` optimised ones are its own
`xtbopt.xyz`), so any of these can be regenerated rather than trusted.

**What the measurement says**, and `../../test_vibspectrum.py` is what holds it: the external modes
are written **first**, an imaginary mode is not one of them (it is the first entry *after* them),
and their number is 6 for a bent molecule and 5 for a linear one — which is what `_align_intensities`
already assumes on the other side. The second ordering above does not occur.

**The order is safe and the count is not**, which is the finding these files were made to settle and
not the one they were expected to produce. How many entries to drop is a judgement about the
*molecule*, and the two sides make it independently: xtb tests the unmassed inertia moments against
an absolute 1e-4 bohr^2 (`is_linear`, `src/axis_trafo.f90`), Chemclaw3 tests the mass-weighted
moments against a relative 1e-4. Swept over the O-C-O angle, xtb writes five external modes at
180.0, 179.99 and 179.9 degrees and **six** from 179.0 on, while Chemclaw3 still calls the molecule
linear at 179.0 and asks for 3N-5 = 4 vibrations. It therefore drops five entries of six, keeps
`[0.0, 68.71118, 0.00429, 1046.64228]`, and lands the 2593 cm^-1 asymmetric stretch's intensity on
its *third* band with a zero on its first — every band shifted by one, and nothing raised, because
`intensities.size - modes` is 5 and the check is `>= 0`. A geometry 1 degree off linear is what a
scan point and an unrelaxed embedding look like, and `compute_hessian` accepts both deliberately.
That is what `ir_wavenumbers_cm` is for: a caller that matches a band to its intensity cannot be
wrong about how many modes somebody else projected out.

**How far that reaches is one environment variable.** `Containerfile` sets
`CHEMCLAW_XTB_ENGINE=tblite`, and the in-process backend returns dipole derivatives rather than
intensities — there is no `vibspectrum` on that path at all. Everything above applies to a
deployment that selects the binary, which the same file describes as one variable away and
argues for on its own terms. That is a reason to state the blast radius, not a reason to leave
the pairing unpinned: the argument for switching is about `calc_version` and the calibration
ledger, and nothing in it would have surfaced this.

**`--raman` is not reachable through this server** — `xtb_cli.run` builds a fixed argv with no
`--raman`, `--alpha` or `--ptb` in it. That fixture is here because `_read_vibspectrum` is a parser
for the file rather than for one invocation of it, and because reading the two numbers from the
*right* of the row (which is what the code did while its own comment said it read from the left) is
correct for the narrow format and silently wrong for this one: it returns the Raman activity and the
Raman cross-section where the caller asked for the wavenumber and the IR intensity.
