"""The input guards every correlation in this server starts with, written once.

`servers/kinetics` wrote `_positive` twice, once per engine module, which is two places for one
rule about what a rate constant may be. This server has six correlation modules and would have
written it six times, so it is here instead — the Rule of Three with the third caller already
present at the moment the module is created.

**Every function here refuses rather than approximating.** A negative area, a fraction given as a
percentage, a temperature below absolute zero: each is a units mistake far more often than a typo,
and each has a plausible-looking wrong answer waiting on the other side of it. The message names
the argument and the value, because the reader is a chemist looking at a chat window rather than an
operator looking at a log.
"""

from __future__ import annotations

__all__ = [
    "ABSOLUTE_ZERO_C",
    "GRAVITY_M_PER_S2",
    "UnitOpsInputError",
    "fraction",
    "kelvin",
    "non_negative",
    "positive",
]

#: m/s². Standard gravity, written here rather than imported from `scipy.constants` so this
#: server's dependency closure stays the MCP transport and nothing else — the reason
#: `servers/thermalsafety` gives for its gas constant, and `servers/props` before it.
GRAVITY_M_PER_S2 = 9.80665

#: °C at 0 K.
ABSOLUTE_ZERO_C = -273.15


class UnitOpsInputError(ValueError):
    """An input that cannot be interpreted as the quantity it is named for.

    `ValueError` deliberately: `mcp_server_kit` passes this family through to the model verbatim,
    so the message is written for a chemist reading it in a chat rather than for a log.
    """


def positive(value: float, what: str) -> float:
    """A dimension, a mass, a rate or a coefficient that must be above zero to mean anything.

    Args:
        value: The number supplied.
        what: The argument's name as the caller wrote it, for the message.

    Returns:
        The value unchanged.

    Raises:
        UnitOpsInputError: If the value is zero or negative.
    """
    if value <= 0.0:
        raise UnitOpsInputError(f"{what} must be greater than zero; got {value}.")
    return value


def non_negative(value: float, what: str) -> float:
    """A quantity that may legitimately be zero — an evaporated mass, a medium resistance.

    Args:
        value: The number supplied.
        what: The argument's name as the caller wrote it, for the message.

    Returns:
        The value unchanged.

    Raises:
        UnitOpsInputError: If the value is negative.
    """
    if value < 0.0:
        raise UnitOpsInputError(f"{what} must not be negative; got {value}.")
    return value


def fraction(value: float, what: str) -> float:
    """A mole or mass fraction, which is between zero and one and is not a percentage.

    Args:
        value: The number supplied.
        what: The argument's name as the caller wrote it, for the message.

    Returns:
        The value unchanged.

    Raises:
        UnitOpsInputError: If the value is outside `(0, 1)`. The message says what 95% is, because
            a percentage entered as a fraction is the mistake this guard exists for and it produces
            a plausible answer rather than an obvious one.
    """
    if not 0.0 < value < 1.0:
        raise UnitOpsInputError(
            f"{what} must be a fraction above 0 and below 1; got {value}. 95% is 0.95, not 95."
        )
    return value


def kelvin(celsius: float, what: str) -> float:
    """°C to K, refusing anything below absolute zero.

    Args:
        celsius: The temperature supplied, in °C.
        what: The argument's name as the caller wrote it, for the message.

    Returns:
        The temperature in kelvin.

    Raises:
        UnitOpsInputError: If the temperature is below absolute zero — which is a units mistake (a
            kelvin figure entered as °C reads as -250 °C) far more often than a typo.
    """
    value = celsius - ABSOLUTE_ZERO_C
    if value <= 0.0:
        raise UnitOpsInputError(
            f"{what} is {celsius} °C, at or below absolute zero. If this was meant as kelvin, "
            "this tool takes °C."
        )
    return value
