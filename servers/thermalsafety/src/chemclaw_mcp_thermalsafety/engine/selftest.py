"""The readiness check: run the arithmetic on known cases and refuse if any answer has moved.

**Why this server has a probe at all, when it loads nothing.** Every other server in the fleet has
a corpus, a rule table or a backend that can fail between the image being built and a call arriving,
and `/healthz` exists to catch exactly that. The first version of this server passed no `readiness=`
callable on the argument that there is nothing here to load — and `tests/test_fleet.py` refused it,
correctly. A server with no probe is the constant-200 path the fleet-wide invariant exists to
forbid, and "my case is different" is how an exemption list starts.

The invariant turned out to be right on the facts as well as on the policy. This server *does* have
a corpus: `oxygen_balance.ATOMIC_WEIGHTS` and the band table beside it are vendored data that
happen to live in Python source rather than in a CSV, and a transposed digit in either is exactly
the failure `servers/props/tests/test_dataset.py` exists to catch — a wrong number in a row nobody
looks at again. Being in `.py` makes it *harder* to corrupt, not impossible, and makes the
corruption invisible to a checksum nobody computes.

So the probe **runs the thing**, which is what
`D-2026-09-12-a-readiness-check-that-does-not-run-the-thing-is-not-a-readiness-check` asks of one:
each case goes through the real public function and its answer is compared against a value published
independently of this code. A probe that merely imported the module, or checked that a version
string could be derived, would pass a table with a wrong weight in it — which is the shape that
record names as the defect, not the fix.
"""

from __future__ import annotations

import hashlib
from importlib.metadata import version
from pathlib import Path

from mcp_server_kit.datasets import Dataset

from chemclaw_mcp_thermalsafety.engine import oxygen_balance, runaway, semenov

#: The revision of the constants *this repository* writes. Bumped by hand in the commit that changes
#: a band boundary or a formula, so an operator reading `/healthz` can tell two pods apart without a
#: shell on either. It is not derived from the file's digest, because a digest answers "are these
#: the same" and a version answers "which one is newer".
# 1.1.0: the atomic weights stopped being transcribed and are read from `molmass` instead.
# Every one of them moved in its last decimals, which is a different table serving the same
# answers and is exactly what this string exists to let an operator tell apart.
_FIRST_PARTY_REVISION = "1.1.0"

#: What `/healthz` publishes: the revision above **and the distribution the atomic weights now come
#: from**. The second half is not decoration, and this string carried only the first for a wave
#: (`D-2026-09-16-a-version-that-cannot-see-its-own-table-is-not-a-version`).
#:
#: The commit that made 1.1.0 what it is took `ATOMIC_WEIGHTS` out of this repository and into
#: `molmass`, and neither half of the pair below could see that: the version is hand-bumped, and the
#: digest is over `oxygen_balance.py`, which now holds a *comprehension* rather than seventeen
#: floats. `uv.lock` then resolves **two** molmass releases on purpose — 2026.1.8 below Python 3.12
#: and 2026.8.15 at or above it — so two pods can legitimately hold different weight tables and,
#: before this, would have published the same `thermalsafety-constants@1.1.0`. Measured across both
#: releases the seventeen weights are byte-identical today, which makes this structural rather than
#: observable, and is exactly the window in which it is cheap to fix: the same argument
#: `engine_version()` makes one server over, about the same kind of adopted table.
CONSTANTS_VERSION = f"{_FIRST_PARTY_REVISION}+molmass-{version('molmass')}"

#: `(formula, published OB%)`. Every value is from the explosives literature and was written down
#: independently of this code — which is what makes agreement evidence about the table rather than
#: about the probe. Deliberately small: a readiness check runs on every kubelet probe, and three
#: cases spanning near-zero, moderately deficient and strongly deficient exercise every band
#: boundary the screen can be wrong about.
_PUBLISHED_BALANCES: tuple[tuple[str, float], ...] = (
    ("C3H5N3O9", 3.5),  # nitroglycerine
    ("C7H5N3O6", -74.0),  # TNT
    ("C6H12O6", -106.6),  # glucose
)

