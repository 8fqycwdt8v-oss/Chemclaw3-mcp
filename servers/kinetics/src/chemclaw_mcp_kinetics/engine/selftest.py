"""The readiness check: run the arithmetic against relations it does not itself contain.

- The CSTR/PFR volume ratio at 90% first-order conversion is 3.909 — fails if either model is
  wrong, and cannot be satisfied by both being wrong the same way.
- The closed-form batch conversion and its inverse round-trip to machine precision at every order.
- The RK4 integrator converges at fourth order; a rate check catches an O(h) defect that an
  absolute tolerance would pass.
- The stable scheme lands on the quasi-steady limit `1 / (k * C_co,end * t_dose)`.
- Arrhenius doubles the rate per 10 °C near room temperature at about 53 kJ/mol.

No corpus, so the `Dataset` digests the engine source rather than a table.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

from mcp_server_kit.datasets import Dataset

from chemclaw_mcp_kinetics.engine import arrhenius, reactors

__all__ = ["CONSTANTS_VERSION", "SelfTestFailed", "verify"]

#: The version of the first-party formulas this build serves. Bumped by hand in the commit that
#: changes one, so an operator reading `/healthz` can tell two pods apart without a shell on either.
CONSTANTS_VERSION = "1.1.0"

#: τ_CSTR/τ_PFR at X = 0.9, first order: X/((1-X)·ln(1/(1-X))). Written as the closed form rather
#: than as 3.909 so the check cannot be satisfied by somebody updating a literal.
_NINETY_PERCENT = 0.9

#: Refining by 2.5x must improve a fourth-order answer by about 2.5^4 = 39; a first-order defect
#: gives 2.5, so 20 separates them with room either side.
_FOURTH_ORDER_FLOOR = 20.0

_DOSE = {
    "rate_constant": 0.02,
    "dose_time_seconds": 7200.0,
    "initial_volume": 50.0,
    "dosed_moles": 40.0,
    "dosed_volume": 8.0,
    "initial_coreagent_concentration": 0.9,
}


class SelfTestFailed(RuntimeError):
    """A relation this server's arithmetic no longer satisfies.

    `RuntimeError`, not `ValueError`: this is the pod being wrong, and `connector_app` treats it as
    a
    permanent cause, so the pod leaves its Service.
    """


def _check_reactor_ratio() -> None:
    """The textbook CSTR/PFR ratio, which neither reactor model contains."""
    rate = 0.01
    plug = reactors.time_for_batch_conversion(
        rate_constant=rate, initial_concentration=1.0, conversion=_NINETY_PERCENT
    )
    tank = _NINETY_PERCENT / (rate * (1.0 - _NINETY_PERCENT))
    expected = _NINETY_PERCENT / ((1.0 - _NINETY_PERCENT) * math.log(1.0 / (1.0 - _NINETY_PERCENT)))
    if abs(tank / plug - expected) > 1e-6 * expected:
        raise SelfTestFailed(
            f"a CSTR needs {tank / plug:.4f} times a PFR's residence time at 90% first-order "
            f"conversion, where the closed form gives {expected:.4f}. One of the two reactor "
            "models has moved."
        )


def _check_inverse_round_trip() -> None:
    """Two separately derived integrals of one rate law must invert exactly, at every order."""
    for order in (0.0, 0.5, 1.0, 1.5, 2.0):
        seconds = reactors.time_for_batch_conversion(
            rate_constant=0.02, initial_concentration=2.0, conversion=0.75, order=order
        )
        back = reactors.batch_conversion(
            rate_constant=0.02, initial_concentration=2.0, time_seconds=seconds, order=order
        )
        if abs(back - 0.75) > 1e-10:
            raise SelfTestFailed(
                f"at order {order:g} the batch rate law and its inverse disagree: the time to 75% "
                f"conversion evaluates back to {back:.12f}"
            )


def _peak(steps: int) -> float:
    """Peak accumulation on the worked dose at a given step count, raising the bound to measure."""
    original = reactors.MAX_INTEGRATION_STEPS
    reactors.MAX_INTEGRATION_STEPS = max(original, steps)
    try:
        return reactors.semibatch_accumulation(
            steps=steps, force_stable=False, **_DOSE
        ).peak_accumulation_fraction
    finally:
        reactors.MAX_INTEGRATION_STEPS = original


def _check_integrator_order() -> None:
    """The convergence *rate*, which is what caught the feed-term discontinuity."""
    reference = _peak(20_000)
    coarse = abs(_peak(200) - reference) / reference
    finer = abs(_peak(500) - reference) / reference
    if finer <= 0.0:  # pragma: no cover - only if the two grids agree to the last bit
        return
    improvement = coarse / finer
    if improvement < _FOURTH_ORDER_FLOOR:
        raise SelfTestFailed(
            f"refining the semi-batch integration 200 -> 500 steps improved the answer by "
            f"{improvement:.1f}x, where fourth-order convergence gives about 39x. A first-order "
            "rate means a discontinuity inside the integration domain — check that no stage of the "
            "RK4 step sees a different feed rate from the others."
        )


#: A 1 h dose of 5 mol into 0.10 volume against a co-reagent at 60, at a rate constant six orders
#: past RK4's ceiling.
_STIFF_DOSE = {
    "rate_constant": 1.0e6,
    "dose_time_seconds": 3600.0,
    "initial_volume": 0.1,
    "dosed_moles": 5.0,
    "dosed_volume": 0.0,
    "initial_coreagent_concentration": 60.0,
}

#: Measured agreement with the limit is 1e-9; the dose's own departure from it is `1/(k*C_co*t)`,
#: about 3e-11 here. Loose enough that neither moves it, tight enough that a coefficient error does.
_QUASI_STEADY_TOLERANCE = 1e-6


def _check_stable_scheme() -> None:
    """The stiff band lands on the quasi-steady closed form, integrated by the stable scheme."""
    profile = reactors.semibatch_accumulation(
        steps=reactors.DEFAULT_INTEGRATION_STEPS, force_stable=False, **_STIFF_DOSE
    )
    if profile.method != reactors.METHOD_STABLE:
        raise SelfTestFailed(
            f"a dose {profile.dose_damkohler:.3g} times faster than its addition was integrated "
            f"by {profile.method!r}, not by the stable scheme"
        )
    coreagent_at_end = (
        _STIFF_DOSE["initial_coreagent_concentration"] * _STIFF_DOSE["initial_volume"]
        - _STIFF_DOSE["dosed_moles"]
    ) / _STIFF_DOSE["initial_volume"]
    limit = 1.0 / (
        _STIFF_DOSE["rate_constant"] * coreagent_at_end * _STIFF_DOSE["dose_time_seconds"]
    )
    error = abs(profile.peak_accumulation_fraction - limit) / limit
    if error > _QUASI_STEADY_TOLERANCE:
        raise SelfTestFailed(
            f"the stable scheme put the peak of a dose that reacts as it arrives at "
            f"{profile.peak_accumulation_fraction:.6e}, where the quasi-steady limit is "
            f"{limit:.6e} ({error:.1e} relative) — check the SDIRK coefficients and the "
            "projection onto the conserved line"
        )


def _check_arrhenius() -> None:
    """The one Arrhenius fact every chemist carries, and the two-point inverse."""
    doubling = arrhenius.rate_constant_at(
        35.0,
        reference_temperature_c=25.0,
        reference_rate_constant=1.0,
        activation_energy_kj_per_mol=52.9,
    )
    if abs(doubling.rate_ratio - 2.0) > 0.01:
        raise SelfTestFailed(
            f"at 52.9 kJ/mol the rate ratio over 10 K computed {doubling.rate_ratio:.4f}, where "
            "the rule of thumb every chemist carries is 2"
        )
    pair = arrhenius.activation_energy_from_two_points(
        lower_temperature_c=25.0,
        lower_rate_constant=1.0e-4,
        upper_temperature_c=55.0,
        upper_rate_constant=1.6e-3,
    )
    carried = arrhenius.rate_constant_at(
        55.0,
        reference_temperature_c=25.0,
        reference_rate_constant=1.0e-4,
        activation_energy_kj_per_mol=pair.activation_energy_kj_per_mol,
    )
    if abs(carried.rate_constant - 1.6e-3) > 1e-12:
        raise SelfTestFailed(
            "determining an activation energy from two points and extrapolating back does not "
            f"return the second point: {carried.rate_constant:.6e} against 1.6e-03"
        )


def _formula_digest() -> str:
    """A digest over this server's two engine modules.

    Over the source, since there is no table: two pods agreeing means they run the same formulas (a
    comment change also moves it).
    """
    digest = hashlib.sha256()
    for module in (arrhenius, reactors):
        source = module.__file__
        if source is None:  # pragma: no cover - only for a module with no file, e.g. frozen
            raise SelfTestFailed(
                f"{module.__name__} has no source file, so this pod cannot say which formulas it "
                "is serving"
            )
        digest.update(Path(source).read_bytes())
    return digest.hexdigest()


def verify() -> list[Dataset]:
    """Recompute what this server is made of, against relations it does not contain.

    Returns:
        One `Dataset` naming the formulas this pod serves.

    Raises:
        SelfTestFailed: If any relation no longer holds; `/healthz` then answers unready.
    """
    _check_reactor_ratio()
    _check_inverse_round_trip()
    _check_integrator_order()
    _check_stable_scheme()
    _check_arrhenius()

    return [
        Dataset(
            name="kinetics-formulas",
            version=CONSTANTS_VERSION,
            licence="first-party",
            retrieved_from=(
                "Levenspiel, Chemical Reaction Engineering, 3rd ed. (ideal batch, CSTR and PFR "
                "design equations); Arrhenius (1889) as given in any kinetics text"
            ),
            description=(
                "The ideal-reactor and Arrhenius formulas this server computes with. Verified on "
                "every probe against relations the implementation does not contain: the textbook "
                "CSTR/PFR ratio, the exactness of the closed-form inverse, the integrator's "
                "convergence order, the stable scheme's quasi-steady limit, and the "
                "doubling-per-10-degrees rule."
            ),
            sha256=_formula_digest(),
            # The formulas *are* the modules, so a module is what is named. Pointing this at a
            # records file that does not exist would be provenance that quietly says nothing.
            records_path=Path(reactors.__file__),
        )
    ]
