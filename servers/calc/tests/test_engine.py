"""The physics and the domain limits, at the layer where they live — no transport imported.

Ported from Chemclaw3's own suite for these calculators and narrowed to what this server serves. The
selection principle: every test here pins a behaviour whose loss would be **silent**. A wrong energy
raises nothing, a pKa for an aliphatic amine looks exactly like a pKa for a pyridine, and an ESOL
prediction for a salt is a number with an error bar drawn from a distribution it was never in.

The molecules are small on purpose — water, ethanol, acetic acid, pyridine. The properties being
asserted are structural rather than about size, and the suite runs on every pull request.
"""

from __future__ import annotations

import math
import re

import pytest
from chemclaw_mcp_calc.engine import xtb_cli, xtb_props
from chemclaw_mcp_calc.engine.chem import InvalidSmilesError
from chemclaw_mcp_calc.engine.descriptors import DescriptorInput, compute_descriptor_profile
from chemclaw_mcp_calc.engine.logd import LogdInput, predict_logd
from chemclaw_mcp_calc.engine.pka import PkaInput, ionisable_sites, predict_pka
from chemclaw_mcp_calc.engine.solubility import SolubilityInput, predict_solubility
from chemclaw_mcp_calc.engine.structure import Structure, structure_from_smiles
from chemclaw_mcp_calc.engine.uncertainty import CalculationDomainError
from chemclaw_mcp_calc.engine.xtb import XtbInput, run_xtb
from chemclaw_mcp_calc.engine.xtb_opt import OptSpec, optimize_structure
from chemclaw_mcp_calc.engine.xtb_spec import XtbSpec, resolve_backend
from chemclaw_mcp_calc.engine.xtb_thermo import ThermoSpec, compute_thermochemistry

# ----------------------------------------------------------------------------------------------
# Structure: the validation every xTB task inherits


def test_a_declared_charge_that_contradicts_the_smiles_is_refused() -> None:
    """tblite converges a wrong-charge system silently, to an energy hundreds of kcal/mol off.

    Which is why this is a refusal rather than a warning: the number that comes back from the
    unguarded path is a real, converged, entirely wrong energy.
    """
    with pytest.raises(ValueError, match="does not match the formal charge"):
        structure_from_smiles("CC(=O)O", charge=-1)


def test_an_odd_electron_count_cannot_be_a_closed_shell_singlet() -> None:
    """The other half of the guard, and the reason `multiplicity` is a field rather than a flag."""
    with pytest.raises(ValueError, match="cannot be a closed-shell singlet"):
        Structure(elements=[7, 1, 1], positions=[[0, 0, 0], [1, 0, 0], [0, 1, 0]])


def test_a_radical_smiles_carries_its_own_multiplicity() -> None:
    """`multiplicity=None` reads the SMILES' explicit radical electrons instead of guessing."""
    methyl = structure_from_smiles("[CH3]", multiplicity=None)
    assert methyl.multiplicity == 2 and methyl.uhf == 1


def test_a_string_rdkit_would_truncate_never_reaches_a_calculator() -> None:
    """`"CCO junk"` is ethanol to RDKit. A converged energy for another molecule is the failure."""
    with pytest.raises(InvalidSmilesError):
        structure_from_smiles("CCO junk")


# ----------------------------------------------------------------------------------------------
# Backend resolution and solvents


def test_the_backend_never_resolves_to_the_word_auto() -> None:
    """A version string containing "auto" would mean different things on two deployments.

    This also documents what the shipped image actually does: with no `xtb` binary installed,
    `resolve_backend()` answers `tblite`, and every `calc_version` says `+tblite+`.
    """
    assert resolve_backend() in ("tblite", "xtb")
    assert resolve_backend() == ("xtb" if xtb_cli.is_available() else "tblite")


def test_an_unparameterised_solvent_is_refused_with_the_supported_list() -> None:
    """2-MeTHF is among the commonest process solvents and GFN2-xTB has no parameters for it.

    Refused at spec construction so it cannot reach either backend — tblite would answer with
    "String value for epsilon was not found among database of solvents", and the `xtb` binary would
    fail minutes later inside a subprocess.
    """
    with pytest.raises(ValueError) as raised:
        XtbSpec(task="sp", solvent="2-methyltetrahydrofuran")
    message = str(raised.value)
    assert "tetrahydrofuran" in message, "the closest supported spelling is the actionable half"
    assert "thf" in message, "the shortlist of common supported solvents must be quoted"


