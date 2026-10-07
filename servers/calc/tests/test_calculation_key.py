"""`calculation_key` returns the identity the compute tool will produce — the design rests on this.

Chemclaw3 keeps the cache and needs the key before paying for the calculation:

```python
hit = await store.get(key)          # <- the key is an argument, not a result
if hit is not None:
    return hit.result, True
result = await compute()
```

If the two keys disagreed, lookups would miss forever and ledger rows would be written under
versions nothing reconciles, with no error. So parity is asserted for every tool against the
real compute path.
"""

from __future__ import annotations

from typing import Any

import pytest
from chemclaw_mcp_calc import tools
from chemclaw_mcp_calc.engine import crest_cli, xtb_cli, xtb_engine
from chemclaw_mcp_calc.engine.identity import COMPUTE_TOOLS, calculation_identity
from chemclaw_mcp_calc.engine.key import CalculationKey

# Tools that compute nothing, so there is nothing to derive an identity *for*: two build a geometry
# and one is this probe itself.
HELPERS = {"embed_structure", "combine_structures", "calculation_key"}


def sweep_arguments(tool: str, accepts: frozenset[str], geometry: dict[str, Any]) -> dict[str, Any]:
    """Minimal valid arguments for `tool`, for the tests that sweep the whole table.

    Built from the declared `accepts` set, so a new tool is exercised without anyone adding a case.
    """
    arguments: dict[str, Any] = (
        {"structure": geometry} if "structure" in accepts else {"smiles": "CC(=O)O"}
    )
    if tool == "scan_point":
        arguments |= {"atoms": [0, 1, 2], "value": 1.5}
    return arguments


# Tools whose key is not derivable from their arguments and return `None` on both sides of the
# parity check. Joining this set is a real loss that must be argued. `predict_logd` was never
# cached: its expensive half is a cached pKa. A composite whose key would name its own output is
# decomposed into keyed primitives instead of being added here.
WITHOUT_A_DERIVABLE_KEY = frozenset({"predict_logd"})

# Tools whose identity refuses on an image without the program that runs them: the two CREST
# searches and the two binary-only xTB panels (`atomic`, `surface`), which `_FIXED_BACKEND` pins to
# the `xtb` binary whatever the configured backend. They refuse exactly where their compute path
# does. `test_the_tools_that_need_a_binary_refuse_rather_than_key` drives every member and every
# non-member.
NEEDS_A_BINARY = frozenset(
    {
        "search_conformer_ensemble",
        "search_binding_modes",
        "compute_atomic_descriptors",
        "compute_surface_potential",
    }
)

# One argument set per tool, used identically on both sides, which is the property. Small
# molecules, since every calculation runs. `predict_site_reactivity` carries `mode`, an accepted
# but unkeyed argument, to prove it still reaches the same row.
CASES: list[tuple[str, dict[str, Any], Any]] = [
    ("compute_xtb_energy", {"smiles": "CCO"}, tools.compute_xtb_energy),
    ("compute_xtb_energy", {"smiles": "CC(=O)[O-]", "charge": -1}, tools.compute_xtb_energy),
    (
        "compute_electronic_properties",
        {"smiles": "CCO", "solvent": "water"},
        tools.compute_electronic_properties,
    ),
    (
        "predict_site_reactivity",
        {"smiles": "CCO", "mode": "nucleophilic"},
        tools.predict_site_reactivity,
    ),
    ("optimize_geometry", {"smiles": "O"}, tools.optimize_geometry),
    ("optimize_geometry", {"smiles": "O", "solvent": "thf"}, tools.optimize_geometry),
    ("predict_pka", {"smiles": "CC(=O)O"}, tools.predict_pka),
    ("predict_solubility", {"smiles": "CCO"}, tools.predict_solubility),
    ("predict_logd", {"smiles": "CC(=O)O", "ph": 1.0}, tools.predict_logd),
    (
        "predict_developability_profile",
        {"smiles": "CCO"},
        tools.predict_developability_profile,
    ),
]


@pytest.mark.parametrize(
    ("tool", "arguments", "compute"), CASES, ids=[f"{c[0]}-{c[1]['smiles']}" for c in CASES]
)
async def test_the_key_derived_up_front_is_the_key_the_result_carries(
    tool: str, arguments: dict[str, Any], compute: Any
) -> None:
    """Derive, then compute, then compare: the one property the remote-cache design rests on.

    `calc_version` is what the ledger matches exactly and `calc_key` addresses the cache, so a
    divergence in either is a silent cost.
    """
    identity = await tools.calculation_key(tool, arguments)
    result = await compute(**arguments)

    assert identity.tool == tool
    assert identity.calc_version == result.calc_version, (
        f"{tool}: the version derived up front is not the one the result carries; a ledger row "
        "written under one and read under the other is unreachable, and reads as UNCALIBRATED"
    )
    if tool in WITHOUT_A_DERIVABLE_KEY:
        assert identity.calc_key is None
        assert identity.caveat, (
            f"{tool} returns no key, so it must say why — an absent key with no reason reads as "
            "'not computed yet', which is the one thing it does not mean"
        )
        return
    assert identity.calc_key == result.calc_key, (
        f"{tool}: the key derived up front is not the one the result carries; every lookup would "
        "miss forever and every calculation would be paid for twice"
    )


