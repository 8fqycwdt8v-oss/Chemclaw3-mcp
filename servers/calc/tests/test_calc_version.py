"""Every compute result carries the version that produced it.

Chemclaw3's calculation cache and calibration ledger are addressed by `calc_version`, matched
exactly. That string is assembled from things that exist only in this process (tblite and rdkit
versions, `_HAMILTONIAN_REVISION`, `xtb --version`, pKa calibration constants). A Chemclaw3 pod
re-deriving it would get `"absent"` rather than an error and silently match no rows. So **the
derivation lives here, the string ships in every result, and nothing re-derives it.**

Asserted through the tool functions, where a projection could drop a field:

1. `calc_version` is present and non-empty on every result.
2. `calc_key` is the full four-part string wherever a key is derivable, and `None` on
   `predict_logd`.
"""

from __future__ import annotations

import re

import pytest
from chemclaw_mcp_calc import tools
from chemclaw_mcp_calc.engine import crest_cli, xtb_cli
from chemclaw_mcp_calc.engine.config import settings
from chemclaw_mcp_calc.engine.key import Keyed
from chemclaw_mcp_calc.engine.xtb_opt import optimizer_version

# `calc_type@calc_version:input_hash:params_hash`, with the two hashes being 16 hex characters —
# `stable_hash`'s width, which is itself part of the contract with Chemclaw3 (`engine/ids.py`).
KEY_SHAPE = re.compile(r"^[a-z_.]+@.+:[0-9a-f]{16}:[0-9a-f]{16}$")

# Small, fast molecules on purpose: this file runs every tool, and the point being asserted is about
# strings rather than chemistry. Acetic acid is the cheapest input that reaches the pKa acid branch
# and therefore also the logD path.
ETHANOL = "CCO"
ACETIC = "CC(=O)O"

# Tools that compute nothing and therefore carry no version: two build a geometry, one answers what
# a calculation would be stored under. Named rather than filtered by a predicate, so adding a
# fourth is a deliberate act with a reason beside it.
HELPERS = {"embed_structure", "combine_structures", "calculation_key"}

# `search_binding_modes` is excluded by cost: a metadynamics over a pair takes minutes. Its
# identity is covered by `test_calculation_key.py`.
_UNRUNNABLE_HERE = {"search_binding_modes"}


def _crest_exclusions() -> set[str]:
    """Which searches this run cannot exercise — a fact about the machine, read at call time.

    The subtraction keeps the remainder closed: a tool neither exercised nor named here fails the
    set assertion.
    """
    if crest_cli.is_available():
        return set(_UNRUNNABLE_HERE)
    return {"search_conformer_ensemble", *_UNRUNNABLE_HERE}


def _binary_exclusions() -> set[str]:
    """Which binary-only panels this run cannot exercise — a fact about the machine, not the image.

    With no `xtb` binary they are excluded here; their refusal is tested in
    `test_reactivity_panel.py`.
    """
    if xtb_cli.is_available():
        return set()
    return {"compute_atomic_descriptors", "compute_surface_potential"}


