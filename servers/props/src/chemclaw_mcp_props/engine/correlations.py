"""The physics: vapour pressure, Hansen distance, and which route produced the number.

Two routes to a vapour pressure, and every answer returns `method` so they are never confused:

- **Antoine** (`log10(P/bar) = A - B/(T/K + C)`, NIST form), inside its fitted range plus
  `ANTOINE_EXTRAPOLATION_K`. Good to about a percent.
- **Clausius-Clapeyron from the normal boiling point** otherwise: exact at the boiling point,
  degrading away from it.

Both refuse outside the liquid range: below the melting point, and above
`MAX_TEMPERATURE_TB_RATIO` times the boiling point (Guldberg's estimate of the critical
temperature — a sanity ceiling, since the corpus carries no sourced critical temperature).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from mcp_server_kit.limits import env_ratio

from chemclaw_mcp_props.engine.records import Solvent

KELVIN = 273.15
R_J_PER_MOL_K = 8.314462618
ATM_BAR = 1.01325
# How far past its fitted range an Antoine set may still be evaluated, in kelvin.
#
# The largest margin at which the "good to about a percent" caveat still held against an
# independent Clausius-Clapeyron anchor. Symmetric, because below the range the fit is still the
# better of the two routes.
ANTOINE_EXTRAPOLATION_K = 20.0
# Trouton's rule, used only when there is neither an Antoine fit nor a measured ΔHvap. It
# underestimates ΔHvap for hydrogen-bonding liquids, and every answer using it says so.
TROUTON_J_PER_MOL_K = 88.0
# The upper end of the temperature axis, as a multiple of the normal boiling point in kelvin.
#
# Above the critical temperature there is no liquid. The corpus has no critical temperature, so this
# uses Guldberg's rule (`Tc ≈ 1.5 x Tb`), set loose at 1.8 because hydrogen-bonding liquids sit well
# above 1.5 (water at 300 °C must still answer). It is therefore a sanity ceiling, not a phase
# boundary, and can admit a slightly supercritical point; a sourced critical-temperature column is
# the better fix.
#
# Read through `env_ratio`, which refuses a malformed or sub-floor value at import: a knob that
# loosens a bound can also remove it. No upper limit, since raising it only loosens the ceiling.
MAX_TEMPERATURE_TB_RATIO = env_ratio(
    "CHEMCLAW_PROPS_MAX_TB_RATIO",
    default=1.8,
    # The ceiling must sit a real distance above the normal boiling point; 1.01 leaves a few kelvin
    # for the lowest-boiling row and excludes nothing a deployment could legitimately want.
    minimum=1.01,
    consequence=(
        "a ratio this small leaves no usable liquid range above the normal boiling point - at 1 "
        "the ceiling is the boiling point itself, so every vapour pressure above it is refused, "
        "and at 0 or below the ceiling is under absolute zero and every question is refused on a "
        "pod that starts and passes its readiness probe"
    ),
)

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
            correlation's pole; the caller then takes the boiling-point route.
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


def max_temperature_c(solvent: Solvent) -> float:
    """The highest temperature this server will report a liquid vapour pressure at, in Celsius.

    Exposed because `boiling_point_at` must clamp its bisection bracket to exactly this, or the
    forward refusal would fire from inside the inverse.
    """
    return MAX_TEMPERATURE_TB_RATIO * (solvent.bp_c + KELVIN) - KELVIN


def vapour_pressure(solvent: Solvent, temperature_c: float) -> VapourPressure:
    """The vapour pressure of `solvent` at `temperature_c`, with the route that produced it.

    Args:
        solvent: The row to evaluate.
        temperature_c: Temperature in degrees Celsius.

    Returns:
        The pressure in bar, mbar and mmHg, the `method`, and a one-line caveat.

    Raises:
        ValueError: below the melting point or above the estimated critical temperature — this
            server answers about a liquid only.
    """
    if temperature_c < solvent.mp_c:
        raise ValueError(
            f"{temperature_c} °C is below the melting point of {solvent.name} ({solvent.mp_c} °C); "
            "this server carries liquid vapour pressures only"
        )
    ceiling_c = max_temperature_c(solvent)
    if temperature_c > ceiling_c:
        raise ValueError(
            f"{temperature_c} °C is above {ceiling_c:.1f} °C, where this server stops reporting a "
            f"vapour pressure for {solvent.name}: {MAX_TEMPERATURE_TB_RATIO:g} x its normal "
            f"boiling point of {solvent.bp_c + KELVIN:.1f} K. Above the critical temperature there "
            "is no liquid and so no vapour pressure, and the correlation would keep returning a "
            "number for a substance that is not there. The table carries no critical temperature, "
            "so this bound is Guldberg's rule of thumb set deliberately loose — a sanity ceiling "
            "rather than a phase boundary, which means a number just below it can already be "
            "supercritical nonsense. If you need this region, get the real critical temperature "
            "and raise CHEMCLAW_PROPS_MAX_TB_RATIO deliberately"
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

    The Antoine branch quotes its fitted range, which makes the accuracy claim checkable.
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

    Bisection on `vapour_pressure`, so it works for either route and cannot disagree with the
    forward direction.

    Args:
        solvent: The row to evaluate.
        pressure_mbar: The absolute pressure in the still, in mbar.

    Returns:
        The boiling temperature in degrees Celsius.

    Raises:
        ValueError: for a non-positive pressure, a boiling point below the melting point, or a
            pressure not reached inside the bracket (both bracket ends are checked, so the ceiling
            is never returned as an answer).
    """
    if pressure_mbar <= 0:
        raise ValueError("pressure must be positive")
    target_bar = pressure_mbar / 1000.0
    # Clamped to the forward direction's ceiling so `vapour_pressure` never refuses inside the
    # bisection.
    low, high = solvent.mp_c, min(max(solvent.bp_c + 200.0, 400.0), max_temperature_c(solvent))
    if vapour_pressure(solvent, low).pressure_bar > target_bar:
        raise ValueError(
            f"{solvent.name} reaches {pressure_mbar} mbar only below its melting point "
            f"({solvent.mp_c} °C) — it freezes in the still before it boils at that vacuum"
        )
    ceiling_mbar = vapour_pressure(solvent, high).pressure_mbar
    if ceiling_mbar < pressure_mbar:
        raise ValueError(
            f"{solvent.name} never reaches {pressure_mbar} mbar in this correlation: at "
            f"{high:.1f} °C, the top of the range this server models, its vapour pressure is still "
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

    `Ra = sqrt(4*(dD1-dD2)^2 + (dP1-dP2)^2 + (dH1-dH2)^2)` — the factor 4 is Hansen's convention.
    Roughly: below 4 the two dissolve much the same things, above 8 they do not. It says nothing
    about reactivity.
    """
    return math.sqrt(
        4.0 * (first.hansen_d - second.hansen_d) ** 2
        + (first.hansen_p - second.hansen_p) ** 2
        + (first.hansen_h - second.hansen_h) ** 2
    )
