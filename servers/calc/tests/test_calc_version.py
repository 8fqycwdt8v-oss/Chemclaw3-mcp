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

# Tools that compute nothing and therefore carry no version: two build a geometry, one answers what
# a calculation would be stored under. Named rather than filtered by a predicate, so adding a
# fourth is a deliberate act with a reason beside it.
HELPERS = {"embed_structure", "combine_structures", "calculation_key"}

# The two CREST searches. They key and refuse like everything else, but they cannot *run* without a
# binary this image does not ship, so they are exercised in `test_engine.py` (the refusal) and
# `test_calculation_key.py` (the identity) rather than here.
CREST_TOOLS = {"search_conformer_ensemble", "search_binding_modes"}


async def _every_tool_result() -> dict[str, Keyed]:
    """Call every computing tool once and return its result by tool name.

    One helper rather than a fixture per tool because the assertions below are the *same* assertion
    twelve times — a per-tool fixture would make it possible to add a thirteenth tool and a
    thirteenth fixture without the new tool ever being checked, which is the failure mode this file
    guards against in the first place.

    Water for the primitives: three atoms, so a Hessian is 18 single points and the whole chain runs
    in milliseconds. The CREST searches are absent from this dict and subtracted from the served set
    below rather than skipped silently — a skip would stop noticing the day the binary *is* shipped.
    """
    water = await tools.embed_structure("O")
    relaxed = await tools.relax_structure(water)
    return {
        "compute_xtb_energy": await tools.compute_xtb_energy(ETHANOL),
        "compute_electronic_properties": await tools.compute_electronic_properties(ETHANOL),
        "predict_site_reactivity": await tools.predict_site_reactivity(ETHANOL),
        "optimize_geometry": await tools.optimize_geometry("O"),
        "predict_pka": await tools.predict_pka(ACETIC),
        "predict_solubility": await tools.predict_solubility(ETHANOL),
        "predict_logd": await tools.predict_logd(ACETIC, ph=1.0),
        "predict_developability_profile": await tools.predict_developability_profile(ETHANOL),
        "relax_structure": relaxed,
        "compute_properties_at": await tools.compute_properties_at(relaxed.structure),
        "compute_hessian": await tools.compute_hessian(relaxed.structure),
        "scan_point": await tools.scan_point(
            await tools.embed_structure(ETHANOL), [0, 1, 2, 3], 60.0
        ),
    }


async def test_every_compute_tool_returns_a_non_empty_calc_version() -> None:
    """The one invariant this port turns on. Every calculation, one property, no exceptions.

    Also asserts the *set*, so adding a tool to `tools.py` without adding it here fails rather than
    being silently unchecked — and the two exclusions are named sets rather than a predicate, so
    growing either is a deliberate act.
    """
    results = await _every_tool_result()
    # The helpers are excluded by name rather than by forgetting them, and the CREST searches
    # because no binary is installed — both sets stated, so the remainder is closed.
    served = {tool.name for tool in await tools.server.list_tools()} - HELPERS - CREST_TOOLS
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
        "relax_structure",
        "compute_properties_at",
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
