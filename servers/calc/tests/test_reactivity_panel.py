"""The descriptor panel Tier 1 reads out of SCFs that already ran, and Tier 2 out of the binary.

Two claims carry this feature and both are tested here rather than asserted in prose:

- **The global and local descriptors are free.** They come from the three single points
  `compute_fukui` already runs; the count is pinned, so a future edit that adds a fourth turns red.
- **The binary-only panel refuses rather than approximates.** With no `xtb`, the answer is a named
  refusal, never a payload of nulls a caller cannot distinguish from a chemical result.

The arithmetic identities are written out independently of the implementation — `f_zero` really is
the mean, `dual` really is the difference — so a sign flip anywhere is caught by a relation rather
than by a pinned number that would be updated alongside the bug.
"""

from __future__ import annotations

import math

import pytest
from chemclaw_mcp_calc.engine import xtb_atomic, xtb_cli, xtb_props
from chemclaw_mcp_calc.engine.structure import structure_from_smiles
from chemclaw_mcp_calc.engine.xtb_spec import XtbSpec

_BINARY = pytest.mark.skipif(
    not xtb_cli.is_available(),
    reason="the xtb binary is not installed; this deployment gets the refusal path instead",
)


@pytest.fixture(scope="module")
def phenol() -> xtb_props.SiteReactivityResult:
    """One Fukui run, shared: three SCFs is the cost, and paying it per test is the waste."""
    return xtb_props.compute_fukui(*xtb_props.fukui_inputs("Oc1ccccc1"), "electrophilic")


