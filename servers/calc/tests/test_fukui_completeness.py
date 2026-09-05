"""What this server caches must not depend on an argument its cache key excludes.

`engine/identity.py` keys `xtb.fukui` on the spec and the geometry alone, and says why: the three
single points do not depend on `mode`, and `top_n` "only truncates the ranking". Chemclaw3 relies
on exactly that — it sends **neither** argument, re-ranks locally with `ranked_for`, and its own
docstring states the contract as "the row holds every atom, so asking for more sites re-slices a
cached result instead of running three more single points".

It did not. Both Fukui tools sliced `sites[:limit]` *before* returning, so the payload Chemclaw3
stores under a mode-and-top_n-free key was truncated by both. Measured on a real GFN2 run of aspirin
(21 atoms): the engine produced 21 sites, the stored row held 15, and six atoms — indices 0, 1, 3,
5, 7 and 15 — were absent from a row that still reported `total_atoms: 21`. Nothing signalled it,
and `ranked_for` re-sorting that row cannot recover an atom that is not in it: a nucleophilic
top-15 read off an electrophilically-truncated row is missing four of the sites it should contain.

The invariant the tests below hold is narrower than "the payload is a function of the key", because
one thing genuinely may vary: **ordering is a permutation and loses nothing**, which is what makes
`ranked_for` sound. Truncation is a loss. So: no argument outside the key may remove a site.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest
from chemclaw_mcp_calc import tools
from chemclaw_mcp_calc.engine import xtb_props
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
    """The whole defect, as behaviour: `mode` is outside the key, so it reorders and nothing more.

    This is the assertion that fails at HEAD in the way that matters — the two rankings truncated to
    two *different* fifteens, so which atoms a cached row contained depended on which mode happened
    to miss the cache first.
    """
    nucleophilic = asyncio.run(tools.predict_site_reactivity(_ASPIRIN, "nucleophilic"))
    assert {site.index for site in nucleophilic.sites} == {site.index for site in from_smiles.sites}
    assert [site.index for site in nucleophilic.sites] != [
        site.index for site in from_smiles.sites
    ], "a re-rank that changes no order would make the set comparison above vacuous"


@pytest.mark.parametrize("tool", ["predict_site_reactivity", "compute_fukui_at"])
def test_neither_fukui_tool_takes_an_argument_that_could_truncate_its_row(tool: str) -> None:
    """`top_n` is gone from the surface, not merely ignored inside it.

    A parameter kept for compatibility and then not applied is the shape that grows the defect back:
    the next person to see an unused argument wires it up. The slice belongs to the caller that
    presents the answer to a chemist, and Chemclaw3 already performs it.
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
    """Completeness costs response size, and the bound on it is the one `Structure` already applies.

    Measured on this fixture: 185 bytes of JSON per site (4,469 for aspirin's 21, envelope
    included), so the full list at `xtb_max_atoms` (500) extrapolates to ~94 kB — an order of
    magnitude inside the 1 MB body cap, and far smaller than `compute_hessian`'s ~2.2 MB at its own
    ceiling. There is no separate bound to add; the atom ceiling is it.
    """
    per_site = len(from_smiles.sites[0].model_dump_json())
    assert per_site < 250, f"{per_site} bytes per site is bigger than this bound assumed"
    projected = len(from_smiles.model_dump_json()) + (500 - len(from_smiles.sites)) * per_site
    assert projected < 200_000, f"a full 500-atom row projects to {projected} bytes"