def test_gas_phase_is_not_a_solvent() -> None:
    """`None` passes untouched — the distinction a validator can easily lose."""
    assert XtbSpec(task="sp", solvent=None).solvent is None
    assert XtbSpec(task="sp", solvent="water").solvent == "water"


# ----------------------------------------------------------------------------------------------
# Single-point energies


def test_relaxing_before_the_single_point_gets_the_isomer_ordering_right() -> None:
    """The measured reason `_sp_structure` sets `optimize=True`.

    On a raw ETKDG embedding the residual strain is larger than the energy difference being asked
    about, and ethanol vs. dimethyl ether comes out with the *wrong sign*. Ethanol is the more
    stable of the two, and a single-point energy is only ever useful relatively.
    """
    ethanol = run_xtb(XtbInput(smiles="CCO")).total_energy_hartree
    dimethyl_ether = run_xtb(XtbInput(smiles="COC")).total_energy_hartree
    assert ethanol < dimethyl_ether


def test_two_spellings_of_one_molecule_give_one_energy() -> None:
    """Canonicalization happens before embedding, so the geometry cannot depend on the spelling."""
    assert run_xtb(XtbInput(smiles="CCO")).total_energy_hartree == pytest.approx(
        run_xtb(XtbInput(smiles="OCC")).total_energy_hartree
    )


# ----------------------------------------------------------------------------------------------
# Electronic properties and Fukui indices


def test_the_properties_come_out_of_one_scf_with_a_sane_frontier_gap() -> None:
    """HOMO below LUMO, a positive gap, one charge per atom, and water's dipole in range."""
    structure = xtb_props.property_structure("O")
    result = xtb_props.compute_properties(XtbSpec(task="properties"), structure)
    assert result.lumo_ev is not None and result.gap_ev is not None
    assert result.homo_ev < result.lumo_ev and result.gap_ev > 0
    assert len(result.atom_charges) == len(structure.elements)
    # Experimental gas-phase water is 1.85 D; GFN2 is close and the point is that the unit
    # conversion is right, not that the Hamiltonian is exact.
    assert 1.4 < result.dipole_debye < 2.6


def test_fukui_ranks_the_ring_carbons_of_toluene_para_over_meta() -> None:
    """The textbook ordering, and the measurement that made `property_structure` relax first.

    On an unrelaxed embedding the residual distortion breaks the symmetry of chemically equivalent
    ring positions and *ortho* and *meta* overlap. Read on the ring carbons only, because a
    heteroatom or the methyl would otherwise dominate — which is precisely the caveat the tool's
    docstring gives the model.
    """
    structure = xtb_props.property_structure("Cc1ccccc1")
    result = xtb_props.compute_fukui(XtbSpec(task="fukui"), structure, "electrophilic")
    by_index = {site.index: site for site in result.sites}
    # Canonical toluene: atom 0 is the methyl carbon, 1 the ipso, then around the ring.
    ortho = (by_index[2].f_minus + by_index[6].f_minus) / 2
    meta = (by_index[3].f_minus + by_index[5].f_minus) / 2
    para = by_index[4].f_minus
    assert para > ortho > meta
    # Chemically equivalent positions must agree, which is what the relaxation restores.
    assert by_index[2].f_minus == pytest.approx(by_index[6].f_minus, abs=1e-3)


def test_a_second_fukui_mode_is_a_re_sort_rather_than_three_more_scfs() -> None:
    """`f_zero` is the mean of the other two by construction: every ranking is already present."""
    structure = xtb_props.property_structure("CCO")
    electrophilic = xtb_props.compute_fukui(XtbSpec(task="fukui"), structure, "electrophilic")
    nucleophilic = xtb_props.ranked_for(electrophilic, "nucleophilic")
    assert nucleophilic.ranked_by == "f_plus"
    assert nucleophilic.sites[0].f_plus == max(site.f_plus for site in electrophilic.sites)
    for site in electrophilic.sites:
        assert site.f_zero == pytest.approx((site.f_minus + site.f_plus) / 2, abs=1e-4)


def test_fukui_refuses_an_open_shell_parent() -> None:
    """Both ions of a closed-shell molecule are unambiguously doublets; an open shell's are not."""
    radical = structure_from_smiles("[CH3]", multiplicity=None)
    with pytest.raises(ValueError, match="closed-shell"):
        xtb_props.compute_fukui(XtbSpec(task="fukui"), radical, "radical")


# ----------------------------------------------------------------------------------------------
# Geometry optimization


