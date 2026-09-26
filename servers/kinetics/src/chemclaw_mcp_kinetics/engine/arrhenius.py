"""Arrhenius arithmetic for a rate constant: extrapolate one, or determine E_a from two.

    k(T) = A · exp( -E_a / (R·T) )

Two operations, and the difference between them is the whole of this module's honesty.

**Extrapolating** takes a rate constant measured at one temperature and an activation energy the
chemist supplies, and gives k at another. It is exact arithmetic on inputs, and its *error* is
entirely the error in the E_a it was handed — which is why every answer carries how far it
extrapolated.

**Determining E_a from two measured points** is exact algebra, not a fit:

    E_a = R · ln(k₂/k₁) / (1/T₁ - 1/T₂)

Two points determine a line through two points. There is no residual, no goodness of fit and no
confidence interval, because there is nothing left over — and this module says so rather than
returning an r² of 1.0, which would be true and would read as a quality claim. A *fit* over three
or more points is regression, it belongs with the tools this server deliberately does not ship, and
calling this one "a fit" would blur exactly that line.

**What this is not.** `servers/thermalsafety` already owns an Arrhenius extrapolation, and it is a
different one: `temperature_for_tmr` extrapolates **q**, the specific heat-release rate of a
*decomposition*, inside a TMR_ad inversion, and returns a temperature. This module extrapolates
**k**, the rate constant of the reaction a chemist is running, and returns a rate constant. Nothing
here returns a TMR, a T_D24 or a criticality class; a question about a runaway is that server's.

The two are not interchangeable even where the algebra rhymes: a decomposition's apparent E_a from
a DSC and a synthesis reaction's E_a from a kinetic study are different numbers about different
processes, and a tool that accepted either would let one be quoted as the other.
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

#: J/(mol·K). Written here rather than imported from `scipy` so this server's dependency closure
#: stays the MCP transport and nothing else — the reason `servers/thermalsafety` gives for the same
#: choice, and `servers/props` before it.
GAS_CONSTANT_J_PER_MOL_K = 8.314462618

#: °C at 0 K. The one conversion every function here starts with.
ABSOLUTE_ZERO_C = -273.15

#: Beyond this, an extrapolation is reported with the distance named rather than refused: how far is
#: too far depends on whether the mechanism changes, which no arithmetic can know. 50 K is where a
#: process chemist would normally want a second measured point.
_EXTRAPOLATION_NOTICE_K = 50.0


class KineticsInputError(ValueError):
    """An input that cannot be interpreted as kinetics.

    `ValueError` deliberately: `mcp_server_kit` passes this family through to the model verbatim,
    so the message is written for a chemist reading it in a chat rather than for a log.
    """


def representable(what: str, compute: Callable[[], float]) -> float:
    """Evaluate one closed-form expression, refusing by name a result a double cannot hold.

    **Every input guard here is about finiteness, not magnitude**, and finite inputs still reach
    an unrepresentable answer: a float `**` or `math.exp` *raises* `OverflowError` — which is not a
    `ValueError`, so `connector_app` hands the model an opaque `error_id` — while a float `*` or
    `/` quietly returns infinity, and `inf / inf` returns NaN. Measured: a CSTR at order 3 with
    `C0 = 1e200`, a batch reactor at order 200 with `C0 = 1e-5`, and an Arrhenius extrapolation from
    -273 °C each raised, and two points 1e-9 K apart returned an infinite activation energy.

    Caught at the expression rather than by an input ceiling, for the reason `_rate` in
    `reactors.semibatch_accumulation` gives: no magnitude bound on the inputs is physical, and a
    number that cannot be represented is the refusal the caller needs to read.

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
        KineticsInputError: If the temperature is below absolute zero — which is a units mistake
            (a kelvin figure entered as °C reads as -250 °C) far more often than a typo.
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

    Finite first, because `value <= 0.0` is False for NaN and infinity alike, and an infinite rate
    constant passes pydantic's `gt=0` — the MCP JSON parser accepts the literal `Infinity`.
    """
    if not math.isfinite(value):
        raise KineticsInputError(f"{what} must be a finite number; got {value}.")
    if value <= 0.0:
        raise KineticsInputError(f"{what} must be greater than zero; got {value}.")
    return value


@dataclass(frozen=True)
class RateConstant:
    """A rate constant at a temperature, with how far it was carried to get there."""

    #: In the same unit as the reference rate constant it came from. This module never names that
    #: unit, because it depends on the reaction order, and a tool that guessed would be wrong for
    #: every order but the one it assumed.
    rate_constant: float
    temperature_c: float
    #: The gap between this temperature and the measured one, in kelvin. Signed, so a reader can
    #: tell an extrapolation up from one down — they are not equally safe, since a mechanism that
    #: switches usually does so on heating.
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
    #: ln A, in the same unit as the two rate constants. Reported as a logarithm because A itself
    #: routinely overflows a float for a real reaction (10^13 s^-1 is ordinary) and because the
    #: number a chemist compares against literature is the logarithm anyway.
    ln_pre_exponential: float
    lower_temperature_c: float
    upper_temperature_c: float
    #: The temperature gap the two points span, in kelvin. An E_a determined over 5 K carries the
    #: measurement error of both points amplified by the reciprocal of that gap, so the span is
    #: part of the answer rather than context.
    span_k: float
    #: True when the two points are close enough together that the determination is dominated by
    #: measurement error rather than by the temperature dependence.
    span_is_narrow: bool


#: Below this span, the reciprocal-temperature difference is small enough that ordinary error in
#: two rate constants dominates the answer. Stated as a measurement rather than a feeling: over a
#: 5 K span at 300 K, a 5% error in each k propagates to roughly 35% in E_a; over 30 K, to 6%.
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
        reference_rate_constant: The measured rate constant, in whatever unit its reaction order
            implies. The unit is not asked for and not converted: the answer comes back in the same
            one, and a ratio is unitless either way.
        activation_energy_kj_per_mol: The activation energy of *this* reaction, in kJ/mol. There is
            no default — an assumed E_a is the whole of the answer's error, and supplying one would
            be this tool inventing the number the chemist came to ask about.

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

    Two points determine a line through two points, so there is no residual and no goodness of fit
    to report. A regression over three or more points is a different operation and is not served
    here.

    Args:
        lower_temperature_c: The cooler temperature, in °C.
        lower_rate_constant: The rate constant measured there.
        upper_temperature_c: The warmer temperature, in °C.
        upper_rate_constant: The rate constant measured there.

    Returns:
        The activation energy, ln A, the span the two points cover, and whether that span is narrow
        enough for measurement error to dominate.

    Raises:
        KineticsInputError: If the two temperatures are equal (no span, so E_a is undefined rather
            than large), if they are given in the wrong order, if a rate constant is not positive,
            or if the rate constant *falls* with temperature — which is a real phenomenon with a
            real cause and not one Arrhenius describes.
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