async def _every_tool_result() -> dict[str, Keyed]:
    """Call every computing tool once and return its result by tool name.

    One helper, so a new tool cannot get a fixture without being checked. Water for the primitives,
    so the whole chain runs in milliseconds. Excluded searches are subtracted from the served set
    below rather than skipped.
    """
    water = await tools.embed_structure("O")
    relaxed = await tools.relax_structure(water)
    results: dict[str, Keyed] = {
        "compute_xtb_energy": await tools.compute_xtb_energy(ETHANOL),
        "compute_electronic_properties": await tools.compute_electronic_properties(ETHANOL),
        "predict_site_reactivity": await tools.predict_site_reactivity(ETHANOL),
        **(
            {}
            if not xtb_cli.is_available()
            else {
                "compute_atomic_descriptors": await tools.compute_atomic_descriptors(ETHANOL),
                "compute_surface_potential": await tools.compute_surface_potential(ETHANOL),
            }
        ),
        "optimize_geometry": await tools.optimize_geometry("O"),
        "predict_pka": await tools.predict_pka(ACETIC),
        "predict_solubility": await tools.predict_solubility(ETHANOL),
        "predict_logd": await tools.predict_logd(ACETIC, ph=1.0),
        "predict_developability_profile": await tools.predict_developability_profile(ETHANOL),
        "relax_structure": relaxed,
        "compute_properties_at": await tools.compute_properties_at(relaxed.structure),
        "compute_fukui_at": await tools.compute_fukui_at(relaxed.structure),
        "compute_hessian": await tools.compute_hessian(relaxed.structure),
        "scan_point": await tools.scan_point(
            await tools.embed_structure(ETHANOL), [0, 1, 2, 3], 60.0
        ),
    }
    if crest_cli.is_available():
        # Water: one conformer and seconds of sampling, so the *contract* is checked without the
        # cost. What is being asserted is that a search's payload carries a `calc_version` like
        # every other calculation's — which nothing checked while the binary was absent everywhere.
        results["search_conformer_ensemble"] = await tools.search_conformer_ensemble(
            await tools.embed_structure("O")
        )
    return results


async def test_every_compute_tool_returns_a_non_empty_calc_version() -> None:
    """Every compute tool returns a non-empty `calc_version`.

    The set is asserted too, so an unlisted new tool fails, and the exclusions are named sets.
    """
    results = await _every_tool_result()
    # The helpers are excluded by name rather than by forgetting them, the searches this machine
    # cannot afford to run by `_crest_exclusions`, and the binary-only panels by
    # `_binary_exclusions` — every set stated, so the remainder is closed.
    served = (
        {tool.name for tool in await tools.server.list_tools()}
        - HELPERS
        - _crest_exclusions()
        - _binary_exclusions()
    )
    assert set(results) == served, (
        "a tool is served that this test does not exercise (or vice versa); every compute "
        "tool must be checked for calc_version, and the served surface is the list that decides"
    )
    for name, result in results.items():
        assert isinstance(result, Keyed), f"{name} returns a model that cannot carry a calc_version"
        assert result.calc_version, f"{name} returned an empty calc_version"


async def test_the_version_names_the_programs_that_actually_ran() -> None:
    """The version names the programs that actually ran, so an upgrade is a cache miss.

    - every xTB-family result names the GFN method, the resolved backend and the tblite build;
    - the two RDKit-only calculators name the rdkit build;
    - pKa names its calibration constants.
    """
    results = await _every_tool_result()
    for name in (
        "compute_xtb_energy",
        "compute_electronic_properties",
        "predict_site_reactivity",
        "optimize_geometry",
        "relax_structure",
        "compute_properties_at",
        "compute_fukui_at",
        "compute_hessian",
        "scan_point",
    ):
        version = results[name].calc_version
        assert "GFN2-xTB" in version, f"{name}: {version!r} does not name the method"
        assert "tblite-" in version, f"{name}: {version!r} does not name the tblite build"
        assert "auto" not in version, (
            f"{name}: {version!r} carries the unresolved backend name; two deployments would then "
            "share entries computed by different programs"
        )
    for name in ("predict_solubility", "predict_developability_profile"):
        assert "rdkit-" in results[name].calc_version

    pka = results["predict_pka"].calc_version
    assert "cal-0.28733:-29.3116" in pka and "u-1.6:1.0" in pka, (
        f"{pka!r} omits the calibration it was mapped through — re-tuning a slope would then serve "
        "the old pKa under the new calibration's name, and the ledger would score both as one"
    )
    # logD composes the two, and says so rather than passing itself off as either.
    logd = results["predict_logd"].calc_version
    assert logd.startswith("logd/") and "pka-" in logd and "rdkit-" in logd