async def test_the_primitives_key_up_front_too() -> None:
    """The structure-in primitives key up front too, which is where it pays.

    Chemclaw3's durable jobs compose them, so each must be keyable before the call. One chain, as a
    caller uses them: embed, relax, then differentiate at the relaxed geometry.
    """
    water = await tools.embed_structure("O")
    ethanol = await tools.embed_structure("CCO")

    relax_id = await tools.calculation_key("relax_structure", {"structure": water.model_dump()})
    relaxed = await tools.relax_structure(water)
    assert relax_id.calc_key == relaxed.calc_key
    assert relax_id.structure_id == water.structure_id

    for tool, arguments, computed in (
        (
            "compute_properties_at",
            {"structure": relaxed.structure.model_dump()},
            await tools.compute_properties_at(relaxed.structure),
        ),
        (
            "compute_hessian",
            {"structure": relaxed.structure.model_dump()},
            await tools.compute_hessian(relaxed.structure),
        ),
        (
            "scan_point",
            {"structure": ethanol.model_dump(), "atoms": [0, 1, 2, 3], "value": 60.0},
            await tools.scan_point(ethanol, [0, 1, 2, 3], 60.0),
        ),
    ):
        identity = await tools.calculation_key(tool, arguments)
        assert identity.calc_key == computed.calc_key, tool
        assert identity.calc_version == computed.calc_version, tool


async def test_a_scan_point_keys_as_the_constrained_optimisation_it_is() -> None:
    """A scan point is an `xtb.opt` row, not a namespace of its own.

    Driving the coordinate is pure geometry; what runs is a constrained optimisation, and sharing
    the row stops a profile and an equivalent hand-written relaxation paying twice.
    """
    ethanol = await tools.embed_structure("CCO")
    point = await tools.calculation_key(
        "scan_point", {"structure": ethanol.model_dump(), "atoms": [0, 1, 2, 3], "value": 60.0}
    )
    assert point.key is not None and point.key.calc_type == "xtb.opt"

    # The frozen atoms have to be *in* the key, or every point of a profile would collide with the
    # free optimisation of the same driven geometry.
    free = await tools.calculation_key("relax_structure", {"structure": ethanol.model_dump()})
    assert free.key is not None and free.key.calc_type == "xtb.opt"
    assert free.key.params_hash != point.key.params_hash


async def test_a_crest_search_is_keyed_or_refused_exactly_where_the_search_is() -> None:
    """The probe and the search agree about whether this deployment can answer.

    `CrestSpec.calc_version()` answers `crest-absent` rather than raising, so both paths must refuse
    together where the binary is missing. Where present, the key names the build that would run.
    """
    water = await tools.embed_structure("O")
    for tool in ("search_conformer_ensemble", "search_binding_modes"):
        if crest_cli.is_available():
            keyed = await tools.calculation_key(tool, {"structure": water.model_dump()})
            assert keyed is not None
            assert f"crest-{crest_cli.binary_version()}" in keyed.key.calc_version
            continue
        with pytest.raises(ValueError, match="crest"):
            await tools.calculation_key(tool, {"structure": water.model_dump()})


@pytest.mark.parametrize(
    ("tool", "arguments", "compute"), CASES, ids=[f"{c[0]}-{c[1]['smiles']}" for c in CASES]
)
async def test_the_four_parts_reconstruct_the_flat_key(
    tool: str, arguments: dict[str, Any], compute: Any
) -> None:
    """`key` is what `store.get` takes; `calc_key` is the same identity flattened.

    Returned as an object because `calc_version` legitimately contains `@` and `:`, so parsing the
    flat form is unsafe.
    """
    identity = await tools.calculation_key(tool, arguments)
    if identity.key is None:
        assert identity.calc_key is None
        return
    assert isinstance(identity.key, CalculationKey)
    assert identity.key.as_str() == identity.calc_key
    assert identity.key.calc_version == identity.calc_version