def test_the_panel_costs_no_extra_single_point(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole Tier 1 claim: three SCFs before the descriptors existed, three after.

    Pinned as a count rather than described in a docstring, because "free" is the property that
    justifies computing these at all — and the cheap way to lose it is a fourth call added by
    someone who did not know the energies were already in hand.
    """
    calls = 0
    original = xtb_props.run_singlepoint

    def counting(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(xtb_props, "run_singlepoint", counting)
    result = xtb_props.compute_fukui(*xtb_props.fukui_inputs("CO"), "nucleophilic")
    assert calls == 3
    assert result.descriptors.hardness_ev > 0


def test_the_local_indices_are_the_relations_they_claim_to_be(
    phenol: xtb_props.SiteReactivityResult,
) -> None:
    """Every derived per-site number, checked against its own definition."""
    softness = phenol.descriptors.softness_per_ev
    omega = phenol.descriptors.electrophilicity_ev
    for site in phenol.sites:
        assert site.f_zero == pytest.approx((site.f_minus + site.f_plus) / 2, abs=1e-4)
        assert site.dual == pytest.approx(site.f_plus - site.f_minus, abs=1e-4)
        assert site.local_softness_minus == pytest.approx(softness * site.f_minus, abs=1e-5)
        assert site.local_softness_plus == pytest.approx(softness * site.f_plus, abs=1e-5)
        assert site.local_electrophilicity_ev == pytest.approx(omega * site.f_plus, abs=1e-3)


def test_the_global_panel_is_internally_consistent(
    phenol: xtb_props.SiteReactivityResult,
) -> None:
    """mu, eta, S and omega are one derivation, so they must agree with each other."""
    panel = phenol.descriptors
    assert panel.hardness_ev == pytest.approx(
        panel.ionization_potential_ev - panel.electron_affinity_ev, abs=1e-3
    )
    assert panel.chemical_potential_ev == pytest.approx(
        -(panel.ionization_potential_ev + panel.electron_affinity_ev) / 2, abs=1e-3
    )
    assert panel.softness_per_ev == pytest.approx(1 / panel.hardness_ev, abs=1e-5)
    assert panel.electrophilicity_ev == pytest.approx(
        panel.chemical_potential_ev**2 / (2 * panel.hardness_ev), abs=1e-3
    )
    assert panel.chemical_potential_ev < 0, "electrons are bound"
    assert panel.ionization_potential_ev > panel.electron_affinity_ev


def test_the_physics_still_says_para(phenol: xtb_props.SiteReactivityResult) -> None:
    """Reading more of the result must not change what the result was.

    Phenol's ring carbons, compared with each other: *para* above *ortho* above *meta*, the
    classical pattern for an activating substituent. This is the number the whole feature is about,
    so it is pinned against the descriptors being wired in.
    """
    by_index = {site.index: site for site in phenol.sites}
    para, ortho, meta = by_index[4], by_index[2], by_index[3]
    assert para.f_minus > ortho.f_minus > meta.f_minus


def test_a_non_positive_hardness_is_refused_rather_than_divided_by() -> None:
    """eta divides both S and omega; a zero would return an infinity dressed as a descriptor."""
    with pytest.raises(ValueError, match="non-positive chemical hardness"):
        xtb_props._global_descriptors(neutral=-1.0, cation=-1.0, anion=-1.0)


def test_free_valence_is_absent_where_the_element_has_no_normal_valence() -> None:
    """A free valence subtracted from a valence nobody can state is worse than no free valence."""
    properties = xtb_props.compute_properties(*xtb_props.properties_inputs("CS(=O)(=O)C"))
    by_element = {atom.element: atom for atom in properties.atom_charges}
    assert by_element["S"].free_valence is None, "hypervalent sulfur has no single normal valence"
    assert by_element["C"].free_valence is not None
    for atom in properties.atom_charges:
        assert atom.wiberg_valence > 0


def test_carbon_uses_close_to_its_four_bonds() -> None:
    """An independent sanity check on the Wiberg sum: methane's carbon is saturated."""
    properties = xtb_props.compute_properties(*xtb_props.properties_inputs("C"))
    carbon = next(atom for atom in properties.atom_charges if atom.element == "C")
    assert carbon.wiberg_valence == pytest.approx(4.0, abs=0.15)
    assert carbon.free_valence == pytest.approx(0.0, abs=0.15)


# --- Tier 2: the binary-only panel ------------------------------------------------------------


def test_the_two_binary_panels_are_keyed_apart() -> None:
    """One key standing for both would serve a surface request the panel-only row it found.

    The defect this pins: `surface` was a *flag* on the atomic panel and deliberately kept out of
    its key, so a `surface=True` call hit the row an earlier `surface=False` call wrote and came
    back with no surface, having run nothing.
    """
    atomic = xtb_atomic.atomic_inputs("CCO")
    surface = xtb_atomic.surface_inputs("CCO")
    assert atomic[0].cache_key(atomic[1]).as_str() != surface[0].cache_key(surface[1]).as_str()
    assert atomic[0].cache_key(atomic[1]).calc_type == "xtb.atomic"
    assert surface[0].cache_key(surface[1]).calc_type == "xtb.surface"


def test_the_binary_panel_refuses_by_name_when_the_binary_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal names the missing program and says what still works without it.

    A `ValueError`, which is the family `connector_app` lets reach the model verbatim — this message
    is the difference between "this deployment cannot answer that" and "this molecule has no
    answer", and only the first is true.
    """
    monkeypatch.setattr(xtb_cli, "is_available", lambda: False)
    # The *key* still derives — deriving an identity is not running a calculation, and
    # `calculation_key` exists so a caller can ask before committing. Only computing refuses.
    spec, structure = xtb_atomic.atomic_inputs("CCO")
    assert spec.cache_key(structure).as_str().startswith("xtb.atomic@")

    with pytest.raises(ValueError, match="'xtb' binary, which is not installed") as raised:
        xtb_atomic.compute_atomic_descriptors(spec, structure)
    message = str(raised.value)
    assert "no in-process fallback" in message
    assert "predict_site_reactivity" in message, "say what still works"


def test_an_open_shell_structure_is_refused_rather_than_silently_run_on_tblite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`for_structure` moves an open shell to tblite, which cannot produce this panel at all."""
    monkeypatch.setattr(xtb_cli, "is_available", lambda: True)
    radical = structure_from_smiles("C[CH2]", multiplicity=2)
    with pytest.raises(ValueError, match="resolved to 'tblite'"):
        xtb_atomic.compute_atomic_descriptors(XtbSpec(task="atomic", engine="xtb"), radical)
    with pytest.raises(ValueError, match="resolved to 'tblite'"):
        xtb_atomic.compute_surface_potential(XtbSpec(task="surface", engine="xtb"), radical)


def test_the_atomic_table_parser_reads_a_captured_run() -> None:
    """The property table as xtb 6.6.1 really prints it, transcribed from a captured run."""
    log = """
     #   Z          covCN         q      C6AA      a(0)
     1   8 O        1.659    -0.395    20.748     6.149
     2   6 C        2.844     0.147    24.802     8.162
     3   1 H        0.804     0.298     0.727     1.339

 Mol. C6AA /au*bohr^6  :       1361.604384
"""
    rows = xtb_cli._read_atomic_table(log, 3)
    assert [row.element for row in rows] == ["O", "C", "H"]
    assert [row.index for row in rows] == [0, 1, 2], "reported 1-based, returned 0-based"
    assert rows[0].polarisability_au == 6.149
    assert rows[0].c6_au == 20.748
    assert rows[2].coordination_number == 0.804


def test_a_short_or_missing_table_yields_nothing_rather_than_a_partial_panel() -> None:
    """A caller that needs these says so in its own words; a half-read table is not a panel."""
    assert xtb_cli._read_atomic_table("no table here", 3) == []
    partial = (
        "     #   Z          covCN         q      C6AA      a(0)\n     1   8 O 1.6 -0.3 20.7 6.1\n"
    )
    assert xtb_cli._read_atomic_table(partial, 3) == []


def test_the_surface_grid_parser_reduces_to_extrema() -> None:
    """`xtb_esp.dat` is four columns; only the potential is read, and it converts to kcal/mol."""
    grid = (
        "  0.7686481500E+01 -0.1823585700E+00  0.2059801500E-01   -0.02594203\n"
        "  0.4708441600E+01  0.2795681400E+01  0.2059801500E-01    0.04401631\n"
        "not a data line\n"
    )
    surface = xtb_cli._read_surface(grid)
    assert surface.grid_points == 2
    assert surface.minimum_kcal_per_mol == pytest.approx(-0.02594203 * 627.5094740631, abs=1e-2)
    assert surface.maximum_kcal_per_mol == pytest.approx(0.04401631 * 627.5094740631, abs=1e-2)
    with pytest.raises(xtb_cli.CliError, match="no potential values"):
        xtb_cli._read_surface("nothing parseable\n")


@_BINARY
def test_the_binary_agrees_with_the_library_about_partial_charges() -> None:
    """Two backends, one number: the strongest available check that the table is read correctly.

    Nothing forces these to agree — one is a Fortran binary's stdout table, the other a Python
    library's array — so agreement to three decimals says the parser lines the rows up with the
    right atoms. A transposed column or an off-by-one would show here and nowhere else.
    """
    smiles = "Oc1ccccc1"
    binary = xtb_atomic.compute_atomic_descriptors(*xtb_atomic.atomic_inputs(smiles))
    library = xtb_props.compute_properties(*xtb_props.properties_inputs(smiles))
    assert len(binary.atoms) == len(library.atom_charges)
    for from_binary, from_library in zip(binary.atoms, library.atom_charges, strict=True):
        assert from_binary.element == from_library.element
        assert from_binary.charge == pytest.approx(from_library.charge, abs=2e-3)


@_BINARY
def test_polarisability_ranks_the_halogens_the_way_the_periodic_table_does() -> None:
    """Iodine is the most polarisable atom of iodobenzene — the sigma-hole case, and a fact no
    partial charge carries."""
    result = xtb_atomic.compute_atomic_descriptors(*xtb_atomic.atomic_inputs("Ic1ccccc1"))
    most = max(result.atoms, key=lambda atom: atom.polarisability_au)
    assert most.element == "I"
    carbons = [atom.polarisability_au for atom in result.atoms if atom.element == "C"]
    assert most.polarisability_au > max(carbons) * 2


@_BINARY
def test_the_calc_version_names_the_binary_that_produced_the_numbers() -> None:
    """A version naming a program that did not run survives an upgrade to the one that did."""
    result = xtb_atomic.compute_atomic_descriptors(*xtb_atomic.atomic_inputs("CCO"))
    assert "xtb-" in result.calc_version
    assert result.calc_version.startswith("GFN2-xTB+xtb+")
    assert result.calc_key is not None and result.calc_key.startswith("xtb.atomic@")


@_BINARY
def test_the_surface_has_both_a_positive_and_a_negative_extreme() -> None:
    """Phenol's OH hydrogen is the positive patch and its oxygen lone pair the negative one."""
    result = xtb_atomic.compute_surface_potential(*xtb_atomic.surface_inputs("Oc1ccccc1"))
    assert result.surface.maximum_kcal_per_mol > 0 > result.surface.minimum_kcal_per_mol
    assert result.surface.grid_points > 0


@_BINARY
def test_atomic_multipoles_are_present_and_finite() -> None:
    """The anisotropy a point charge cannot carry — the reason the binary is worth a subprocess."""
    result = xtb_atomic.compute_atomic_descriptors(*xtb_atomic.atomic_inputs("Oc1ccccc1"))
    for atom in result.atoms:
        assert atom.dipole_norm_au is not None and math.isfinite(atom.dipole_norm_au)
        assert atom.quadrupole_norm_au is not None and math.isfinite(atom.quadrupole_norm_au)
    oxygen = next(atom for atom in result.atoms if atom.element == "O")
    assert oxygen.dipole_norm_au > 0, "a lone pair is not isotropic"


# --- the backend a version names must be the backend that ran ---------------------------------


def test_the_in_process_calculators_name_tblite_even_where_a_binary_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `calc_version` may name only programs that actually ran, and these three run tblite.

    **This is a regression test for a defect the shipped image cannot see.** `xtb_engine` defaults
    to `"auto"`, so `resolve_backend()` answers `"xtb"` wherever the binary is installed — and
    `compute_xtb_energy`, `compute_electronic_properties` and `predict_site_reactivity` then stamped
    `+xtb+xtb-6.6.1` onto results computed entirely by tblite, because none of the three has a
    binary code path at all. `xtb_opt` and `xtb_hessian` do dispatch and keep resolving.

    Forced True rather than skipped when absent: the assertion is about what the code *would* do on
    a deployment with a binary, and a test that only runs where one exists would never have caught
    this.
    """
    from chemclaw_mcp_calc.engine import xtb
    from chemclaw_mcp_calc.engine.xtb import XtbInput

    monkeypatch.setattr(xtb_cli, "is_available", lambda: True)
    monkeypatch.setattr(xtb_cli, "binary_version", lambda: "6.6.1")

    for label, (spec, structure) in (
        ("compute_xtb_energy", xtb.sp_inputs(XtbInput(smiles="CCO"))),
        ("compute_electronic_properties", xtb_props.properties_inputs("CCO")),
        ("predict_site_reactivity", xtb_props.fukui_inputs("CCO")),
    ):
        resolved = spec.for_structure(structure)
        assert resolved.engine == "tblite", label
        assert "+xtb+" not in resolved.calc_version(), label