def test_a_converged_structure_is_a_fixed_point() -> None:
    """Re-optimizing a minimum must return it unchanged, coordinates included.

    Not cosmetic: `structure_id` is a hash of the coordinates, so an optimizer that always moves
    something mints a new id on every pass — which forks the key of every task built on that
    geometry, and quietly turns "compute once" into "compute every time".
    """
    relaxed = optimize_structure(OptSpec(), structure_from_smiles("O", optimize=True)).structure
    again = optimize_structure(OptSpec(), relaxed)
    assert again.structure.positions == relaxed.positions
    assert again.steps == 0


def test_a_strained_start_relaxes_and_reports_what_it_was_worth() -> None:
    """`relaxation_kcal` is what tells a chemist the unrelaxed numbers described a strain."""
    stretched = Structure(
        elements=[8, 1, 1],
        positions=[[0.0, 0.0, 0.0], [1.6, 0.0, 0.0], [-0.3, 0.9, 0.0]],
    )
    result = optimize_structure(OptSpec(), stretched)
    assert result.relaxation_kcal > 1.0
    assert result.max_gradient is not None
    assert result.max_gradient <= OptSpec().gradient_tolerance
    assert result.structure.origin == result.calc_key


def test_frozen_atoms_are_held_exactly() -> None:
    """The constrained path is an exact minimization over the free subspace, not a soft penalty."""
    start = structure_from_smiles("O", optimize=True)
    result = optimize_structure(OptSpec(frozen_atoms=(0,), gradient_tolerance=1e-2), start)
    assert result.structure.positions[0] == start.positions[0]


def test_freezing_everything_is_an_error_rather_than_a_no_op() -> None:
    """Silently returning the input would look exactly like a converged optimization."""
    with pytest.raises(ValueError, match="nothing to optimize"):
        optimize_structure(
            OptSpec(frozen_atoms=(0, 1, 2)), structure_from_smiles("O", optimize=True)
        )


# ----------------------------------------------------------------------------------------------
# Thermochemistry


def test_water_comes_out_with_three_real_modes_and_its_measured_entropy() -> None:
    """The whole RRHO stack in one assertion, against a number that exists outside this repository.

    Water's standard molar entropy in the gas phase is 188.8 J/(mol K) = 45.1 cal/(mol K), at
    sigma = 2. Getting the symmetry number wrong shifts it by R ln 2 = 1.4 cal/(mol K), which is why
    the second half of this test matters as much as the first: it is the one error in this module
    that a plausible-looking result hides completely.
    """
    relaxed = optimize_structure(OptSpec(), structure_from_smiles("O", optimize=True)).structure
    result = compute_thermochemistry(ThermoSpec(symmetry_number=2), relaxed)
    assert result.is_minimum and result.mode_count == 3
    assert all(mode.wavenumber_cm > 0 for mode in result.modes)
    assert result.entropy_cal_per_mol_k == pytest.approx(45.1, abs=0.5)

    unsymmetric = compute_thermochemistry(ThermoSpec(symmetry_number=1), relaxed)
    gas_constant_ln2 = 8.314462618 * math.log(2) / 4.184
    assert unsymmetric.entropy_cal_per_mol_k - result.entropy_cal_per_mol_k == pytest.approx(
        gas_constant_ln2, abs=0.01
    )


def test_a_linear_molecule_keeps_3n_minus_5_modes() -> None:
    """CO2 loses a real mode to a naive projection, because an "optimized linear" molecule is bent.

    Its null rotation then has a small but perfectly ordinary singular value and survives a
    singular-value cut. The fix is to build the rotations about the principal axes and drop the one
    with a vanishing moment — asserted here on the mode *count*, which is the observable.
    """
    relaxed = optimize_structure(OptSpec(), structure_from_smiles("O=C=O", optimize=True)).structure
    result = compute_thermochemistry(ThermoSpec(symmetry_number=2), relaxed)
    assert result.mode_count == 3 * len(relaxed.elements) - 5


def test_the_free_energy_is_the_electronic_energy_plus_the_correction() -> None:
    """Internal consistency of the reported fields, in the two units they are reported in."""
    relaxed = optimize_structure(OptSpec(), structure_from_smiles("O", optimize=True)).structure
    result = compute_thermochemistry(ThermoSpec(symmetry_number=2), relaxed)
    hartree_per_kcal = 1.0 / 627.5094740631
    assert result.gibbs_free_energy_hartree == pytest.approx(
        result.electronic_energy_hartree + result.gibbs_correction_kcal * hartree_per_kcal, abs=1e-9
    )
    assert result.enthalpy_hartree > result.gibbs_free_energy_hartree


