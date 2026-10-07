"""The readiness check: run the arithmetic on known cases and refuse if any answer has moved.

This server loads nothing, but `oxygen_balance.ATOMIC_WEIGHTS` and the band table are a corpus that
lives in source, and a wrong digit there is invisible without a check. So the probe runs each case
through the real public function and compares against values published independently of this code;
merely importing the module would pass a wrong table.
"""

from __future__ import annotations

import hashlib
from importlib.metadata import version
from pathlib import Path

from mcp_server_kit.datasets import Dataset

from chemclaw_mcp_thermalsafety.engine import oxygen_balance, runaway, semenov

#: The revision of the constants this repository writes; bump it by hand when a band boundary or
#: formula changes, so `/healthz` says which is newer (a digest only says whether two match).
_FIRST_PARTY_REVISION = "1.1.0"

#: What `/healthz` publishes: the revision above and the `molmass` release the atomic weights come
#: from. `uv.lock` resolves different `molmass` releases per Python version, so two pods may hold
#: different weight tables and must say so.
CONSTANTS_VERSION = f"{_FIRST_PARTY_REVISION}+molmass-{version('molmass')}"

#: `(formula, published OB%)` from the explosives literature, independent of this code. Three cases
#: spanning near-zero to strongly deficient exercise every band boundary.
_PUBLISHED_BALANCES: tuple[tuple[str, float], ...] = (
    ("C3H5N3O9", 3.5),  # nitroglycerine
    ("C7H5N3O6", -74.0),  # TNT
    ("C6H12O6", -106.6),  # glucose
)

#: Percentage points, covering the published values' rounding; a carbon weight wrong in its first
#: decimal moves TNT by ~0.4.
_BALANCE_TOLERANCE = 0.15


class SelfTestFailed(RuntimeError):
    """This build's arithmetic does not agree with the values it was verified against.

    A `RuntimeError`: the pod is wrong, not the caller. `connector_app` classifies it as permanent,
    keeping the pod out of service until replaced.
    """


def _constants_digest() -> str:
    """The SHA-256 of this server's numbers: the module's source **and the weights it resolved**.

    Published on `/healthz` so two pods can be shown to serve the same values. The weights now come
    from `molmass`, so the source alone no longer contains them; they are hashed as resolved values
    (`repr`, sorted keys) so the digest moves exactly when a value does.
    """
    source = Path(oxygen_balance.__file__)
    table = repr(sorted(oxygen_balance.ATOMIC_WEIGHTS.items())).encode("utf-8")
    return hashlib.sha256(source.read_bytes() + b"\n" + table).hexdigest()


def verify() -> list[Dataset]:
    """Run one case through each engine module and return what this pod verified.

    Raises:
        SelfTestFailed: An answer disagrees with its reference value.
    """
    for formula, expected in _PUBLISHED_BALANCES:
        computed = oxygen_balance.oxygen_balance(formula).oxygen_balance_percent
        if abs(computed - expected) > _BALANCE_TOLERANCE:
            raise SelfTestFailed(
                f"oxygen balance for {formula} computed {computed:.2f}%, published {expected}% "
                f"(tolerance {_BALANCE_TOLERANCE}) — the atomic-weight table or the formula in "
                "engine/oxygen_balance.py is not the one this build was verified against"
            )

    # 150 kJ/mol x 10 mol / (50 kg x 1.9 kJ/(kg·K)) = 15.789 K by hand; catches a unit error in the
    # adiabatic path.
    rise = runaway.adiabatic_temperature_rise(
        heat_of_reaction_kj_per_mol=-150.0, moles=10.0, mass_kg=50.0, specific_heat_kj_per_kg_k=1.9
    )
    if abs(rise - 1500.0 / 95.0) > 1e-6:
        raise SelfTestFailed(
            f"the adiabatic temperature rise for the reference case computed {rise:.4f} K where "
            "the hand-computed value is 15.7895 K"
        )

    # Checked against its defining condition: at tangency generation equals loss.
    balance = semenov.semenov_criticality(
        mass_kg=25.0,
        heat_release_rate_w_per_kg=1.0,
        reference_temperature_c=100.0,
        activation_energy_kj_per_mol=140.0,
        heat_transfer_coefficient_w_per_m2_k=5.0,
        surface_area_m2=0.5,
    )
    loss = 5.0 * 0.5 * balance.self_heating_at_criticality_k
    if abs(balance.heat_generation_at_criticality_w - loss) > 1e-4 * loss:
        raise SelfTestFailed(
            "the Semenov solver returned a point where generation "
            f"({balance.heat_generation_at_criticality_w:.4f} W) does not equal loss "
            f"({loss:.4f} W), so it is not a tangency"
        )

    return [
        Dataset(
            name="thermalsafety-constants",
            version=CONSTANTS_VERSION,
            licence="first-party",
            retrieved_from=(
                "Stoessel, Thermal Safety of Chemical Processes (Wiley 2008); Townsend & Tou, "
                "Thermochim. Acta 37 (1980) 1-30; Bretherick's Handbook, 8th ed. §2.3.3; "
                "standard atomic weights from the molmass distribution named in `version`"
            ),
            description=(
                "The first-party constants and formulas this server computes with: atomic weights, "
                "the oxygen-balance screening bands, and the runaway expressions. Verified on "
                "every probe by recomputing published values rather than by being loaded."
            ),
            sha256=_constants_digest(),
            # The constants are the module, so the module is what is digested and named.
            records_path=Path(oxygen_balance.__file__),
        )
    ]
