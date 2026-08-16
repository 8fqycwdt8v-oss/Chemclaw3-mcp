"""Every compute result carries the version that produced it. This is the port's whole point.

## The failure this file exists to prevent, stated once

Chemclaw3 keeps two stores this server's answers belong in: the calculation cache
(`calculation_results`, addressed by `calc_type@calc_version:input_hash:params_hash`) and the
**calibration ledger** (`predictions`, unique on `(calc_type, calc_version, input_hash)`, read with
an *exact* `calc_version` predicate — no version pooling, deliberately, so a v1 that ran high is
never averaged with a v2 that ran low).

`calc_version` is assembled from things that live in *this* process and nowhere else:

- the installed `tblite` and `rdkit` distribution versions,
- `_HAMILTONIAN_REVISION`, a constant in `engine/xtb_engine.py`,
- `xtb --version`, a subprocess, when the backend resolves to the binary,
- and, for pKa, seven `settings.*` calibration constants.

After the split a Chemclaw3 pod has neither distribution installed and no xtb binary. If it
re-derived the string it would **not** get an exception. `xtb_cli.binary_version()` returns the
literal string `"absent"` rather than raising, so the reconstruction would be well-formed, would
match zero ledger rows, and `calculator_trust("pka")` would report `UNCALIBRATED`, n=0 — a confident
answer about a calibration that is merely unreachable. Silent, not loud, which is why it needs a
test rather than a convention.

So: **the derivation lives here, the string ships in every result, and nothing re-derives it.**

## What is asserted

Two properties, over all nine tools, driven through the tool functions themselves rather than the
engine — because the tool layer is where a `model_copy(update=...)` or a summary projection could
drop a field while every engine test stayed green (`OptimizationSummary.of` is exactly that shape).

1. `calc_version` is present and non-empty on every result.
2. `calc_key` is the full four-part string on the eight calculators whose Chemclaw3 source derives a
   key, and `None` on the one that does not (`predict_logd` — see its module docstring).
"""

from __future__ import annotations

import re

import pytest
from chemclaw_mcp_calc import tools
from chemclaw_mcp_calc.engine.key import Keyed

# `calc_type@calc_version:input_hash:params_hash`, with the two hashes being 16 hex characters —
# `stable_hash`'s width, which is itself part of the contract with Chemclaw3 (`engine/ids.py`).
KEY_SHAPE = re.compile(r"^[a-z_.]+@.+:[0-9a-f]{16}:[0-9a-f]{16}$")

# Small, fast molecules on purpose: this file runs every tool, and the point being asserted is about
# strings rather than chemistry. Acetic acid is the cheapest input that reaches the pKa acid branch
# and therefore also the logD path.
ETHANOL = "CCO"
ACETIC = "CC(=O)O"


async def _every_tool_result() -> dict[str, Keyed]:
    """Call all nine tools once and return their results by tool name.

    One helper rather than nine fixtures because the assertions below are the *same* assertion nine
    times — a per-tool fixture would make it possible to add a tenth tool and a tenth fixture
    without the new tool ever being checked, which is the failure mode this file is guarding against
    in the first place.

    `compute_thermochemistry` runs on water: three atoms, so the 6N finite-difference Hessian is 18
    single points and the whole call is milliseconds. Every other tool takes ethanol or acetic acid.
    """
    return {
        "compute_xtb_energy": await tools.compute_xtb_energy(ETHANOL),
        "compute_electronic_properties": await tools.compute_electronic_properties(ETHANOL),
        "predict_site_reactivity": await tools.predict_site_reactivity(ETHANOL),
        "optimize_geometry": await tools.optimize_geometry("O"),
        "compute_thermochemistry": await tools.compute_thermochemistry("O", symmetry_number=2),
        "predict_pka": await tools.predict_pka(ACETIC),
        "predict_solubility": await tools.predict_solubility(ETHANOL),
        "predict_logd": await tools.predict_logd(ACETIC, ph=1.0),
        "predict_developability_profile": await tools.predict_developability_profile(ETHANOL),
    }


