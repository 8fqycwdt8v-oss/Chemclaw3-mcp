"""The named-substance incompatibility pairs, each safe apart and dangerous together.

Ported from Chemclaw3's `tests/test_safety_pairs.py`. That repository shipped structural hazard
screening with one generic oxidizer/reductant pair rule, and a parallel screen keyed on resolved
reagent *identity* had independently accumulated the named pairs below. Rather than keep two
screens, that knowledge moved into `rules.yaml` as SMARTS pair rules — so these tests pin it there,
because a rule that silently never fires is worse than no rule: it reports "no rule matched" for a
hazard the table claims to cover.

The azide rule is the cautionary case. Written the obvious way — the X2 form that is correct for an
*organic* azide — it never fired on a **salt**, because RDKit sanitizes the azide anion to two
one-coordinate nitrogens. It was caught only by screening a parsed molecule, which is exactly what
the rule table's own header tells a contributor to do.
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
    """Swapping dichloromethane for acetonitrile clears the *pair* rule — not the reagent itself.

    Sodium azide in MeCN still carries its own structural flag: it is acutely toxic and liberates
    explosive HN3 on contact with acid, in any solvent. Only the diazidomethane hazard — the one the
    solvent substitution exists to remove — goes away, so this asserts the pair rule is silent
    rather than that the whole screen is. Asserting "no flags at all" here is what let the missing
    azide-salt alert (`non-carbon-azide`) look like correct behavior.
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
    """Three arms written for the common spelling, each measured through the shipped screen.

    The class, not three coincidences: a pair rule's arm was written for one way of writing a
    reagent, and the *other* way — the one a catalogue or an ELN uses — fell through. Sodium
    peroxide writes its oxygens as one-coordinate anions, so the HO-OH arm screened it clean;
    chloroform is the source of the triazidomethane the azide rule's own explanation names, and the
    geminal-CH2 arm could only ever produce the di- form; and sodium hydride is a saline hydride, so
    the `[AlH4-]`/`[BH4-]` arm never fired on it at all.

    Each is silent in the worst possible way. The *structural* flag still fires — `peroxide`,
    `non-carbon-azide` — so the screen returns something, and what is missing is precisely the flag
    that names the combination and says do not do this. A reader sees a populated result.
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
    """A broad pattern that flags half the corpus is worse than no rule — this table says so.

    The negative half of each widening above, and the reason each is enumerated rather than
    generalised. Di-tert-butyl peroxide is not the acetone-peroxide hazard (that needs H2O2, and the
    twin arm is `[OX2,OX1-][OX2,OX1-]`, which a dialkyl peroxide does match — so this asserts the
    *pair* stays silent, which is the claim). A substrate's gem-dichloro centre is an ordinary
    functional group and not a solvent choice. THF is the solvent the hydride rule recommends, and
    an aryl chloride is not a chlorinated solvent.

    Asserted as "this rule is not in the set" rather than "no flags at all", for the reason the
    azide-in-MeCN test above gives: a reagent keeps its own structural flags, and demanding an empty
    result would make this pass for the wrong reason the day one is added.
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
    """The widening this rule was given twice, finally exercised against the *reagent*.

    `oxidizer-with-reductant`'s hydrazine arm gained `NX4+` because hydrazine is weighed out as its
    hydrochloride or sulfate, and then dropped the H-on-both-nitrogens requirement for UDMH. Both
    widenings were pinned by SMILES alone, and the claim they support is about a *named* reagent — a
    chemist writes "hydrazine sulfate", and whether that reaches the screen as a protonated or a
    neutral spelling is the source's choice, not theirs. Chemclaw3's reagent table now carries every
    one of these forms (`core/reagents.py`), so both halves of the path exist; this pins the half
    that lives here.

    Hydrazine plus hydrogen peroxide is the archetypal hypergolic pair — ignition on contact, no
    ignition source — so a spelling that slips through is not a cosmetic miss.
    """
    fired = {flag.rule_id for flag in screen_reaction([hydrazine, "OO"]).flags}
    assert "oxidizer-with-reductant" in fired, f"{label}: the pair rule never fired"
    assert "hydrazine" in fired, f"{label}: the structural rule never fired either"
