"""The role tables in `engine/agents.py`, validated against themselves.

Three hand-compiled tables decide what a species *was doing*: a set of transition-metal symbols, a
list of solvent SMILES, and two tuples of SMARTS for ligands and bases. Nothing checked any of
them, and both of the mechanisms around them fail **quietly** — `_matches_any` skips a SMARTS that
will not compile, and `_canonical_or_none` drops a solvent token that will not parse. A dead rule
and an absent rule look identical from outside, so the realistic failure here is not a bad decision
but a transposed character in a row nobody reads again.

That is not hypothetical in this file. The bicarbonate pattern shipped as
`[OX2H0-][CX3](=O)[OX2H0]`, demanding a two-connection anionic oxygen that bicarbonate does not
have, and matched no `NaHCO3` written any way — one of the commonest bases in a coupling corpus,
falling through every rule. And the solvent list shipped 40 tokens for 39 molecules, methyl acetate
written twice.

So this follows `servers/props/tests/test_dataset.py`: every assertion here is an **independently
written** structure or an independently derived fact that must agree with the table, never a
re-typing of the table's own text.

- The reagent SMILES below are typed from the names in the comments beside each rule, in a
  different spelling from anything in `agents.py` — `agents.py` carries SMARTS, this file carries
  SMILES, so a character copied across would not even be well-formed in the other.
- The element facts come from RDKit's periodic table by atomic number, not from a second copy of
  `TRANSITION_METALS`.
- The negative controls are the species the comments in `agents.py` say each rule was narrowed to
  exclude. They are what stops this file passing by a rule that matches everything.
"""

from __future__ import annotations

import pytest
from chemclaw_mcp_rxnlabel.engine import agents
from rdkit import Chem

# A metal is present, so the ligand rules are live. `is_ligand` refuses without one by design.
WITH_METAL = agents.ReactionContext(has_transition_metal=True)

# One reagent per `_BASE_SMARTS` entry, named by the comment that entry carries. Typed from the
# name, not from the pattern: if a rule is edited into something that no longer recognises the
# reagent it was written for, the row here is what says so.
BASES_THE_RULES_NAME = [
    ("potassium carbonate", "[K+].[K+].[O-]C([O-])=O"),
    ("sodium bicarbonate", "OC(=O)[O-].[Na+]"),
    ("sodium hydroxide", "[OH-].[Na+]"),
    ("sodium hydride", "[H-].[Na+]"),
    ("potassium tert-butoxide", "CC(C)(C)[O-].[K+]"),
    ("caesium fluoride", "[F-].[Cs+]"),
    ("tripotassium phosphate", "[O-]P([O-])([O-])=O.[K+].[K+].[K+]"),
    ("dipotassium hydrogen phosphate", "OP([O-])([O-])=O.[K+].[K+]"),
    ("LiHMDS", "C[Si](C)(C)[N-][Si](C)(C)C.[Li+]"),
    ("LDA", "CC(C)[N-]C(C)C.[Li+]"),
    ("DBU", "C1CCC2=NCCCN2CC1"),
    ("tetramethylguanidine", "CN(C)C(=N)N(C)C"),
    ("pyridine", "c1ccncc1"),
    ("DMAP", "CN(C)c1ccncc1"),
    ("2,6-lutidine", "Cc1cccc(C)n1"),
    ("N-methylimidazole", "Cn1ccnc1"),
    ("imidazole", "c1c[nH]cn1"),
    ("triethylamine", "CCN(CC)CC"),
    ("Hunig's base", "CCN(C(C)C)C(C)C"),
    ("N-methylmorpholine", "CN1CCOCC1"),
]

# The species the comments in `_BASE_SMARTS` say each narrowing was written to keep out. A rule
# loosened back to its earlier form fails here rather than silently over-counting a frequency table.
NOT_BASES = [
    # "HOBt is a coupling *additive* and would have been counted as a base through its
    # benzotriazole" — the reason the pyridine rule is a whole six-ring and not a bare `[nX2]`.
    ("HOBt", "On1nnc2ccccc21"),
    ("1,2,4-triazole", "c1nc[nH]n1"),
    ("tetrazole", "c1nn[nH]n1"),
    # "KH2PO4 — one `[O-]`, a buffer — stays out."
    ("potassium dihydrogen phosphate", "OP(O)([O-])=O.[K+]"),
    # A primary aromatic amine is a substrate, not a base: the tertiary-amine rule demands H0.
    ("aniline", "Nc1ccccc1"),
    ("water", "O"),
]

# One reagent per `_LIGAND_SMARTS` entry, again typed from the comment's name.
LIGANDS_THE_RULES_NAME = [
    ("triphenylphosphine", "c1ccc(P(c2ccccc2)c2ccccc2)cc1"),
    ("tricyclohexylphosphine", "C1CCC(P(C2CCCCC2)C2CCCCC2)CC1"),
    ("triphenyl phosphite", "c1ccc(OP(Oc2ccccc2)Oc2ccccc2)cc1"),
    ("IMes imidazolium salt", "Cc1cc(C)c(-[n+]2ccn(-c3c(C)cc(C)cc3C)c2)c(C)c1"),
    ("2,2'-bipyridine", "c1ccc(-c2ccccn2)nc1"),
    ("1,10-phenanthroline", "c1cnc2c(c1)ccc1cccnc12"),
    ("TMEDA", "CN(C)CCN(C)C"),
]