async def test_the_optimizer_that_decides_the_geometry_is_in_the_optimization_version() -> None:
    """geomeTRIC chooses the stationary point, so a geomeTRIC upgrade is a different answer.

    The optimized geometry is the payload every downstream `structure_id` is built from, so the
    optimization version must name geomeTRIC. Driven through the tools; `scan_point` and
    `predict_pka` relax through an `OptSpec` without mentioning the optimizer. The negative half:
    single points, property panels and Hessians are evaluated at a given geometry and must not name
    it.
    """
    if xtb_cli.is_available() and settings.xtb_engine != "tblite":
        pytest.skip(
            "the binary's ANCopt relaxes here, not geomeTRIC; `backend_version` names xtb instead"
        )
    results = await _every_tool_result()
    expected = optimizer_version()
    for name in ("optimize_geometry", "relax_structure", "scan_point", "predict_pka"):
        assert expected in results[name].calc_version, (
            f"{name}: {results[name].calc_version!r} does not name the optimizer that chose its "
            "geometry, so a geomeTRIC upgrade serves the old stationary point under an unchanged "
            "key"
        )
    for name in ("compute_xtb_energy", "compute_electronic_properties", "compute_hessian"):
        assert "geometric" not in results[name].calc_version, (
            f"{name}: {results[name].calc_version!r} names an optimizer it never runs, which keys "
            "it on a program that cannot have moved its answer"
        )


async def test_the_key_travels_wherever_the_source_derives_one() -> None:
    """Every keyed tool carries the full four-part key; `predict_logd` carries `None`, and only it.

    Chemclaw3 never cached logD, so there is no key to derive; asserting `None` stops anyone
    inventing one that addresses a row nothing writes.
    """
    results = await _every_tool_result()
    for name, result in results.items():
        if name == "predict_logd":
            assert result.calc_key is None
            continue
        assert result.calc_key is not None, f"{name} derives a key in Chemclaw3 but returned none"
        assert KEY_SHAPE.match(result.calc_key), f"{name}: malformed key {result.calc_key!r}"
        assert result.calc_key.split("@", 1)[1].startswith(result.calc_version), (
            f"{name}: the key's version segment is not the calc_version it reported"
        )

    # The lineage that survives logD having no key of its own.
    assert results["predict_logd"].pka_calc_key is not None  # type: ignore[attr-defined]


async def test_the_key_is_stable_across_two_identical_calls() -> None:
    """A key that changed per call would address a new row every time — a cache that never hits.

    `structure_id` hashes coordinates, so an unseeded embedding or an always-moving optimizer would
    mint a new id every pass.
    """
    first = await tools.compute_xtb_energy(ETHANOL)
    second = await tools.compute_xtb_energy(ETHANOL)
    assert first.calc_key == second.calc_key
    assert first.total_energy_hartree == second.total_energy_hartree


async def test_two_spellings_of_one_molecule_share_a_key() -> None:
    """`"CCO"` and `"OCC"` are one molecule, so they must be one key.

    Canonicalization happens before embedding, because atom order steers the seeded geometry.
    """
    assert (await tools.compute_xtb_energy("CCO")).calc_key == (
        await tools.compute_xtb_energy("OCC")
    ).calc_key
    assert (await tools.predict_solubility("CCO")).calc_key == (
        await tools.predict_solubility("OCC")
    ).calc_key


async def test_a_different_parameter_is_a_different_key() -> None:
    """The whole reason the key is derived from `model_dump()`: a knob nobody keyed is a stale hit.

    A solvated calculation and a gas-phase one are different calculations, and `solvent` is an
    ordinary spec field — so it must land in `params_hash` without anyone having remembered it.
    """
    gas = await tools.compute_electronic_properties(ETHANOL)
    solvated = await tools.compute_electronic_properties(ETHANOL, solvent="water")
    assert gas.calc_key != solvated.calc_key
    assert gas.calc_version == solvated.calc_version, (
        "the solvent is a parameter, not a calculator version: it must move params_hash and leave "
        "calc_version alone, or every solvent would partition the calibration ledger"
    )
