"""The input guards every correlation in this server starts with, written once.

Every guard refuses rather than approximating: a negative area, a percentage given as a fraction or
a sub-absolute-zero temperature is usually a units mistake with a plausible wrong answer behind it.
Messages name the argument and value for a chemist reading a chat.
"""

from __future__ import annotations

import math
from collections.abc import Callable

__all__ = [
    "ABSOLUTE_ZERO_C",
    "GRAVITY_M_PER_S2",
    "UnitOpsInputError",
    "finite",
    "finite_result",
    "fraction",
    "kelvin",
    "non_negative",
    "positive",
]

#: Standard gravity, m/s²; a literal so the dependency closure stays the transport alone.
GRAVITY_M_PER_S2 = 9.80665

#: °C at 0 K.
ABSOLUTE_ZERO_C = -273.15


class UnitOpsInputError(ValueError):
    """An input that cannot be interpreted as the quantity it is named for.

    A `ValueError` so `mcp_server_kit` passes the chemist-facing message to the model verbatim.
    """


def finite(value: float, what: str) -> float:
    """Refuse infinity and NaN, which every comparison below would otherwise wave through.

    JSON input can carry both, and `value <= 0.0` is False for each. Public for quantities with no
    sign constraint, such as a feed quality `q`.
    """
    if not math.isfinite(value):
        raise UnitOpsInputError(f"{what} must be a finite number; got {value}.")
    return value


def finite_result(compute: Callable[[], float], what: str) -> float:
    """Run one power-law correlation, refusing a result a float cannot hold.

    Finite inputs can still overflow (`OverflowError`, `inf`) or underflow to `0.0` and then divide
    by zero; each is refused as a `UnitOpsInputError` naming `what`. `compute` is deferred so its
    `OverflowError` is caught here.
    """
    try:
        value = compute()
    except OverflowError as error:
        raise UnitOpsInputError(
            f"{what} overflows a floating-point number for these inputs; check their units."
        ) from error
    except ZeroDivisionError as error:
        raise UnitOpsInputError(
            f"{what} divides by a quantity that underflows to zero for these inputs; check their "
            "units."
        ) from error
    if not math.isfinite(value):
        raise UnitOpsInputError(
            f"{what} is not a finite number for these inputs ({value}); check their units."
        )
    return value


def positive(value: float, what: str) -> float:
    """Return `value` if finite and above zero, else refuse naming `what`."""
    finite(value, what)
    if value <= 0.0:
        raise UnitOpsInputError(f"{what} must be greater than zero; got {value}.")
    return value


def non_negative(value: float, what: str) -> float:
    """Return `value` if finite and not negative (an evaporated mass, a medium resistance)."""
    finite(value, what)
    if value < 0.0:
        raise UnitOpsInputError(f"{what} must not be negative; got {value}.")
    return value


def fraction(value: float, what: str) -> float:
    """Return a mole or mass fraction strictly inside `(0, 1)`, else refuse naming `what`.

    The message says what 95% is, since a percentage entered here gives a plausible wrong answer.
    """
    finite(value, what)
    if not 0.0 < value < 1.0:
        raise UnitOpsInputError(
            f"{what} must be a fraction above 0 and below 1; got {value}. 95% is 0.95, not 95."
        )
    return value


def kelvin(celsius: float, what: str) -> float:
    """Convert °C to K, refusing NaN or below absolute zero (often a kelvin figure given as °C)."""
    finite(celsius, what)
    value = celsius - ABSOLUTE_ZERO_C
    if value <= 0.0:
        raise UnitOpsInputError(
            f"{what} is {celsius} °C, at or below absolute zero. If this was meant as kelvin, "
            "this tool takes °C."
        )
    return value
