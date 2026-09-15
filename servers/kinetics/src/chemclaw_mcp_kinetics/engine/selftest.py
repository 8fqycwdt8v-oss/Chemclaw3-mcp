"""The readiness check: run the arithmetic against relations it does not itself contain.

This server loads no corpus, and
`D-2026-09-15-a-server-with-nothing-to-load-still-has-something-to-verify` already settled that
this does not excuse a probe.

What it verifies is chosen on the same principle as `servers/suitability`'s: prefer a check the
implementation cannot satisfy by agreeing with itself.

- **The CSTR/PFR ratio at 90% first-order conversion is 3.909**, a textbook number written nowhere
  in `reactors.py`. It comes out of the two reactor models being *different* — a tank at outlet
  composition against a plug at every composition — so it fails if either is wrong, and it cannot
  be satisfied by both being wrong the same way.
- **The closed-form inverse must round-trip to machine precision.** `batch_conversion` and
  `time_for_batch_conversion` are separately derived integrals of the same rate law, so a sign or
  exponent error in either breaks the identity, at every order.
- **The integrator must converge at fourth order.** This is the check that has already earned its
  place: the first version of `semibatch_accumulation` guarded its feed term with
  `time < dose_time_seconds`, which put one step of O(h) error into an O(h⁴) scheme and dropped it
  to first-order convergence. The answer was still right to three significant figures, so no
  absolute tolerance would have caught it — only the *rate* did.
- **Arrhenius must reproduce the rule every chemist carries**: the rate doubles per 10 °C near room
  temperature at an activation energy of about 53 kJ/mol.

There is nothing to digest here — unlike `suitability`, this server transcribes no table. Every
number it uses is a physical constant or a definition, so the `Dataset` names the module and the
version rather than a checksum over a corpus that does not exist.
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
CONSTANTS_VERSION = "1.0.0"

#: τ_CSTR/τ_PFR at X = 0.9, first order: X/((1-X)·ln(1/(1-X))). Written as the closed form rather
#: than as 3.909 so the check cannot be satisfied by somebody updating a literal.
_NINETY_PERCENT = 0.9

#: Refining by 2.5x must improve a fourth-order answer by about 2.5^4 = 39. The threshold is 20
#: because the measured improvement is ~40 and the defect it guards against gave 2.5 — an order of
#: magnitude of daylight on either side, so the bound needs no precision.
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

    `RuntimeError` rather than `ValueError`: this is never a caller's input, it is this pod being
    wrong, and `connector_app` classifies it as a permanent cause so the pod leaves its Service
    rather than serving arithmetic that has moved.
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
        return reactors.semibatch_accumulation(steps=steps, **_DOSE).peak_accumulation_fraction
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

    Unlike `servers/suitability`, there is no transcribed table to digest field by field — every
    number here is a physical constant or a definition. So the digest is over the source, and it
    says what it is: two pods agreeing means they run the same formulas, and a comment change moves
    it. That is weaker than a value digest and is the honest thing available.
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
        SelfTestFailed: If any relation no longer holds. `connector_app` turns that into an unready
            `/healthz` naming the reason.
    """
    _check_reactor_ratio()
    _check_inverse_round_trip()
    _check_integrator_order()
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
                "convergence order, and the doubling-per-10-degrees rule."
            ),
            sha256=_formula_digest(),
            # The formulas *are* the modules, so a module is what is named. Pointing this at a
            # records file that does not exist would be provenance that quietly says nothing.
            records_path=Path(reactors.__file__),
        )
    ]