#: The tolerance the published values' own rounding accounts for, in percentage points. Tight enough
#: that a wrong atomic weight fails: carbon wrong in its first decimal moves TNT by ~0.4 points.
_BALANCE_TOLERANCE = 0.15


class SelfTestFailed(RuntimeError):
    """This build's arithmetic does not agree with the values it was verified against.

    A `RuntimeError` rather than `ThermalInputError`: nothing a caller passed is wrong, the pod is.
    `connector_app` classifies it as `CAUSE_FAILED`, which is permanent — correctly, because a table
    with a wrong number in it does not get better under less load, and the pod must be kept out of
    its Service until it is replaced.
    """


def _constants_digest() -> str:
    """The SHA-256 of this server's numbers: the module's source **and the weights it resolved**.

    Published on `/healthz` for the same reason a corpus's checksum is: two pods claiming the same
    version can be shown to be serving the same table, which a version string alone cannot do.

    **The source alone stopped being that table and this function did not notice.** Since the
    weights moved to `molmass`, `oxygen_balance.py` holds
    `{symbol: ELEMENTS[symbol].mass for symbol in sorted(ALLOWED_ELEMENTS)}` — seventeen bytes of
    comprehension standing in for seventeen numbers that live in another distribution. So the digest
    answered "are these two pods running the same *code*", which they always are inside one image,
    and said nothing about the values the code resolved
    (`D-2026-09-16-a-version-that-cannot-see-its-own-table-is-not-a-version`).

    Hashing the resolved values rather than the installed distribution's version is deliberate and
    is the stronger of the two: `CONSTANTS_VERSION` already names the distribution, and a digest's
    job is the question a version cannot answer — whether the numbers actually agree. It also
    covers the case a version string never could, which is a release that changes a weight without
    changing the fields this server reads.

    The weights are serialised with `repr` at full float precision and in sorted key order, so the
    digest moves when a value moves and not when a dict happens to be built in another order.
    """
    source = Path(oxygen_balance.__file__)
    table = repr(sorted(oxygen_balance.ATOMIC_WEIGHTS.items())).encode("utf-8")
    return hashlib.sha256(source.read_bytes() + b"\n" + table).hexdigest()


def verify() -> list[Dataset]:
    """Run one case through each engine module and return what this pod verified.

    Raises:
        SelfTestFailed: an answer disagrees with the value it was checked against, which means the
            constants this build serves are not the ones that were reviewed.
    """
    for formula, expected in _PUBLISHED_BALANCES:
        computed = oxygen_balance.oxygen_balance(formula).oxygen_balance_percent
        if abs(computed - expected) > _BALANCE_TOLERANCE:
            raise SelfTestFailed(
                f"oxygen balance for {formula} computed {computed:.2f}%, published {expected}% "
                f"(tolerance {_BALANCE_TOLERANCE}) — the atomic-weight table or the formula in "
                "engine/oxygen_balance.py is not the one this build was verified against"
            )

    # 150 kJ/mol over 10 mol into 50 kg at 1.9 kJ/(kg·K) is 1500/95 = 15.789 K, computed on paper.
    # The one case that would catch a unit error in the adiabatic path, which is where this server's
    # numbers are most consequential and most easily wrong by a factor of a thousand.
    rise = runaway.adiabatic_temperature_rise(
        heat_of_reaction_kj_per_mol=-150.0, moles=10.0, mass_kg=50.0, specific_heat_kj_per_kg_k=1.9
    )
    if abs(rise - 1500.0 / 95.0) > 1e-6:
        raise SelfTestFailed(
            f"the adiabatic temperature rise for the reference case computed {rise:.4f} K where "
            "the hand-computed value is 15.7895 K"
        )

    # The Semenov root, checked against its own defining condition rather than a literal: at the
    # tangency the generation equals the loss. A bisection that stopped early or ran off its bracket
    # fails this without anybody having to write down what the answer should be.
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
            # There is no separate records file: the constants *are* the module, so the module is
            # what is digested and named. Pointing this at a file that does not exist would be the
            # provenance record that quietly says nothing, one field over.
            records_path=Path(oxygen_balance.__file__),
        )
    ]
