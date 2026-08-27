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

Nothing here is extrapolated past the point of embarrassment: a temperature outside the range the
Antoine constants were fitted over falls back to the boiling-point route rather than evaluating a
fit where it is known to diverge, and both routes refuse below the melting point, where a liquid
vapour pressure is not the quantity anybody wants.

**That sentence used to be false, and the cost was measurable.** There was no range in the corpus
and no check in the code, so the only condition `_antoine_bar` could raise on was the correlation's
own pole -- which for every row here sits between 1 K and 76 K and is unreachable above any melting
point. The fallback branch was dead, and water at 200 °C came back as 13.775 bar against a
steam-table 15.549 bar (-11.4%) still labelled `antoine` and still carrying the "good to about a
percent" caveat. The range is now a column in the table, the caveat quotes it, and outside it the
answer is the Clausius-Clapeyron route saying so -- which at 200 °C is +3.9%, not -11.4%.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from chemclaw_mcp_props.engine.records import Solvent

KELVIN = 273.15
R_J_PER_MOL_K = 8.314462618
ATM_BAR = 1.01325
# How far past its fitted range an Antoine set may still be evaluated, in kelvin.
#
# **Measured, not chosen.** A NIST fitted range is the span of the source's experimental data, and
# the Antoine form extrapolates modestly well and distantly badly; the question is only where the
# "good to about a percent" claim stops being true. Both ends were measured against an independent
# anchor -- above the range, each row's own tabulated dHvap through Clausius-Clapeyron, which is
# reliable there because it is anchored at the boiling point:
#
#   +20 K past tmax: median disagreement 2.8%, and under 5% for every non-associating row.
#   +50 K past tmax: 4.8% median, and water is -6.5% against the steam tables.
#   +100 K past tmax: water is -11.4% (13.775 bar against 15.549) -- the number this bound exists
#                     to refuse, and the one that used to come back labelled `antoine`.
#
# Below the range the fit is the better of the two, and refusing there would make answers worse
# rather than safer: toluene at 25 °C is 10.4 K under its fitted range, where the fit reproduces
# the literature 37.9 mbar exactly and the Clausius-Clapeyron fallback reads 51.1 mbar (+35%).
# So the allowance is symmetric, and 20 K is the largest value at which the caveat's accuracy
# claim still holds on the side where it can be checked.
ANTOINE_EXTRAPOLATION_K = 20.0
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


def _antoine_bar(
    constants: tuple[float, float, float],
    fitted_range_k: tuple[float, float],
    temperature_k: float,
) -> float:
    """`log10(P/bar) = A - B/(T/K + C)`, the NIST WebBook form the table stores.

    Raises:
        ValueError: outside the fitted range (plus `ANTOINE_EXTRAPOLATION_K`), or below the
            correlation's pole. Both are the caller's signal to take the boiling-point route
            instead; neither is an answer this function is willing to give.
    """
    low, high = fitted_range_k
    if not low - ANTOINE_EXTRAPOLATION_K <= temperature_k <= high + ANTOINE_EXTRAPOLATION_K:
        raise ValueError(
            f"{temperature_k:.2f} K is outside the {low}-{high} K range these Antoine constants "
            "were fitted over"
        )
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
    fitted_range = solvent.antoine_range_k
    if solvent.antoine is not None and fitted_range is not None:
        try:
            bar = _antoine_bar(solvent.antoine, fitted_range, temperature_k)
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
        caveat=_caveat(method, fitted_range),
    )


def _caveat(method: Method, fitted_range_k: tuple[float, float] | None) -> str:
    """The sentence that must travel with a number produced by `method`.

    The Antoine branch quotes the range it was fitted over, because that is what makes "good to
    about a percent" a checkable claim rather than a reassurance: the number was produced inside
    that window, or it was not produced by this branch at all.
    """
    if method == "antoine":
        low, high = fitted_range_k if fitted_range_k is not None else (0.0, 0.0)
        return (
            "Antoine fit from the vendored table (log10(P/bar) = A - B/(T/K + C)), fitted over "
            f"{low:.1f} K to {high:.1f} K and evaluated inside it; good to about a percent there. "
            "Outside that window this server falls back to the Clausius-Clapeyron route and says "
            "so in `method`."
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
        ValueError: for a non-positive pressure, one whose boiling point lies below the melting
            point (the solvent freezes before it boils at that vacuum), or one the solvent never
            reaches inside the bracket. **Both ends of the bracket are guarded.** Only the low end
            used to be, so an unreachable pressure converged on `high` itself and was returned as a
            boiling point: a 30 bar autoclave question about DMSO answered "400.0 °C", which is the
            bracket ceiling wearing a `method` and a `caveat`.
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
    ceiling_mbar = vapour_pressure(solvent, high).pressure_mbar
    if ceiling_mbar < pressure_mbar:
        raise ValueError(
            f"{solvent.name} never reaches {pressure_mbar} mbar in this correlation: at "
            f"{high} °C, the top of the range this server models, its vapour pressure is still "
            f"{ceiling_mbar:.0f} mbar. There is no boiling point to report at that pressure"
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
