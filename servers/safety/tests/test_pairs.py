"""The named-substance incompatibility pairs, each safe apart and dangerous together.

These live in `rules.yaml` as SMARTS pair rules and are pinned here with parsed molecules,
because a rule that never fires reports "no rule matched" for a hazard the table claims to
cover. Azide salts are the cautionary case: RDKit sanitizes the anion to one-coordinate
nitrogens, so a pattern written for organic azides never matches a salt.
"""

from __future__ import annotations

import pytest
from chemclaw_mcp_safety.engine.screen import screen_reaction


@pytest.mark.parametrize(
    ("label", "components", "expected_rule"),
    [
        ("azide salt in DCM", ["[Na+].[N-]=[N+]=[N-]", "ClCCl"], "azide-with-dichloromethane"),
        ("NaH in DMF", ["[Na+].[H-]", "CN(C)C=O"], "hydride-with-dipolar-aprotic"),
        ("NaH in DMSO", ["[Na+].[H-]", "CS(C)=O"], "hydride-with-dipolar-aprotic"),
        ("peroxide with ketone", ["OO", "CC(C)=O"], "peroxide-with-ketone"),
        (
            "LiAlH4 in DCM",
            ["[Li+].[AlH4-]", "ClCCl"],
            "complex-hydride-with-chlorinated-solvent",
        ),
    ],
)
def test_a_contributed_pair_actually_fires(
    label: str, components: list[str], expected_rule: str
) -> None:
    """Each pair is individually unremarkable and dangerous together — the reason pairs exist."""
    fired = {flag.rule_id for flag in screen_reaction(components).flags}
    assert expected_rule in fired, f"{label}: rule never fired"


@pytest.mark.parametrize(
    "components",
    [
        ["CCOC(C)=O", "O"],  # ethyl acetate + water
        ["Cc1ccccc1", "CCN(CC)CC"],  # toluene + triethylamine
    ],
)
def test_an_ordinary_combination_is_not_flagged(components: list[str]) -> None:
    """A table that flags everything trains people to ignore it — including the real flags."""
    assert screen_reaction(components).flags == []


def test_the_azide_in_an_acceptable_solvent_raises_no_pair_flag() -> None:
    """Swapping dichloromethane for acetonitrile clears the pair rule, not the reagent itself.

    Sodium azide keeps its own structural flag in any solvent; only the diazidomethane pair goes
    away. So the assertion is that the pair rule is silent, not that the screen is empty.
    """
    fired = {flag.rule_id for flag in screen_reaction(["[Na+].[N-]=[N+]=[N-]", "CC#N"]).flags}
    assert "azide-with-dichloromethane" not in fired
    assert fired == {"non-carbon-azide"}


def test_each_component_alone_is_unflagged_by_its_pair_rule() -> None:
    """A pair rule must need *both* sides; firing on one would make the pairing meaningless."""
    for single in (["[Na+].[N-]=[N+]=[N-]"], ["ClCCl"]):
        fired = {flag.rule_id for flag in screen_reaction(single).flags}
        assert "azide-with-dichloromethane" not in fired


@pytest.mark.parametrize(
    ("label", "components", "expected_rule"),
    [
        # Each of these is the *same* rule that already fires on the reagent one row above it in
        # the table, spelled the way a catalogue or an ELN actually writes the reagent.
        (
            "sodium peroxide with acetone",
            ["[Na+].[O-][O-].[Na+]", "CC(C)=O"],
            "peroxide-with-ketone",
        ),
        (
            "azide salt in chloroform",
            ["[Na+].[N-]=[N+]=[N-]", "ClC(Cl)Cl"],
            "azide-with-dichloromethane",
        ),
        ("NaH in DCM", ["[Na+].[H-]", "ClCCl"], "saline-hydride-with-chlorinated-solvent"),
        (
            "NaH in 1,2-dichloroethane",
            ["[Na+].[H-]", "ClCCCl"],
            "saline-hydride-with-chlorinated-solvent",
        ),
    ],
)
def test_a_reagent_spelling_the_pair_rule_missed_now_fires(
    label: str, components: list[str], expected_rule: str
) -> None:
    """A reagent spelling a pair arm missed now fires the pair rule.

    Each arm was written for one spelling: sodium peroxide's one-coordinate anionic oxygens,
    chloroform as the triazidomethane source, sodium hydride as a saline hydride. The structural
    flag still fired in each case, so the missing pair flag hid behind a populated result.
    """
    fired = {flag.rule_id for flag in screen_reaction(components).flags}
    assert expected_rule in fired, f"{label}: rule never fired"


@pytest.mark.parametrize(
    ("label", "components"),
    [
        # The widenings must not have bought their coverage by flagging ordinary chemistry.
        ("di-tert-butyl peroxide with acetone", ["CC(C)(C)OOC(C)(C)C", "CC(C)=O"]),
        (
            "an azide salt with a gem-dichloro substrate",
            ["[Na+].[N-]=[N+]=[N-]", "CC(Cl)(Cl)c1ccccc1"],
        ),
        ("NaH in THF", ["[Na+].[H-]", "C1CCOC1"]),
        ("NaH with chlorobenzene", ["[Na+].[H-]", "Clc1ccccc1"]),
    ],
)
def test_the_widenings_did_not_buy_coverage_with_false_flags(
    label: str, components: list[str]
) -> None:
    """The widenings did not buy coverage with false flags.

    Negative controls per widening: di-tert-butyl peroxide is not the acetone-peroxide hazard, a
    substrate's gem-dichloro centre is not a solvent, THF is the recommended hydride solvent, and an
    aryl chloride is not a chlorinated solvent. Asserted as "this rule is absent", since reagents
    keep their own structural flags.
    """
    fired = {flag.rule_id for flag in screen_reaction(components).flags}
    for rule in (
        "peroxide-with-ketone",
        "azide-with-dichloromethane",
        "saline-hydride-with-chlorinated-solvent",
    ):
        assert rule not in fired, f"{label}: {rule} fired on ordinary chemistry"


@pytest.mark.parametrize(
    ("label", "hydrazine"),
    [
        ("free base", "NN"),
        ("hydrate", "NN.O"),
        ("hydrochloride, neutral spelling", "Cl.NN"),
        ("hydrochloride, protonated spelling", "[NH3+]N.[Cl-]"),
        ("sulfate, neutral spelling", "NN.OS(=O)(=O)O"),
        ("sulfate, protonated spelling", "[NH3+]N.[O-]S(=O)(=O)O"),
        ("UDMH", "CN(C)N"),
    ],
)
def test_the_hydrazine_arm_fires_on_every_form_a_catalogue_sells(
    label: str, hydrazine: str
) -> None:
    """The hydrazine arm fires on every form a catalogue sells.

    Hydrazine is weighed out as its hydrochloride or sulfate (`NX4+`), and UDMH has H on one
    nitrogen only; whether a named reagent arrives protonated or neutral is the source's choice.
    Hydrazine plus hydrogen peroxide is hypergolic, so a missed spelling is not cosmetic.
    """
    fired = {flag.rule_id for flag in screen_reaction([hydrazine, "OO"]).flags}
    assert "oxidizer-with-reductant" in fired, f"{label}: the pair rule never fired"
    assert "hydrazine" in fired, f"{label}: the structural rule never fired either"
