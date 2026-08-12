"""The physics: vapour pressure, Hansen distance, and the honesty about which one you got.

Two ways to a vapour pressure live here, and the difference between them is the whole point of
returning a `method` field beside the number:

- **Antoine**, when the table carries constants for that solvent. `log10(P/bar) = A - B/(T/K + C)`,
  the NIST WebBook form. Good to a percent or so inside its fitted range.
- **Clausius-Clapeyron from the normal boiling point**, when it does not. One point and a slope,
  assuming ΔHvap is constant with temperature. It is right at the boiling point by construction and
  degrades as you move away from it — perfectly adequate for "will this strip at 40 °C and 50 mbar",
  useless for a VLE calculation.

A tool that returned only the number would let those two be confused, and the second one silently
carries several times the error of the first. So the method comes back with the value, every time,
and the tool docstrings tell the model to quote it.

Nothing here is extrapolated past the point of embarrassment: a temperature far outside the
Antoine fit falls back to the boiling-point route rather than evaluating a fit where it is known to
diverge, and both routes refuse below the melting point, where a liquid vapour pressure is not the
quantity anybody wants.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from chemclaw_mcp_props.engine.records import Solvent

KELVIN = 273.15
R_J_PER_MOL_K = 8.314462618
ATM_BAR = 1.01325
# Trouton's rule. Used only when the table has neither Antoine constants nor a measured ΔHvap;
# it systematically *underestimates* ΔHvap for hydrogen-bonding liquids (alcohols, acids, water),
# which is why every answer that uses it says so rather than quietly widening its own error bar.
TROUTON_J_PER_MOL_K = 88.0

Method = Literal["antoine", "clausius_clapeyron", "clausius_clapeyron_trouton"]


@dataclass(frozen=True, slots=True)
class VapourPressure:
    """A vapour pressure, the route that produced it, and what that route is worth."""

    temperature_c: float
    pressure_bar: float
    pressure_mbar: float
    pressure_mmhg: float
    method: Method
    caveat: str

    @property
    def pressure_kpa(self) -> float:
        """The same pressure in kPa, for plant instrumentation that reads in kPa."""
        return self.pressure_bar * 100.0


def _antoine_bar(constants: tuple[float, float, float], temperature_k: float) -> float:
    """`log10(P/bar) = A - B/(T/K + C)`, the NIST WebBook form the table stores."""
    a, b, c = constants
    denominator = temperature_k + c
    if denominator <= 0:
        raise ValueError("temperature is below the Antoine correlation's pole")
    return float(10.0 ** (a - b / denominator))


def _clausius_clapeyron_bar(solvent: Solvent, temperature_k: float) -> tuple[float, Method]:
    """Vapour pressure from the normal boiling point and ΔHvap, anchored at P = 1 atm."""
    boiling_k = solvent.bp_c + KELVIN
    if solvent.hvap_kj_mol is not None:
        hvap = solvent.hvap_kj_mol * 1000.0
        method: Method = "clausius_clapeyron"
    else:
        hvap = TROUTON_J_PER_MOL_K * boiling_k
        method = "clausius_clapeyron_trouton"
    exponent = -(hvap / R_J_PER_MOL_K) * (1.0 / temperature_k - 1.0 / boiling_k)
    return ATM_BAR * math.exp(exponent), method


def vapour_pressure(solvent: Solvent, temperature_c: float) -> VapourPressure:
    """The vapour pressure of `solvent` at `temperature_c`, with the route that produced it.

    Args:
        solvent: The row to evaluate.
        temperature_c: Temperature in degrees Celsius.

    Returns:
        The pressure in bar, mbar and mmHg, the `method`, and a one-line caveat naming what that
        method is and is not good for.

    Raises:
        ValueError: below the melting point, where a liquid vapour pressure is not the quantity
            being asked for — the answer there is a sublimation pressure this server does not carry.
    """
    if temperature_c < solvent.mp_c:
        raise ValueError(
            f"{temperature_c} °C is below the melting point of {solvent.name} ({solvent.mp_c} °C); "
            "this server carries liquid vapour pressures only"
        )
    temperature_k = temperature_c + KELVIN
    if solvent.antoine is not None:
        try:
            bar = _antoine_bar(solvent.antoine, temperature_k)
            method: Method = "antoine"
        except ValueError:
            bar, method = _clausius_clapeyron_bar(solvent, temperature_k)
    else:
        bar, method = _clausius_clapeyron_bar(solvent, temperature_k)
    return VapourPressure(
        temperature_c=temperature_c,
        pressure_bar=bar,
        pressure_mbar=bar * 1000.0,
        pressure_mmhg=bar * 750.062,
        method=method,
        caveat=_caveat(method),
    )


def _caveat(method: Method) -> str:
    """The sentence that must travel with a number produced by `method`."""
    if method == "antoine":
        return (
            "Antoine fit from the vendored table (log10(P/bar) = A - B/(T/K + C)); good to about a "
            "percent near the fitted range, and not to be extrapolated far past the boiling point."
        )
    if method == "clausius_clapeyron":
        return (
            "Clausius-Clapeyron from the normal boiling point and a tabulated dHvap, assuming "
            "dHvap is constant with temperature. Exact at the boiling point, and progressively "
            "optimistic "
            "away from it — treat it as an order-of-magnitude guide for stripping and drying, not "
            "as VLE data."
        )
    return (
        "Clausius-Clapeyron with dHvap estimated by Trouton's rule (88 J/mol/K), because the table "
        "carries no measured dHvap for this solvent. Trouton underestimates dHvap for "
        "hydrogen-bonding liquids, so the pressure below the boiling point is likely too high. "
        "Use it to rank options, never to size equipment."
    )


def boiling_point_at(solvent: Solvent, pressure_mbar: float) -> float:
    """The temperature at which `solvent` boils under `pressure_mbar` — the distillation question.

    Solved by bisection on `vapour_pressure` rather than by inverting the correlation, so it works
    identically for an Antoine row and a Clausius-Clapeyron one and cannot disagree with the
    forward direction.

    Args:
        solvent: The row to evaluate.
        pressure_mbar: The absolute pressure in the still, in mbar.

    Returns:
        The boiling temperature in degrees Celsius.

    Raises:
        ValueError: for a non-positive pressure, or one whose boiling point lies below the melting
            point (the solvent freezes before it boils at that vacuum).
    """
    if pressure_mbar <= 0:
        raise ValueError("pressure must be positive")
    target_bar = pressure_mbar / 1000.0
    low, high = solvent.mp_c, max(solvent.bp_c + 200.0, 400.0)
    if vapour_pressure(solvent, low).pressure_bar > target_bar:
        raise ValueError(
            f"{solvent.name} reaches {pressure_mbar} mbar only below its melting point "
            f"({solvent.mp_c} °C) — it freezes in the still before it boils at that vacuum"
        )
    for _ in range(200):
        middle = (low + high) / 2.0
        if vapour_pressure(solvent, middle).pressure_bar < target_bar:
            low = middle
        else:
            high = middle
    return (low + high) / 2.0


def hansen_distance(first: Solvent, second: Solvent) -> float:
    """The Hansen distance Ra between two solvents, in MPa^0.5.

    `Ra = sqrt(4*(dD1-dD2)^2 + (dP1-dP2)^2 + (dH1-dH2)^2)` — the dispersion term is weighted by
    four, which is Hansen's own convention and not a typo. Roughly: below 4 the two solvents
    dissolve much the same things, above 8 they do not.

    It is a *solubility* similarity and says nothing about reactivity: DMSO and sulfolane are close
    here, and one of them is a competent oxidant under Swern conditions.
    """
    return math.sqrt(
        4.0 * (first.hansen_d - second.hansen_d) ** 2
        + (first.hansen_p - second.hansen_p) ** 2
        + (first.hansen_h - second.hansen_h) ** 2
    )