async def test_every_compute_tool_returns_a_non_empty_calc_version() -> None:
    """The one invariant this port turns on. Nine tools, one property, no exceptions.

    Also asserts the *count*, so adding a tenth tool to `tools.py` without adding it here fails
    rather than being silently unchecked.
    """
    results = await _every_tool_result()
    # `calculation_key` is excluded by name rather than by forgetting it: it returns an identity
    # rather than a computed value, so it has a `calc_version` and nothing to compute one *for*.
    # Naming it here is what keeps the rest of the assertion a closed set.
    served = {tool.name for tool in await tools.server.list_tools()} - {"calculation_key"}
    assert set(results) == served, (
        "a tool is served that this test does not exercise (or vice versa); every compute "
        "tool must be checked for calc_version, and the served surface is the list that decides"
    )
    for name, result in results.items():
        assert isinstance(result, Keyed), f"{name} returns a model that cannot carry a calc_version"
        assert result.calc_version, f"{name} returned an empty calc_version"


async def test_the_version_names_the_programs_that_actually_ran() -> None:
    """A version string that names nothing is as useless as an absent one.

    Each family is checked for the component it *must* mention, because that is what makes an
    upgrade a cache miss on the other side rather than a silent stale hit:

    - every xTB-family result names the GFN method, the resolved backend and the tblite build;
    - the two RDKit-only calculators name the rdkit build;
    - pKa names its calibration constants, which is the half no program version can see.
    """
    results = await _every_tool_result()
    for name in (
        "compute_xtb_energy",
        "compute_electronic_properties",
        "predict_site_reactivity",
        "optimize_geometry",
        "compute_thermochemistry",
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


async def test_the_key_travels_wherever_the_source_derives_one() -> None:
    """Eight of nine carry the full four-part key; `predict_logd` carries `None`, and only it.

    The exception is not an omission: Chemclaw3 never gave logD a cache entry, because the expensive
    half was already memoized as a pKa and Crippen LogP is sub-millisecond, so there is no key
    derivation to port. Asserting the `None` explicitly is what stops somebody "fixing" it by
    inventing one — an invented `logd@...` key would address a row nothing on the other side writes.
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

    The realistic way to break this is not randomness but the geometry: `structure_id` hashes the
    coordinates, so an embedding that is not seeded, or an optimizer that always moves something,
    mints a new id on every pass. Both have happened in this code's history.
    """
    first = await tools.compute_xtb_energy(ETHANOL)
    second = await tools.compute_xtb_energy(ETHANOL)
    assert first.calc_key == second.calc_key
    assert first.total_energy_hartree == second.total_energy_hartree


async def test_two_spellings_of_one_molecule_share_a_key() -> None:
    """`"CCO"` and `"OCC"` are one molecule, so they must be one key.

    This is the property `engine/chem.require_canonical_smiles` exists for, checked at the level
    that matters: canonicalization happens *before* embedding, because atom order steers the seeded
    geometry, so a canonicalizer applied only to the key would produce one key for two different
    structures.
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


@pytest.mark.parametrize("temperature", [298.15, 350.0])
async def test_a_state_variable_moves_the_thermochemistry_key(temperature: float) -> None:
    """Temperature belongs to the thermochemistry's key and not to the Hessian's underneath it.

    Asserted through the tool because that projection is where it would be lost. The Hessian spec is
    deliberately narrower (`ThermoSpec.hessian_spec`), which is what lets Chemclaw3 hit the
    expensive cache and miss the cheap one when a chemist asks for a second temperature.
    """
    result = await tools.compute_thermochemistry("O", symmetry_number=2, temperature_k=temperature)
    assert result.temperature_k == temperature
    assert result.calc_key is not None and str(temperature) not in result.calc_version, (
        "a state variable must ride in params_hash, never in calc_version — otherwise every "
        "temperature would be a separate calibration"
    )
