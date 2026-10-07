"""Every knob the calculators read, in one settings object — and half of them are cache-key input.

The env prefix, field names and defaults are Chemclaw3's exactly (from
`chemclaw/core/config/calculators.py`), because several values enter `calc_version`, the primary
key of Chemclaw3's calibration ledger: a differently configured server would write rows nothing
reconciles. Changing a scientific default is a scientific decision that moves `calc_version`.
Settings for the cache, artifacts, durable jobs and ledger stay in Chemclaw3.
"""

from __future__ import annotations

from typing import Literal

from mcp_server_kit.limits import report_settings
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Every budget here is its Chemclaw3 caller's bound less this margin, so this server's worded
# refusal arrives before the caller's own timeout (whose clock starts earlier). The binding term is
# granularity: `budget.Deadline` is checked between single points, and one single point at the atom
# ceiling takes ~80 s. Raising `xtb_max_atoms` needs a re-measurement.
_CALLER_MARGIN_SECONDS = 120


class CalcSettings(BaseSettings):
    """The calculators' settings: xTB, the pKa predictor, the solubility model, logD.

    Most enter `calc_version` or `params_hash`, so changing one is a deliberate recompute.
    """

    model_config = SettingsConfigDict(env_prefix="CHEMCLAW_", extra="ignore")

    # Backend for an xTB task: "tblite" (in-process), "xtb" (the binary), or "auto" (binary if
    # installed). The *resolved* name enters `calc_version`, never "auto".
    xtb_engine: Literal["auto", "tblite", "xtb"] = "auto"
    xtb_binary: str = "xtb"
    # The binary's `--acc` (keyed, via `XtbSpec.accuracy`: it changes the numbers) and its per-call
    # wall clock (not keyed: it decides only whether an answer arrives).
    xtb_cli_accuracy: float = 1.0
    # Chemclaw3's `calc_atomic_timeout_seconds` less `_CALLER_MARGIN_SECONDS`.
    xtb_cli_timeout_seconds: int = 3600 - _CALLER_MARGIN_SECONDS
    # The binary's convergence level; "vtight" is the first that meets `xtb_opt_gradient_tolerance`.
    # Keyed via `OptSpec.opt_level`.
    xtb_cli_opt_level: str = "vtight"
    # Threads for the binary; 0 keeps xtb's default (the whole machine).
    xtb_cli_threads: int = 0
    # The GFN parametrization and the embedding seed; both are in the key (via `calc_version` and
    # the
    # coordinates they produce).
    xtb_method: str = "GFN2-xTB"
    xtb_embed_seed: int = 42
    # Decimals coordinates are rounded to before hashing (0.1 pm), so float noise cannot fork a
    # `structure_id`; changing it re-addresses every structure.
    xtb_geometry_decimals: int = 4
    # Wiberg bond order above which atoms are reported bonded. Keyed via
    # `PropertiesSpec.bond_order_threshold`, since it filters the answer.
    xtb_bond_order_threshold: float = 0.5
    # Largest gradient component for convergence, in Hartree/Angstrom: tighter than xtb's "normal"
    # because the finite-difference Hessian is only as clean as the stationary point.
    xtb_opt_gradient_tolerance: float = 5e-4
    xtb_opt_max_steps: int = 1500
    # Ceiling (Angstrom) on one optimiser step (geomeTRIC's `tmax`), so a strained first step cannot
    # collapse a bond.
    xtb_opt_trust_radius: float = 0.35
    # Central-difference Hessian step, in Angstrom: harmonic yet above SCF noise.
    xtb_hessian_displacement: float = 0.005
    # Atom ceiling for a Hessian (6N gradients). Chemclaw3 refuses first at the same count; this is
    # the
    # backstop for other callers.
    xtb_hessian_max_atoms: int = 150
    # Atom ceiling for any structure, enforced by `Structure`. It bounds memory, not time:
    # geomeTRIC's coordinate system is quadratic in atoms, and `calc_max_concurrent_requests`
    # relaxations
    # at the ceiling must fit the Deployment's memory limit. Derived by `tests/test_cost_bounds.py`,
    # and set a margin below the derived bound.
    xtb_max_atoms: int = 450
    # Wall clock (seconds) on one in-process calculation — the optimiser and finite-difference
    # Hessian
    # the shipped tblite image runs — so an abandoned call stops burning CPU. `connector.yaml`'s
    # `request_timeout` less `_CALLER_MARGIN_SECONDS`; raise both together.
    xtb_inline_timeout_seconds: float = 900.0 - _CALLER_MARGIN_SECONDS
    # How much runs at once, in slots of one core, refused past it. Not keyed. 4 is the pod's core
    # count: an in-process calculation costs one slot (`OMP_NUM_THREADS=1`), a CREST search
    # `crest_threads`. Chemclaw3's `calc_backend_max_concurrent_requests` counts requests, so this
    # is the
    # backstop. See `engine/admission.py`.
    calc_max_concurrent_requests: int = Field(default=4, ge=1)
    # CREST sampling temperature, passed to `crest --temp` and therefore keyed. The rest of the
    # thermochemistry settings live with the RRHO arithmetic in Chemclaw3.
    xtb_thermo_temperature_k: float = 298.15
    # CREST sampling: GPL-3.0 and optional — absent, the ensemble primitives refuse and everything
    # else
    # works. (The scan point limit lives in Chemclaw3's `ScanSpec`, which owns the sweep.)
    crest_binary: str = "crest"
    crest_effort: Literal["quick", "normal", "extensive"] = "quick"
    crest_threads: int = 0
    # Chemclaw3's `calc_sampling_timeout_seconds` less the margin; on expiry the sampler's process
    # group is killed and the refusal still reaches the caller.
    crest_timeout_seconds: int = 14400 - _CALLER_MARGIN_SECONDS
    # Atom ceiling for perceiving an ensemble member's SMILES; above it the member is unlabelled.
    crest_perceive_max_atoms: int = 150
    # pKa = slope*dE + intercept on the ALPB deprotonation energy, fitted on 10 O-H acids (R^2 0.93,
    # residual ~1.6 units). All four values enter `pka.calc_version()`.
    pka_solvent: str = "water"
    pka_calibration_slope: float = 0.28733
    pka_calibration_intercept: float = -29.3116
    pka_uncertainty: float = 1.6
    # Conjugate-acid pKa of a base: its own calibration over seven aromatic/aryl-nitrogen references
    # (pKa 1.0-6.95). The reported uncertainty is far above the in-sample RMSE; aliphatic amines are
    # refused. In `pka.calc_version()`.
    pka_base_calibration_slope: float = 0.241396
    pka_base_calibration_intercept: float = -22.1843
    pka_base_uncertainty: float = 1.0
    # ESOL's log-S RMSE: every prediction's uncertainty, and in `solubility.calc_version()`.
    solubility_rmse_log: float = 0.75
    # logD: the working pH used when a caller does not name one. 7.4 (physiological pH) is the
    # conventional analytical-chemistry default.
    logd_default_ph: float = 7.4
    # Ionised fraction of the reported site at or below which other sites can be dismissed; above it
    # logD refuses. With r = f/(1-f), the neglected shift is at most -log10(1 - r**2): 0.0012 log
    # units at f = 0.05.
    logd_negligible_ionised_fraction: float = Field(default=0.05, gt=0, lt=0.5)


# One instance for the process. Read at import by nothing that matters — every consumer reads
# through `settings.<field>` at call time, so a test may monkeypatch an attribute and see it apply.
settings = CalcSettings()
# Report every resolved number for `/healthz`, scientific constants included.
report_settings(settings)