async def test_deriving_a_key_runs_no_scf() -> None:
    """Deriving a key runs no SCF: every SCF path is made to raise.

    Replacing `xtb_engine.Calculator` blocks every route to a single point, and every identity still
    comes back. A key derivation that relaxed geometry first would turn a cheap probe into a
    minutes-long call.
    """

    from chemclaw_mcp_calc.engine.structure import structure_from_smiles

    # Embedded *before* the SCF is broken: building a geometry is RDKit's job, not tblite's, and
    # this test is about what the derivation does with one rather than about how it was made.
    geometry = structure_from_smiles("CC(=O)O", optimize=True).model_dump()

    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("deriving a key must not run an SCF")

    original = xtb_engine.Calculator
    # Substituting a function for the class is the assertion: touching the SCF would raise. mypy
    # reports it under both `misc` and `assignment`.
    xtb_engine.Calculator = _explode  # type: ignore[assignment, misc]
    try:
        for tool, (accepts, _) in sorted(COMPUTE_TOOLS.items()):
            if tool in NEEDS_A_BINARY and not xtb_cli.is_available():
                continue  # refuses on the missing program before any of this is reached
            identity = calculation_identity(tool, sweep_arguments(tool, accepts, geometry))
            assert identity.calc_version
    finally:
        xtb_engine.Calculator = original  # type: ignore[misc]


async def test_only_the_named_tool_lacks_a_derivable_key() -> None:
    """The set is closed. A second tool losing its key would be a real regression, quietly.

    Checked over the whole surface rather than over the cases above, so it holds for arguments no
    case happens to use.
    """
    from chemclaw_mcp_calc.engine.structure import structure_from_smiles

    geometry = structure_from_smiles("CC(=O)O", optimize=True).model_dump()
    for tool, (accepts, _) in sorted(COMPUTE_TOOLS.items()):
        if tool in NEEDS_A_BINARY and not xtb_cli.is_available():
            continue  # keyed like the rest; refuses without the program, checked separately
        identity = calculation_identity(tool, sweep_arguments(tool, accepts, geometry))
        assert (identity.calc_key is None) == (tool in WITHOUT_A_DERIVABLE_KEY), tool


def test_the_tools_that_need_a_binary_refuse_rather_than_key() -> None:
    """Tools needing a missing binary refuse rather than key, and every other tool answers.

    A key naming an absent program is well-formed and addresses a row nothing will write. The second
    half stops `NEEDS_A_BINARY` from being grown to silence a failure.
    """
    from chemclaw_mcp_calc.engine.structure import structure_from_smiles

    geometry = structure_from_smiles("CC(=O)O", optimize=True).model_dump()
    for tool, (accepts, _) in sorted(COMPUTE_TOOLS.items()):
        arguments = sweep_arguments(tool, accepts, geometry)
        if tool in NEEDS_A_BINARY and not xtb_cli.is_available():
            with pytest.raises(ValueError) as refused:
                calculation_identity(tool, arguments)
            assert "installed" in str(refused.value) or "binary" in str(refused.value), tool
            continue
        identity = calculation_identity(tool, arguments)
        assert "absent" not in identity.calc_version, (
            f"{tool} derived {identity.calc_version!r}, which names a program this image does not "
            "carry; that is a Chemclaw3 ledger key addressing a row nothing will write"
        )


async def test_the_one_tool_without_a_key_says_why() -> None:
    """An absent key must never read as "not computed yet". The one case carries its reason.

    And the reason names the alternative: logD's cost is a pKa, whose key *is* available, so a
    caller that wants to avoid paying twice knows exactly what to look up.
    """
    logd = await tools.calculation_key("predict_logd", {"smiles": "CC(=O)O"})
    assert logd.key is None and logd.calc_key is None
    assert logd.caveat is not None and "predict_pka" in logd.caveat
    # The version is still exact, and still worth returning: it is what a calibration ledger matches
    # on, and a ledger is keyed per prediction rather than per cache entry.
    assert logd.calc_version.startswith("logd/")


async def test_an_argument_the_tool_does_not_take_is_refused_not_ignored() -> None:
    """An argument the tool does not take is refused, not ignored.

    A misspelled `solvent` would otherwise key the gas phase and hand back a valid but wrong row.
    """
    with pytest.raises(ValueError, match="does not take 'solvant'"):
        await tools.calculation_key(
            "compute_electronic_properties", {"smiles": "CCO", "solvant": "water"}
        )

    # And the argument that *is* accepted really does move the key, which is what makes the above
    # more than a spelling check.
    gas = await tools.calculation_key("compute_electronic_properties", {"smiles": "CCO"})
    solvated = await tools.calculation_key(
        "compute_electronic_properties", {"smiles": "CCO", "solvent": "water"}
    )
    assert gas.calc_key != solvated.calc_key


async def test_an_unknown_tool_name_is_refused_and_lists_the_real_ones() -> None:
    """A typo'd tool name must not fall through to a default, and the message is the fix."""
    with pytest.raises(ValueError, match="is not a compute tool"):
        await tools.calculation_key("compute_energy", {"smiles": "CCO"})
    with pytest.raises(ValueError, match="requires a 'smiles'"):
        await tools.calculation_key("predict_pka", {})