def test_a_molecule_over_the_atom_limit_is_refused_and_says_where_to_go() -> None:
    """The refusal names Chemclaw3's durable job path, because this server has none of its own."""
    from chemclaw_mcp_calc.engine.config import settings
    from chemclaw_mcp_calc.engine.xtb_hessian import HessianSpec, compute_hessian

    # Two over the limit rather than one, because an odd number of hydrogens is an odd number of
    # electrons and `Structure` rejects that before the size check is ever reached — a nicely
    # illustrative ordering of the two guards, and one that made the first draft of this test fail
    # for the wrong reason.
    count = settings.xtb_hessian_max_atoms + 2
    big = Structure(
        elements=[1] * count,
        positions=[[float(i), 0.0, 0.0] for i in range(count)],
    )
    with pytest.raises(ValueError, match="durable QM job path"):
        compute_hessian(HessianSpec(), big)


# ----------------------------------------------------------------------------------------------
# pKa


def test_the_acid_branch_orders_the_textbook_acids_correctly() -> None:
    """Acetic acid is a stronger acid than phenol, which is stronger than ethanol.

    The calibration's absolute accuracy is ~1.6 pKa units, so ordering is what can honestly be
    asserted — and ordering is what the tool is actually for.
    """
    acetic = predict_pka(PkaInput(smiles="CC(=O)O")).pka
    phenol = predict_pka(PkaInput(smiles="Oc1ccccc1")).pka
    ethanol = predict_pka(PkaInput(smiles="CCO")).pka
    assert acetic < phenol < ethanol


def test_a_base_reports_its_conjugate_acid_and_says_so() -> None:
    """Pyridine's measured pKaH is 5.2, and `site` is what tells a caller which number this is."""
    result = predict_pka(PkaInput(smiles="c1ccncc1"))
    assert result.site == "base"
    assert result.pka == pytest.approx(5.2, abs=1.0)
    assert result.uncertainty == 1.0


def test_an_aliphatic_amine_is_refused_with_the_measurement_behind_the_refusal() -> None:
    """Spearman -0.17 over 13 references: no ranking ability at all, so a number would be a fiction.

    The message is the capability here — a chemist told "internal error" would try another
    substrate; one told the continuum solvent cannot represent the ammonium ion's hydrogen bonding
    knows to measure it instead. `CalculationDomainError` is a `ValueError`, which is what lets
    `connector_app` pass it through verbatim.
    """
    with pytest.raises(CalculationDomainError, match=re.escape("Spearman -0.17")):
        predict_pka(PkaInput(smiles="C1CCNCC1"))


def test_an_amide_nitrogen_is_not_a_basic_site() -> None:
    """Acetamide has no basic centre; free valence alone would have said it did.

    Its pKaH is ~ -0.5 and the proton goes on the *oxygen*, so the nitrogen this enumeration would
    otherwise offer is not the site even in the strongest acid.
    """
    assert ionisable_sites("CC(N)=O").basic == 0
    with pytest.raises(CalculationDomainError, match="nothing to protonate or deprotonate"):
        predict_pka(PkaInput(smiles="CC(C)(C)C(N)=O"))


def test_a_charged_input_is_outside_the_calibration_rather_than_computed() -> None:
    """The calibration was fitted on neutral acids through the acid(0)/anion(-1) path."""
    with pytest.raises(CalculationDomainError, match="net formal charge"):
        predict_pka(PkaInput(smiles="CC(=O)[O-]"))


def test_the_result_carries_the_canonical_smiles_it_computed_on() -> None:
    """Canonicalization moved inside `predict_pka` when `run_cached_pka` was dropped in the port.

    Atom order steers the seeded embedding, so a value computed on the caller's spelling would
    depend on which spelling arrived first — and would disagree with the key, which is built on the
    canonical form.
    """
    assert predict_pka(PkaInput(smiles="OC(C)=O")).smiles == "CC(=O)O"
    assert predict_pka(PkaInput(smiles="OC(C)=O")).pka == pytest.approx(
        predict_pka(PkaInput(smiles="CC(=O)O")).pka
    )


# ----------------------------------------------------------------------------------------------
# logD


