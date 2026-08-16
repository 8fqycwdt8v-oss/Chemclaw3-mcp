"""Every knob the calculators read, in one settings object — and half of them are cache-key input.

Ported from Chemclaw3's `chemclaw/core/config/calculators.py`. Two things about it are
load-bearing rather than tidy:

**The env prefix and every field name are Chemclaw3's, exactly.** `CHEMCLAW_PKA_UNCERTAINTY`,
`CHEMCLAW_XTB_METHOD`, `CHEMCLAW_XTB_ENGINE` mean here what they mean there. That is not
politeness: seven of these values are interpolated into `pka.calc_version()`, one into
`solubility.calc_version()`, and `calc_version` is the primary key of Chemclaw3's calibration
ledger (`predictions`, unique on `(calc_type, calc_version, input_hash)`, matched exactly with no
version pooling). A deployment that configured this server's pKa calibration differently from the
one the ledger was filled under would produce rows nothing reconciles against — silently, because
`calculator_trust("pka")` reports `UNCALIBRATED` rather than an error. Same variable name on both
sides is what makes "configure them identically" a thing an operator can actually do.

**Defaults are the values Chemclaw3 ships**, character for character, including the fitted
calibration constants. Changing one here is a scientific decision, not a deployment tweak, and it
moves `calc_version` — which is the point of it being in the version string at all.

What was left behind, deliberately: every setting that governed the calculation cache, the
artifact store, the durable-job timeouts or the calibration ledger. This server computes on request
and stores nothing, so those knobs would be configuration in appearance only — the failure mode
this repository's own conventions name.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class CalcSettings(BaseSettings):
    """The fast local calculators: xTB, the pKa predictor, the solubility model, logD.

    Grouped because these knobs define the calculators' *scientific* parameters, and most of them
    enter `calc_version` or `params_hash` — changing one is a deliberate recompute on the
    Chemclaw3 side, never a silent drift.
    """

    model_config = SettingsConfigDict(env_prefix="CHEMCLAW_", extra="ignore")

    # Which backend runs an xTB task. "tblite" is the in-process library; "xtb" is the binary,
    # which brings ANCopt (measured 9-11x faster on drug-sized molecules) and GFN-FF. "auto"
    # prefers the binary when it is installed and falls back, so an image without it still works —
    # the *resolved* name goes into `calc_version`, never "auto", so two deployments never share a
    # ledger row they disagree on.
    xtb_engine: Literal["auto", "tblite", "xtb"] = "auto"
    xtb_binary: str = "xtb"
    # Numerical accuracy passed to the binary (xtb's `--acc`; lower is tighter) and the wall-clock
    # ceiling on one invocation.
    xtb_cli_accuracy: float = 1.0
    xtb_cli_timeout_seconds: int = 3600
    # xtb's optimization convergence level. "vtight" (2e-4 Hartree/Bohr) is the first one that
    # satisfies `xtb_opt_gradient_tolerance`; the default "normal" stops around 1e-3 and the
    # geometry is then rejected by our own check, which wastes the run.
    xtb_cli_opt_level: str = "vtight"
    # Threads for the binary and its OpenMP runtime. 0 leaves xtb's own default, which uses the
    # machine — correct for a dedicated pod, and worth measuring before changing: pinning to 1 cost
    # a factor of ~4 on a 76-atom Hessian.
    xtb_cli_threads: int = 0
    # The GFN parametrization, and the RDKit embedding seed that fixes the 3D geometry so results
    # are reproducible. Both are part of the key: the method through `calc_version`, the seed
    # through the coordinates it produces (and, for pKa, through `params`).
    xtb_method: str = "GFN2-xTB"
    xtb_embed_seed: int = 42
    # Decimal places coordinates are rounded to before a `Structure` is hashed. 4 decimals = 0.1
    # pm, far below any chemical significance, so run-to-run float noise cannot fork the id; it is
    # part of `structure_id`, so changing it re-addresses every structure.
    xtb_geometry_decimals: int = 4
    # Wiberg bond order above which a pair of atoms is reported as bonded. 0.5 keeps real bonds (a
    # single bond is ~1.0) and drops the long-range tail.
    xtb_bond_order_threshold: float = 0.5
    # Default number of atoms a site-reactivity ranking reports. Enough to see the ordering of a
    # ring plus its substituents without flooding the agent's context.
    xtb_fukui_top_n: int = 15
    # Geometry optimization. Convergence is on the largest absolute gradient component in
    # Hartree/Angstrom; 5e-4 is ~2.6e-4 Hartree/Bohr, tighter than xtb's own "normal" setting
    # because the finite-difference Hessian is only as clean as the stationary point under it.
    xtb_opt_gradient_tolerance: float = 5e-4
    xtb_opt_max_steps: int = 1500
    # Trust radius (Angstrom): the furthest one Cartesian coordinate may move in a single bounded
    # L-BFGS-B leg. Without it the optimizer's first step on a strained geometry is large enough to
    # collapse a bond and leave the SCF unconvergeable.
    xtb_opt_trust_radius: float = 0.35
    # Curvature (Hartree/Angstrom^2) assumed for the directions the ANC preconditioner's pairwise
    # model cannot see — bends and torsions, which on ibuprofen is 37% of them. Not a safety floor:
    # it is the stand-in for the missing terms, and the true Hessian's median curvature is ~0.4.
    # Swept against measured step counts, it optimizes near 1.0 and turns over by 1.5.
    xtb_anc_curvature_floor: float = 1.0
    # Central-difference step for the Hessian, in Angstrom. Small enough that the harmonic
    # approximation holds, large enough that the gradient difference is well above the SCF's own
    # numerical noise.
    xtb_hessian_displacement: float = 0.005
    # Atom-count ceiling for a Hessian. Cost is 6N gradient evaluations, so this is an absolute
    # practicality limit. **It bites harder here than it did in Chemclaw3**: over there the refusal
    # named the durable QM job path as the alternative, and this server has no durable path — so
    # the message says so instead of naming a route that does not exist from here.
    xtb_hessian_max_atoms: int = 150
    # **CREST sampling temperature, and the only survivor of the thermochemistry block.** It keeps
    # Chemclaw3's name and value because it keeps Chemclaw3's *meaning*: it is passed to `crest
    # --temp`, so it changes what the search samples and therefore belongs in an ensemble's key. The
    # seven settings that stood beside it — pressure, the quasi-RRHO cutoff, the imaginary-mode
    # threshold and kick, the refinement attempt count, the reported free-energy uncertainty and the
    # IR band count — were all read by the RRHO arithmetic, which stayed in Chemclaw3 along with
    # `compute_thermochemistry`. A setting with no reader is configuration in appearance only, so
    # they are gone rather than kept for symmetry.
    xtb_thermo_temperature_k: float = 298.15
    # Maximum points in a relaxed scan. **Read by nothing on this server**, deliberately: a scan is
    # a sweep and this server exposes only the *point*, so the bound belongs to whoever writes the
    # loop. It is named here rather than silently dropped because a reader looking for it should
    # find out where it went — `chemclaw.science.calc.xtb_scan.ScanSpec` enforces it, in the
    # repository that owns the sweep.
    #
    # CREST sampling. GPL-3.0 and optional: absent, the ensemble primitives refuse and everything
    # else works. `crest_effort` is the default search depth and the timeout is generous because
    # this is the most expensive calculation this server can run.
    crest_binary: str = "crest"
    crest_effort: Literal["quick", "normal", "extensive"] = "quick"
    crest_threads: int = 0
    crest_timeout_seconds: int = 14400
    # xTB-based pKa predictor: pKa from the GFN2-xTB solvated (ALPB) deprotonation energy via a
    # linear calibration pKa = slope*dE + intercept. Defaults fitted over 10 reference O-H acids
    # (R^2 0.93, residual ~1.6 pKa units). **All four are interpolated into `pka.calc_version()`.**
    pka_solvent: str = "water"
    pka_calibration_slope: float = 0.28733
    pka_calibration_intercept: float = -29.3116
    pka_uncertainty: float = 1.6
    # Conjugate-acid pKa of a **base**, its own calibration. Fitted over seven aromatic/aryl-
    # nitrogen references spanning pKa 1.0-6.95: Spearman 1.000, R^2 0.993, in-sample RMSE 0.17.
    # The reported uncertainty is deliberately far above that RMSE — a two-parameter fit on seven
    # points does not support a tighter out-of-sample claim. Aliphatic amines are refused rather
    # than calibrated. Also interpolated into `pka.calc_version()`.
    pka_base_calibration_slope: float = 0.241396
    pka_base_calibration_intercept: float = -22.1843
    pka_base_uncertainty: float = 1.0
    # Reported log-S RMSE of the ESOL solubility model: the uncertainty attached to every
    # prediction, and part of `solubility.calc_version()` for the same reason — re-tuning it
    # changes what the stored number means.
    solubility_rmse_log: float = 0.75
    # logD: the working pH used when a caller does not name one. 7.4 (physiological pH) is the
    # conventional analytical-chemistry default.
    logd_default_ph: float = 7.4
    # The ionised fraction of the *one* site the pKa predictor reports, at or below which further
    # unmodelled sites of the same kind can still be dismissed; above it logD refuses rather than
    # report a single-equilibrium number for a polyprotic molecule.
    #
    # The bound is arithmetic, not taste. The predictor reports the *most* ionisable site, so with
    # r = f/(1-f) its ionisation ratio, every other site's is at most r, and the species sum the
    # single term omits is bounded by the geometric series: the neglected shift is at most
    # -log10(1 - r**2). At f = 0.05 that is 0.0012 log units, three orders below the +/-1.6 the
    # result already carries.
    logd_negligible_ionised_fraction: float = Field(default=0.05, gt=0, lt=0.5)


# One instance for the process. Read at import by nothing that matters — every consumer reads
# through `settings.<field>` at call time, so a test may monkeypatch an attribute and see it apply.
settings = CalcSettings()