async def test_the_derivation_table_matches_the_served_surface() -> None:
    """Every compute tool has a derivation, and every derivation names a served tool.

    Both directions. A tenth compute tool with no derivation would be one a caller cannot cache; a
    derivation for a tool that no longer exists would be a key nothing ever writes.
    """
    served = {tool.name for tool in await tools.server.list_tools()}
    assert set(COMPUTE_TOOLS) | HELPERS == served


async def test_each_derivation_accepts_exactly_its_tool_s_arguments() -> None:
    """The `accepts` sets match the served tools' own input schemas.

    A missing parameter makes valid calls refused (loud); a stale one is silent. This catches both.
    """
    schemas = {tool.name: tool.inputSchema for tool in await tools.server.list_tools()}
    for name, (accepts, _) in COMPUTE_TOOLS.items():
        declared = set(schemas[name].get("properties", {}))
        assert accepts == declared, f"{name}: derivation accepts {accepts}, tool takes {declared}"


async def test_two_spellings_of_one_molecule_derive_one_key() -> None:
    """Two spellings of one molecule derive one key on this path too.

    This tool decides which row is looked up, so a lenient probe would miss a row the compute path
    then overwrites under the canonical key.
    """
    for tool in ("compute_xtb_energy", "predict_pka", "predict_solubility"):
        assert (await tools.calculation_key(tool, {"smiles": "CCO"})).calc_key == (
            await tools.calculation_key(tool, {"smiles": "OCC"})
        ).calc_key


async def test_a_bad_input_is_refused_here_exactly_as_the_compute_tool_refuses_it() -> None:
    """A bad input is refused here exactly as the compute tool refuses it.

    Both refusals come from the same code, via the engine's own `*_inputs` pairing.
    """
    with pytest.raises(ValueError, match="invalid SMILES"):
        await tools.calculation_key("compute_xtb_energy", {"smiles": "CCO junk"})
    with pytest.raises(ValueError, match="tetrahydrofuran"):
        await tools.calculation_key(
            "optimize_geometry", {"smiles": "CCO", "solvent": "2-methyltetrahydrofuran"}
        )


def test_no_tool_keys_a_program_this_image_lacks_under_an_explicit_engine_setting() -> None:
    """No tool keys a program this image lacks, under an explicit `CHEMCLAW_XTB_ENGINE=xtb`.

    The ambient sweep runs with `auto`, which falls back to `tblite`; an explicit preference skips
    the availability check. Written as the invariant over the whole table: every tool either refuses
    in words or answers a version naming only programs that are here.
    """
    if xtb_cli.is_available():
        pytest.skip("this image carries the xtb binary, so no version can name it as absent")

    from chemclaw_mcp_calc.engine import config
    from chemclaw_mcp_calc.engine.structure import structure_from_smiles

    geometry = structure_from_smiles("CC(=O)O", optimize=True).model_dump()
    original = config.settings.xtb_engine
    config.settings.xtb_engine = "xtb"
    try:
        for tool, (accepts, _) in sorted(COMPUTE_TOOLS.items()):
            arguments = sweep_arguments(tool, accepts, geometry)
            try:
                identity = calculation_identity(tool, arguments)
            except ValueError as refused:
                assert "binary" in str(refused), (
                    f"{tool} refused without saying a binary is what is missing: {refused}"
                )
                continue
            assert xtb_cli.ABSENT_XTB_VERSION not in (identity.calc_version or ""), (
                f"{tool} derived {identity.calc_version!r} under CHEMCLAW_XTB_ENGINE=xtb on an "
                "image with no binary: a well-formed Chemclaw3 cache and calibration-ledger key "
                "naming a program that never ran, addressing a row nothing will ever write"
            )
    finally:
        config.settings.xtb_engine = original


def test_a_missing_binary_reaches_the_model_as_words_rather_than_an_error_id() -> None:
    """A missing binary reaches the model as words rather than an error id.

    `CliError` is a `RuntimeError` carrying internal stderr, so the sanitiser hides it; that is
    right for a failed run but not for "this image has no xtb", a configuration fault the model can
    act on.
    """
    if xtb_cli.is_available():
        pytest.skip("this image carries the xtb binary, so the refusal path is unreachable here")

    with pytest.raises(ValueError) as refused:
        xtb_cli.require_binary_path()
    assert not isinstance(refused.value, xtb_cli.CliError), (
        "a missing binary is raised as a CliError, which `connector_app` replaces with an opaque "
        "error id — the model is told nothing it can act on"
    )
    message = str(refused.value)
    assert "not installed in this deployment" in message, message
    # And it says what still works, because "nothing works" and "the binary-backed half does not"
    # are different answers and only one of them is true.
    assert "compute_xtb_energy" in message, (
        f"the refusal does not name a tool that still answers on this pod: {message}"
    )
