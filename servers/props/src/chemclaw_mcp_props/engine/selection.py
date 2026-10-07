"""Solvent swaps: filter on what must hold, then rank on how close the replacement is.

Constraints (greenness band, boiling range, peroxide formers, ICH class) are filters and never
traded against similarity, so a solubility gain cannot buy a hazard. Survivors are ranked by Hansen
distance alone; a candidate failing a filter is still returned, naming the filter. Hansen distance
is a solubility argument and says nothing about reactivity.
"""

from __future__ import annotations

from dataclasses import dataclass

from chemclaw_mcp_props.engine.correlations import hansen_distance
from chemclaw_mcp_props.engine.records import Solvent, all_solvents

# Worst-to-best is a real ordering here, not a label: a swap that moves *up* this list is a
# regression, and the default filter refuses one.
BAND_RANK = {
    "recommended": 0,
    "problematic": 1,
    "hazardous": 2,
    "highly_hazardous": 3,
}

# Hansen's own rule of thumb, kept as named constants so the tool can explain its own wording.
CLOSE_RA = 4.0
DISTANT_RA = 8.0


@dataclass(frozen=True, slots=True)
class SwapCandidate:
    """One possible replacement, with every reason it might be rejected already spelled out."""

    name: str
    hansen_distance: float
    similarity: str
    bp_c: float
    bp_delta_c: float
    greenness_band: str
    greenness_change: str
    ich_class: str
    ich_limit_ppm: float | None
    flash_point_c: float | None
    water_miscibility: str
    peroxide_former: bool
    hazard_flags: tuple[str, ...]
    blockers: tuple[str, ...]

    @property
    def passes(self) -> bool:
        """Whether this candidate satisfied every constraint the caller stated."""
        return not self.blockers


def _similarity(distance: float) -> str:
    """Hansen's rule of thumb, in words, so the model does not have to invent a threshold."""
    if distance <= CLOSE_RA:
        return "close — dissolves broadly similar solutes"
    if distance <= DISTANT_RA:
        return "moderate — expect solubility differences worth checking at the bench"
    return "distant — do not assume the solute behaves the same way"


def _greenness_change(reference: Solvent, candidate: Solvent) -> str:
    """Whether the swap improves, holds or worsens the greenness band."""
    delta = BAND_RANK[candidate.greenness_band] - BAND_RANK[reference.greenness_band]
    if delta < 0:
        return f"better ({reference.greenness_band} -> {candidate.greenness_band})"
    if delta == 0:
        return f"unchanged ({candidate.greenness_band})"
    return f"worse ({reference.greenness_band} -> {candidate.greenness_band})"


def swap_candidates(
    reference: Solvent,
    *,
    top_n: int = 5,
    allow_worse_greenness: bool = False,
    min_bp_c: float | None = None,
    max_bp_c: float | None = None,
    exclude_peroxide_formers: bool = False,
    require_water_miscibility: str | None = None,
    max_ich_class: str | None = None,
) -> list[SwapCandidate]:
    """Rank replacements for `reference`, nearest in Hansen space first.

    Args:
        reference: The solvent being replaced.
        top_n: How many candidates to return; blocked ones come after passing ones.
        allow_worse_greenness: When false, a candidate in a worse greenness band is blocked.
        min_bp_c: Reject candidates boiling below this, in °C.
        max_bp_c: Reject candidates boiling above this, in °C.
        exclude_peroxide_formers: Reject ethers and other peroxide formers outright.
        require_water_miscibility: One of `miscible`, `partial`, `immiscible`.
        max_ich_class: `1`, `2` or `3`; rejects a worse ICH Q3C class. Unlisted solvents are
        flagged,
            never rejected.

    Returns:
        Up to `top_n` candidates, passing ones first and each ordered by Hansen distance.
    """
    scored: list[SwapCandidate] = []
    for candidate in all_solvents():
        if candidate.name == reference.name:
            continue
        distance = hansen_distance(reference, candidate)
        blockers: list[str] = []
        if not allow_worse_greenness and (
            BAND_RANK[candidate.greenness_band] > BAND_RANK[reference.greenness_band]
        ):
            blockers.append(f"greenness band worsens to {candidate.greenness_band}")
        if min_bp_c is not None and candidate.bp_c < min_bp_c:
            blockers.append(f"boils at {candidate.bp_c} °C, below the required {min_bp_c} °C")
        if max_bp_c is not None and candidate.bp_c > max_bp_c:
            blockers.append(f"boils at {candidate.bp_c} °C, above the allowed {max_bp_c} °C")
        if exclude_peroxide_formers and candidate.peroxide_former:
            blockers.append("forms peroxides on storage")
        if (
            require_water_miscibility is not None
            and candidate.water_miscibility != require_water_miscibility
        ):
            blockers.append(
                f"is {candidate.water_miscibility} with water, not {require_water_miscibility}"
            )
        if (
            max_ich_class is not None
            and candidate.ich_class in {"1", "2", "3"}
            and int(candidate.ich_class) < int(max_ich_class)
        ):
            blockers.append(f"is ICH Q3C class {candidate.ich_class}")
        scored.append(
            SwapCandidate(
                name=candidate.name,
                hansen_distance=round(distance, 2),
                similarity=_similarity(distance),
                bp_c=candidate.bp_c,
                bp_delta_c=round(candidate.bp_c - reference.bp_c, 1),
                greenness_band=candidate.greenness_band,
                greenness_change=_greenness_change(reference, candidate),
                ich_class=candidate.ich_class,
                ich_limit_ppm=candidate.ich_limit_ppm,
                flash_point_c=candidate.flash_point_c,
                water_miscibility=candidate.water_miscibility,
                peroxide_former=candidate.peroxide_former,
                hazard_flags=candidate.hazard_flags,
                blockers=tuple(blockers),
            )
        )
    scored.sort(key=lambda item: (bool(item.blockers), item.hansen_distance))
    return scored[:top_n]