def test_an_acid_below_its_pka_is_essentially_its_logp() -> None:
    """At pH well below the pKa the molecule is neutral, so the correction term vanishes."""
    result = predict_logd(LogdInput(smiles="CC(=O)O", ph=1.0))
    assert result.log_d == pytest.approx(result.clogp, abs=0.05)
    assert result.uncertainty == 1.6


def test_a_base_is_corrected_in_the_opposite_direction() -> None:
    """The entire content of the module is the sign of one exponent, and it is silent when wrong.

    Before the branch on `site` existed, pyridine took the acid form and came out two log units too
    lipophobic while looking entirely ordinary. Above its pKaH a base is *neutral*, so logD
    approaches logP from below rather than falling away from it.
    """
    low = predict_logd(LogdInput(smiles="c1ccncc1", ph=2.0))
    high = predict_logd(LogdInput(smiles="c1ccncc1", ph=10.0))
    assert low.log_d < high.log_d
    assert high.log_d == pytest.approx(high.clogp, abs=0.05)


def test_an_amphoteric_molecule_is_refused_at_every_ph() -> None:
    """Glycine returned -2.81 with no error, evading the refusal `predict_pka` raises for an amine.

    A carboxyl sends the molecule down the acid branch, so the base site is never evaluated and
    nothing bounds its ionisation.
    """
    with pytest.raises(CalculationDomainError, match="amphoteric"):
        predict_logd(LogdInput(smiles="NCC(=O)O", ph=7.4))


def test_a_polyprotic_acid_is_refused_where_the_second_site_is_not_a_spectator() -> None:
    """Succinic acid at pH 7.4 returned -1.48 +/- 1.6 against a true value near -5.

    And the same molecule *is* served far below its pKa, where the single term is exact to within
    the configured bound — which is what keeps a diol or a sugar working.
    """
    with pytest.raises(CalculationDomainError, match="unaccounted for"):
        predict_logd(LogdInput(smiles="OC(=O)CCC(O)=O", ph=7.4))
    assert predict_logd(LogdInput(smiles="OC(=O)CCC(O)=O", ph=1.0)).log_d is not None


# ----------------------------------------------------------------------------------------------
# Solubility and descriptors


def test_solubility_flags_a_salt_as_out_of_domain_rather_than_refusing() -> None:
    """ESOL returns a number for anything; `estimate.in_domain` is what says not to use it.

    The convention differs from logD's deliberately: ESOL on a salt gives a value of *unknown*
    validity, while a single-equilibrium logD on a diacid gives one known to be wrong by 2-5 log
    units — a number no caller should be handed at all.
    """
    salt = predict_solubility(SolubilityInput(smiles="CC(=O)[O-].[Na+]"))
    assert salt.estimate is not None
    assert salt.estimate.in_domain is False
    assert not salt.estimate.trustworthy
    assert any("counter-ion" in reason for reason in salt.estimate.domain_reasons)
    assert "OUT OF DOMAIN" in salt.estimate.render()

    ordinary = predict_solubility(SolubilityInput(smiles="CCO"))
    assert ordinary.estimate is not None and ordinary.estimate.in_domain is True
    assert ordinary.estimate.method == "reported"


def test_solubility_orders_a_polar_and_a_greasy_molecule_correctly() -> None:
    """Ethanol is far more soluble than naphthalene; ESOL's whole content is that ordering."""
    assert (
        predict_solubility(SolubilityInput(smiles="CCO")).log_s_mol_per_l
        > predict_solubility(SolubilityInput(smiles="c1ccc2ccccc2c1")).log_s_mol_per_l
    )


def test_the_descriptor_panel_flags_a_molecule_that_breaks_the_rule_of_five() -> None:
    """Both heuristics, on a molecule that fails one and passes the other's spirit.

    Ethanol violates nothing; a long-chain fatty alcohol breaks Veber's rotatable-bond count while
    staying inside Lipinski, which is the case that shows the two flags are independent.
    """
    ethanol = compute_descriptor_profile(DescriptorInput(smiles="CCO"))
    assert ethanol.lipinski_violations == 0 and ethanol.veber_pass
    assert 0.0 < ethanol.qed <= 1.0

    floppy = compute_descriptor_profile(DescriptorInput(smiles="CCCCCCCCCCCCCCCCO"))
    assert not floppy.veber_pass


def test_the_panel_is_computed_on_the_canonical_form() -> None:
    """Two spellings must give one panel as well as one key."""
    assert (
        compute_descriptor_profile(DescriptorInput(smiles="OCC")).smiles
        == compute_descriptor_profile(DescriptorInput(smiles="CCO")).smiles
    )