# Solvents spelled differently from every token in `_SOLVENT_SMILES`, so what is tested is that the
# table canonicalises to the molecule rather than that two identical strings are equal.
SOLVENTS_SPELLED_DIFFERENTLY = [
    ("acetonitrile", "N#CC"),
    ("ethanol", "OCC"),
    ("DMF", "O=CN(C)C"),
    ("THF", "C1OCCC1"),
    ("toluene", "c1ccccc1C"),
    ("ethyl acetate", "CC(OCC)=O"),
    ("DMSO", "O=S(C)C"),
    ("dichloromethane", "ClCCl"),
]

# The main-group metals `TRANSITION_METALS`' own comment says are deliberately absent, because "a
# Grignard is not a catalyst and a butyllithium is not a catalyst".
DELIBERATELY_NOT_METALS = ["Li", "Na", "K", "Mg", "Zn"]


@pytest.mark.parametrize("pattern", [*agents._LIGAND_SMARTS, *agents._BASE_SMARTS], ids=lambda p: p)
def test_every_role_smarts_compiles(pattern: str) -> None:
    """`_matches_any` skips a pattern that will not compile, so nothing else would ever notice.

    A dead rule narrows the classification silently: the species falls through to `additive`, which
    is a label and not a blank, so it is invisible in the output as well as in the logs.
    """
    assert Chem.MolFromSmarts(pattern) is not None


@pytest.mark.parametrize(("name", "smiles"), BASES_THE_RULES_NAME, ids=lambda v: str(v))
def test_every_base_rule_recognises_the_reagent_its_comment_names(name: str, smiles: str) -> None:
    """The bicarbonate defect, generalised to every row of the table.

    Each SMARTS in `_BASE_SMARTS` was written for a named reagent. This asserts the two still agree,
    from a structure typed out of the name rather than out of the pattern.
    """
    assert agents.is_base(smiles), f"{name} is no longer matched by any rule in _BASE_SMARTS"


@pytest.mark.parametrize(("name", "smiles"), NOT_BASES, ids=lambda v: str(v))
def test_a_rule_narrowed_to_exclude_something_still_excludes_it(name: str, smiles: str) -> None:
    """Without this, a rule loosened to `[nX2]` or to three-oxygen-free phosphate passes above."""
    assert not agents.is_base(smiles), f"{name} is not a base and a rule in _BASE_SMARTS claims it"


@pytest.mark.parametrize(("name", "smiles"), LIGANDS_THE_RULES_NAME, ids=lambda v: str(v))
def test_every_ligand_rule_recognises_the_reagent_its_comment_names(name: str, smiles: str) -> None:
    """Same audit for `_LIGAND_SMARTS`, with the metal the rules require present."""
    assert agents.is_ligand(smiles, WITH_METAL), f"{name} is matched by no rule in _LIGAND_SMARTS"


def test_no_two_solvent_tokens_are_the_same_molecule() -> None:
    """The measured defect: 40 tokens, 39 molecules — methyl acetate written twice.

    A `frozenset` swallows a duplicate, so the table looked one entry longer than it was and the
    cost was not the duplicate but whichever solvent the slot was meant to hold. Written as a
    token-count-to-set-size comparison rather than as a literal, so it stays true as the list grows.
    """
    tokens = agents._SOLVENT_SMILES.split()
    assert len(tokens) == len(agents.SOLVENTS), (
        f"{len(tokens)} tokens canonicalise to {len(agents.SOLVENTS)} molecules; "
        "a duplicate or an unparseable token is being swallowed"
    )


@pytest.mark.parametrize("token", agents._SOLVENT_SMILES.split(), ids=lambda t: t)
def test_every_solvent_token_parses(token: str) -> None:
    """`_canonical_or_none` drops a token RDKit refuses, so a typo shrinks the table in silence."""
    assert Chem.MolFromSmiles(token) is not None


@pytest.mark.parametrize(("name", "smiles"), SOLVENTS_SPELLED_DIFFERENTLY, ids=lambda v: str(v))
def test_a_solvent_is_recognised_however_it_is_spelled(name: str, smiles: str) -> None:
    """`SOLVENTS` holds canonical forms, so membership must survive a different input spelling."""
    assert agents.is_solvent(smiles), f"{name} is in the table but not recognised as written here"


@pytest.mark.parametrize("symbol", sorted(agents.TRANSITION_METALS))
def test_every_transition_metal_symbol_is_a_d_block_element(symbol: str) -> None:
    """Derived from RDKit's periodic table, not from a second copy of the set.

    This is the check that catches a transposition between two *real* element symbols — `Rb` for
    `Rh`, `Ru` for `Cu` — which a spell-check of the list against itself cannot see. Groups 3-12 are
    the three d-block runs by atomic number.
    """
    number = Chem.GetPeriodicTable().GetAtomicNumber(symbol)
    d_block = 21 <= number <= 30 or 39 <= number <= 48 or 57 <= number <= 80
    assert d_block, f"{symbol} (Z={number}) is not a d-block element"


@pytest.mark.parametrize("symbol", DELIBERATELY_NOT_METALS)
def test_the_main_group_metals_the_comment_excludes_are_still_excluded(symbol: str) -> None:
    """ "A Grignard is not a catalyst" — and adding one here puts it atop every catalyst table."""
    assert symbol not in agents.TRANSITION_METALS
