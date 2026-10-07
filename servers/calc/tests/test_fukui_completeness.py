"""What this server caches must not depend on an argument its cache key excludes.

`xtb.fukui` is keyed on the spec and geometry alone, and Chemclaw3 re-ranks the cached row
locally with `ranked_for`, relying on the row holding every atom. Ordering is a permutation and
loses nothing; truncation is a loss. So no argument outside the key may remove a site.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest
from chemclaw_mcp_calc import tools
from chemclaw_mcp_calc.engine import xtb_props
from chemclaw_mcp_calc.engine.config import settings
from chemclaw_mcp_calc.engine.identity import COMPUTE_TOOLS
from chemclaw_mcp_calc.engine.structure import Structure, structure_from_smiles

# Aspirin: 21 atoms with hydrogens, comfortably past the 15 the truncation used to leave behind.
_ASPIRIN = "CC(=O)Oc1ccccc1C(=O)O"


@pytest.fixture(scope="module")
def geometry() -> Structure:
    """The geometry `compute_fukui_at` is driven on, embedded once."""
    return structure_from_smiles(_ASPIRIN, optimize=True)


@pytest.fixture(scope="module")
def from_smiles() -> xtb_props.SiteReactivityResult:
    """One `predict_site_reactivity` call — three SCFs, shared across the tests below."""
    result: xtb_props.SiteReactivityResult = asyncio.run(tools.predict_site_reactivity(_ASPIRIN))
    return result


def test_the_smiles_route_returns_every_atom_it_counted(
    from_smiles: xtb_props.SiteReactivityResult,
) -> None:
    """`total_atoms` is what the row holds, not what it was drawn from before being cut."""
    assert len(from_smiles.sites) == from_smiles.total_atoms
    assert {site.index for site in from_smiles.sites} == set(range(from_smiles.total_atoms))


def test_the_geometry_route_returns_every_atom_it_counted(geometry: Structure) -> None:
    """The primitive Chemclaw3's ensemble activities call per conformer, held to the same rule."""
    result = asyncio.run(tools.compute_fukui_at(geometry))
    assert len(result.sites) == result.total_atoms


def test_every_mode_returns_the_same_sites_in_a_different_order(
    from_smiles: xtb_props.SiteReactivityResult,
) -> None:
    """`mode` is outside the key, so it reorders the sites and changes nothing else."""
    nucleophilic = asyncio.run(tools.predict_site_reactivity(_ASPIRIN, "nucleophilic"))
    assert {site.index for site in nucleophilic.sites} == {site.index for site in from_smiles.sites}
    assert [site.index for site in nucleophilic.sites] != [
        site.index for site in from_smiles.sites
    ], "a re-rank that changes no order would make the set comparison above vacuous"


@pytest.mark.parametrize("tool", ["predict_site_reactivity", "compute_fukui_at"])
def test_neither_fukui_tool_takes_an_argument_that_could_truncate_its_row(tool: str) -> None:
    """`top_n` is gone from the surface, not merely ignored inside it.

    An unused parameter invites someone to wire it up again. The caller presenting the answer
    slices.
    """
    assert "top_n" not in inspect.signature(getattr(tools, tool)).parameters
    accepted, _ = COMPUTE_TOOLS[tool]
    assert "top_n" not in accepted, (
        "`calculation_key` must refuse an argument the compute tool does not take, or it would "
        "answer with the key of a different call"
    )


def test_the_row_is_bounded_by_the_atom_ceiling_rather_than_by_a_slice(
    from_smiles: xtb_props.SiteReactivityResult,
) -> None:
    """The full site list is bounded by the atom ceiling rather than by a slice.

    At `xtb_max_atoms` the projected row stays well inside the body cap, so no separate bound is
    needed. The projection reads the configured ceiling.
    """
    ceiling = settings.xtb_max_atoms
    per_site = len(from_smiles.sites[0].model_dump_json())
    assert per_site < 250, f"{per_site} bytes per site is bigger than this bound assumed"
    projected = len(from_smiles.model_dump_json()) + (ceiling - len(from_smiles.sites)) * per_site
    assert projected < 200_000, f"a full {ceiling}-atom row projects to {projected} bytes"
