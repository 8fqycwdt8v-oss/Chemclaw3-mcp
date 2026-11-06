"""`CalculationKey` — the identity Chemclaw3 addresses a stored calculation by, derived here.

Derived here because its inputs (backend versions, `xtb --version`, calibration settings, the
embedded geometry's hash) exist only in this process; Chemclaw3 re-derives neither part. Every
result carries `calc_version` and, where derivable, the flat
`calc_type@calc_version:input_hash:params_hash`.

The key itself is the fleet's (`mcp_server_kit.keys.CalculationKey`); what is `calc`'s is the epoch
folded into it. `CALCULATION_EPOCH` need not agree with Chemclaw3's: `remote_key` folds Chemclaw3's
epoch over this `params_hash`, so the two compose. No store lives here; a key is provenance, never
looked up.
"""

from __future__ import annotations

from typing import Any, Self

from mcp_server_kit import keys
from pydantic import BaseModel, Field

__all__ = ["CALCULATION_EPOCH", "CalculationKey", "Keyed"]

# The version of ChemClaw's own contribution to a stored result, folded into every key by
# `CalculationKey.build`. Bump it when our arithmetic or a persisted payload's shape changes in a
# way no backend `calc_version` can see. Bumped in both repositories together by convention.
#
#   1 — introduced.
#   2 — the per-atom reactivity panel gained required global and local descriptors, so epoch-1 rows
#       are incomplete.
CALCULATION_EPOCH = "2"


class CalculationKey(keys.CalculationKey):
    """Content-addressed identity of a calculation, versioned by the calculator.

    The fleet's key with `calc`'s `CALCULATION_EPOCH` folded in by default, read when `build` runs.

    **`calc_version` names every program whose output survives into the payload, and no program
    that does not run** — a calculation that composes two programs names both, because either one
    moving changes the number.

    **The converse — same key, same answer — is what a reader assumes and is not what this states.**
    It holds for every calculation on this server bar one: a CREST search is a stochastic
    metadynamics with no seed set, so its key identifies the *settings* and the cache is what makes
    the ensemble reproducible. `CrestSpec` in `engine/xtb_spec.py` carries the argument.
    """

    @classmethod
    def build(
        cls,
        calc_type: str,
        calc_version: str,
        inputs: Any,
        params: Any = None,
        *,
        epoch: str | None = None,
    ) -> Self:
        """The kit's `build`, with `CALCULATION_EPOCH` as the epoch unless one is given."""
        return super().build(
            calc_type,
            calc_version,
            inputs,
            params,
            epoch=CALCULATION_EPOCH if epoch is None else epoch,
        )


class Keyed(BaseModel):
    """The two provenance fields every compute result on this server carries.

    A base class rather than two fields repeated nine times, so a new result model cannot ship
    without them and `tests/test_calc_version.py` has one property to assert over every tool.

    `calc_version` is **required and non-empty** by declaration. That is the invariant: a result
    that reached a caller without it would invite the caller to derive one, and the client-side
    derivation is precisely the silent failure this port exists to prevent — `xtb_cli
    .binary_version()` answers `"absent"` instead of raising, so a locally-built string is
    well-formed, matches zero rows in Chemclaw3's `predictions` table, and turns
    `calculator_trust("pka")` into a confident `UNCALIBRATED`.

    `calc_key` is the flat `calc_type@calc_version:input_hash:params_hash` form, and it is `None`
    only where the ported source derives no key. Exactly one calculator is in that position —
    `logd`, which composes a cached pKa with an uncached Crippen descriptor and was never keyed as a
    calculation of its own. Everything else carries one.
    """

    calc_version: str = Field(min_length=1)
    calc_key: str | None = None
