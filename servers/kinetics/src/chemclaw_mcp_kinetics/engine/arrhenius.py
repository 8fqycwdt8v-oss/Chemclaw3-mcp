"""Arrhenius arithmetic for a rate constant: extrapolate one, or determine E_a from two.

    k(T) = A · exp( -E_a / (R·T) )

Extrapolation is exact arithmetic whose error is entirely that of the supplied E_a, so every
answer reports how far it extrapolated. E_a from two points is exact algebra, not a fit — there is
no residual, so none is reported; regression over more points is not served.

This extrapolates **k** of the reaction being run. `servers/thermalsafety` extrapolates the heat
release of a *decomposition*; the two activation energies are different numbers and are kept in
separate tools so one is never quoted as the other.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

__all__ = [
    "ABSOLUTE_ZERO_C",
    "GAS_CONSTANT_J_PER_MOL_K",
    "ArrheniusPair",
    "KineticsInputError",
    "RateConstant",
    "activation_energy_from_two_points",
    "kelvin",
    "rate_constant_at",
    "representable",
]

#: J/(mol·K). Written here so the dependency closure stays the MCP transport alone.
GAS_CONSTANT_J_PER_MOL_K = 8.314462618

#: °C at 0 K. The one conversion every function here starts with.
ABSOLUTE_ZERO_C = -273.15

#: Beyond this an extrapolation is reported with its distance named, not refused: whether the
#: mechanism changes is not something arithmetic can know.
_EXTRAPOLATION_NOTICE_K = 50.0


class KineticsInputError(ValueError):
    """An input that cannot be interpreted as kinetics.

    A `ValueError`, so `mcp_server_kit` passes the message to the model verbatim.
    """


def representable(what: str, compute: Callable[[], float]) -> float:
    """Evaluate one closed-form expression, refusing by name a result a double cannot hold.

    Input guards check finiteness, not magnitude, so finite inputs can still overflow: `**` and
    `math.exp` raise `OverflowError` (not a `ValueError`, so the model would see an opaque error
    id), while `*` and `/` return infinity or NaN. Both are caught here and worded.

    Args:
        what: The quantity being computed, as a chemist would name it in the refusal.
        compute: The expression, deferred so its own `OverflowError` lands here.

    Returns:
        The value, finite.

    Raises:
        KineticsInputError: If the expression overflows, divides by zero, or is not finite.
    """
    try:
        value = float(compute())
    except (OverflowError, ZeroDivisionError):
        value = math.inf
    if not math.isfinite(value):
        raise KineticsInputError(
            f"{what} overflows a double for these inputs, so there is no finite number to report "
            "and no real reaction sits there. Check the units of the concentrations, the rate "
            "constant and the temperatures."
        )
    return value


def kelvin(celsius: float) -> float:
    """°C to K, refusing anything below absolute zero.

    Raises:
        KineticsInputError: If the temperature is below absolute zero (usually a kelvin figure
            entered as °C).
    """
    if not math.isfinite(celsius):
        raise KineticsInputError(f"a temperature must be a finite number; got {celsius} °C.")
    value = celsius - ABSOLUTE_ZERO_C
    if value <= 0.0:
        raise KineticsInputError(
            f"{celsius} °C is at or below absolute zero. If this was meant as kelvin, this tool "
            "takes °C."
        )
    return value


def _positive(value: float, what: str) -> float:
    """A rate constant, an energy or a time that must be finite and above zero to mean anything.

    Finiteness is checked first: `value <= 0.0` is False for NaN, and the JSON parser accepts
    `Infinity`, which passes pydantic's `gt=0`.
    """
    if not math.isfinite(value):
        raise KineticsInputError(f"{what} must be a finite number; got {value}.")
    if value <= 0.0:
        raise KineticsInputError(f"{what} must be greater than zero; got {value}.")
    return value


@dataclass(frozen=True)
class RateConstant:
    """A rate constant at a temperature, with how far it was carried to get there."""

    #: In the unit of the reference rate constant; that unit depends on the reaction order, so it is
    #: never named here.
    rate_constant: float
    temperature_c: float
    #: Signed gap from the measured temperature, in kelvin: extrapolating up is less safe than down.
    extrapolated_by_k: float
    #: True when the gap is wide enough that a second measured point is the honest answer.
    far_from_the_measurement: bool
    #: The ratio k(T)/k(T_ref) — the thing a chemist is usually actually asking for ("how much
    #: faster if I run it 10 degrees warmer").
    rate_ratio: float


@dataclass(frozen=True)
class ArrheniusPair:
    """An activation energy determined by two measured points, and the pre-exponential with it."""

    activation_energy_kj_per_mol: float
    #: ln A, in the unit of the rate constants; A itself routinely overflows a float.
    ln_pre_exponential: float
    lower_temperature_c: float
    upper_temperature_c: float
    #: The temperature span of the two points, in kelvin; measurement error in E_a scales with its
    #: reciprocal.
    span_k: float
    #: True when the two points are close enough together that the determination is dominated by
    #: measurement error rather than by the temperature dependence.
    span_is_narrow: bool


#: Below this span ordinary error in the two rate constants dominates E_a (5% per k gives ~35%
#: over 5 K at 300 K).
_NARROW_SPAN_K = 10.0


def rate_constant_at(
    target_temperature_c: float,
    *,
    reference_temperature_c: float,
    reference_rate_constant: float,
    activation_energy_kj_per_mol: float,
) -> RateConstant:
    """Carry a measured rate constant to another temperature along Arrhenius.

    Args:
        target_temperature_c: The temperature wanted, in °C.
        reference_temperature_c: The temperature the rate constant was measured at, in °C.
        reference_rate_constant: The measured rate constant; the answer is in the same unit.
        activation_energy_kj_per_mol: The activation energy of this reaction, in kJ/mol. No
            default: an assumed E_a is the whole of the answer's error.

    Returns:
        The rate constant at the target temperature, the ratio to the measured one, and how far it
        was carried.

    Raises:
        KineticsInputError: If a temperature is below absolute zero, or the rate constant or
            activation energy is not positive.
    """
    target_k = kelvin(target_temperature_c)
    reference_k = kelvin(reference_temperature_c)
    _positive(reference_rate_constant, "the reference rate constant")
    _positive(activation_energy_kj_per_mol, "the activation energy")

    exponent = (
        -(activation_energy_kj_per_mol * 1000.0)
        / GAS_CONSTANT_J_PER_MOL_K
        * (1.0 / target_k - 1.0 / reference_k)
    )
    ratio = representable("the rate ratio k(T)/k(T_ref)", lambda: math.exp(exponent))
    gap = target_k - reference_k
    return RateConstant(
        rate_constant=representable(
            "the extrapolated rate constant", lambda: reference_rate_constant * ratio
        ),
        temperature_c=target_temperature_c,
        extrapolated_by_k=gap,
        far_from_the_measurement=abs(gap) > _EXTRAPOLATION_NOTICE_K,
        rate_ratio=ratio,
    )


def activation_energy_from_two_points(
    *,
    lower_temperature_c: float,
    lower_rate_constant: float,
    upper_temperature_c: float,
    upper_rate_constant: float,
) -> ArrheniusPair:
    """E_a and ln A from two measured (T, k) pairs — exact algebra, not a fit.

    Args:
        lower_temperature_c: The cooler temperature, in °C.
        lower_rate_constant: The rate constant measured there.
        upper_temperature_c: The warmer temperature, in °C.
        upper_rate_constant: The rate constant measured there.

    Returns:
        The activation energy, ln A, the span the two points cover, and whether that span is narrow
        enough for measurement error to dominate.

    Raises:
        KineticsInputError: If the temperatures are equal or out of order, a rate constant is not
            positive, or the rate constant falls with temperature (not Arrhenius behaviour).
    """
    lower_k = kelvin(lower_temperature_c)
    upper_k = kelvin(upper_temperature_c)
    _positive(lower_rate_constant, "the rate constant at the lower temperature")
    _positive(upper_rate_constant, "the rate constant at the upper temperature")

    if upper_k <= lower_k:
        raise KineticsInputError(
            f"the upper temperature ({upper_temperature_c} °C) must be above the lower "
            f"({lower_temperature_c} °C). With no span there is no temperature dependence to "
            "measure, so the activation energy is undefined rather than large."
        )
    if upper_rate_constant < lower_rate_constant:
        raise KineticsInputError(
            f"the rate constant falls from {lower_rate_constant} at {lower_temperature_c} °C to "
            f"{upper_rate_constant} at {upper_temperature_c} °C. Arrhenius describes a rate that "
            "rises with temperature; a rate that falls has a cause this arithmetic cannot "
            "represent — a change of mechanism, a pre-equilibrium, a catalyst decomposing, or the "
            "two points being of different things. A negative activation energy computed anyway "
            "would be quoted as though it meant one."
        )

    reciprocal_gap = 1.0 / lower_k - 1.0 / upper_k
    energy_j = representable(
        "the activation energy",
        lambda: (
            GAS_CONSTANT_J_PER_MOL_K
            * math.log(upper_rate_constant / lower_rate_constant)
            / reciprocal_gap
        ),
    )
    span = upper_k - lower_k
    return ArrheniusPair(
        activation_energy_kj_per_mol=energy_j / 1000.0,
        # ln A = ln k + E_a/(R·T), taken at the lower point; either point gives the same answer,
        # because the line passes through both by construction.
        ln_pre_exponential=representable(
            "ln A",
            lambda: math.log(lower_rate_constant) + energy_j / (GAS_CONSTANT_J_PER_MOL_K * lower_k),
        ),
        lower_temperature_c=lower_temperature_c,
        upper_temperature_c=upper_temperature_c,
        span_k=span,
        span_is_narrow=span < _NARROW_SPAN_K,
    )
