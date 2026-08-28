"""What a species *was doing*: the rules that turn a structure into a role.

The recorded vocabulary an ELN or a patent extract uses has five values — reactant, reagent,
solvent, catalyst, product — and none of them is "ligand" or "base". Those two are most of what a
chemist actually asks for ("which ligand", "which base"), so somebody has to decide them from the
structures. That is this module.

**Rules and a dictionary, not a model, and the reason is that this is a decision a chemist can
check.** A misclassified ligand is not a wrong number, it is a wrong *count* in a frequency table
that somebody then quotes; a rule that says "phosphorus with three carbon substituents, in a
reaction that also contains a transition metal" can be read, argued with and corrected. A learned
classifier over the same evidence would be more accurate at the margins and unauditable in exactly
the place where being wrong is expensive.

**Two rules are context-dependent, and that is the whole reason this takes a reaction rather than a
molecule.** Triphenylphosphine is a ligand in a Suzuki and a stoichiometric *reagent* in a
Mitsunobu — the structure is identical and only the rest of the flask distinguishes them. So a
phosphine is a ligand when the reaction also contains a transition metal, and a reagent when it
does not. The same argument applies to a diimine.
"""

from __future__ import annotations

from dataclasses import dataclass

from rdkit import Chem

from chemclaw_mcp_rxnlabel.engine.chem import read_molecule

# The transition metals whose presence makes a reaction "catalysed" for the purposes of the ligand
# rule above, plus the main-group metals that are ordinarily *reagents* rather than catalysts and
# are deliberately absent (Li, Na, K, Mg, Zn as an organometallic partner). A Grignard is not a
# catalyst and a butyllithium is not a catalyst, and calling either one would put them at the top of
# every "catalysts used" table.
TRANSITION_METALS = frozenset(
    {
        "Sc",
        "Ti",
        "V",
        "Cr",
        "Mn",
        "Fe",
        "Co",
        "Ni",
        "Cu",
        "Y",
        "Zr",
        "Nb",
        "Mo",
        "Tc",
        "Ru",
        "Rh",
        "Pd",
        "Ag",
        "Cd",
        "Hf",
        "Ta",
        "W",
        "Re",
        "Os",
        "Ir",
        "Pt",
        "Au",
        "Hg",
    }
)

# Solvents, by canonical SMILES. A dictionary and not a rule, because "is a solvent" is not a
# structural property: acetonitrile is a solvent and a ligand, water is a solvent and a reagent,
# and DMF is a solvent and a formylating agent. What decides is that the species is one of the few
# dozen things a process chemist pours, and the honest encoding of that is a list.
#
# Kept here rather than read from the `props` server's 44-solvent table on purpose: that table is
# another server's vendored data, reaching it would be an outbound call at request time (which this
# fleet forbids), and copying it would make one file's checksum govern two servers' answers. This
# list overlaps it and is not derived from it.
_SOLVENT_SMILES = """
O CO CCO CC(C)O CCCCO CC(C)(C)O
CC#N CC(C)=O CCOC(C)=O CC(=O)OC
C1CCOC1 CC1CCCO1 COCCOC C1COCCO1 CCOCC CC(C)OC(C)C
CN(C)C=O CN(C)C(C)=O CS(C)=O CN1CCCC1=O
ClCCl ClC(Cl)Cl ClCCCl ClC(Cl)(Cl)Cl
c1ccccc1 Cc1ccccc1 Cc1cccc(C)c1 Clc1ccccc1
CCCCCC CCCCCCC CC(C)CC(C)(C)C C1CCCCC1
CC(=O)O OC=O CCCCCCO CO[Si](C)(C)C
N#Cc1ccccc1 O=C1CCCCC1 CCCCCCCCCCCC
"""


def _canonical_or_none(token: str) -> str | None:
    """`Chem.CanonSmiles(token)`, or `None` for a token that does not parse."""
    if Chem.MolFromSmiles(token) is None:
        return None
    return Chem.CanonSmiles(token)  # type: ignore[no-untyped-call,no-any-return]


SOLVENTS = frozenset(
    canonical for token in _SOLVENT_SMILES.split() if (canonical := _canonical_or_none(token))
)

# Ligand scaffolds, as SMARTS. Each is a donor motif that binds a metal, and each is a *ligand only
# in the presence of one* — see the module docstring.
_LIGAND_SMARTS = (
    # Phosphine: three-coordinate P with only carbon substituents. PPh3, PCy3, XPhos, SPhos,
    # dppf, BINAP — the whole cross-coupling shelf.
    "[PX3](-[#6])(-[#6])-[#6]",
    # Phosphite, phosphoramidite, phosphonite, phosphinite: three-coordinate phosphorus carrying
    # at least one N or O donor. The pattern was `[PX3](-[OX2])(-[OX2])-[OX2]` — three oxygens —
    # while this comment already said "phosphoramidite", which has a nitrogen where one of them
    # would be and so could never match. Measured: `CN(C)P(OC)OC`, the Feringa monodentate ligand
    # and a DNA-synthesis amidite core all failed, and so did the phosphonites and phosphinites the
    # same sentence covers. `#15` rather than `P`, because `P` in SMARTS is *aliphatic* phosphorus
    # and RDKit perceives the Feringa ligand's dioxaphosphepine ring — phosphorus included — as
    # aromatic. P(V) is excluded by `X3`, which is what keeps HMPA, triphenyl phosphate and the
    # phosphate bases out; the phosphine rule above covers the all-carbon case.
    "[#15X3]([#7,#8])",
    # N-heterocyclic carbene, and its imidazolium precursor — which is what is usually charged.
    # The carbene pattern demanded `[#6X2-1]`, a carbon with a formal **-1** charge, and an
    # imidazol-2-ylidene is a *neutral* divalent carbon: measured, IMe, IMes and IPr all parse and
    # none matched, while the only thing that did was `Cn1cc[n+](C)[c-]1`, a zwitterionic spelling
    # of the imidazolium nobody writes. The `:` bonds were the other half — a free carbene's ring
    # is not aromatic to RDKit, and the unsaturated ring's C=C is a double bond, which SMARTS'
    # default bond (single or aromatic) does not match either. `~` covers the saturated and
    # unsaturated rings with one pattern.
    "[#6X2H0;r5]1~[#7]~[#6]~[#6]~[#7]~1",
    "[#7+1]1:[#6]:[#7]:[#6]:[#6]:1",
    # Bidentate diimine: bipyridine, phenanthroline.
    "c1ccnc(-c2ccccn2)c1",
    "c1cnc2c(c1)ccc1cccnc12",
    # Chelating diamine and amino alcohol used as ligands (TMEDA, prolinol-type).
    "[NX3;H0](-[CX4])(-[CX4])-[CX4]-[CX4]-[NX3;H0](-[CX4])-[CX4]",
)

# Base motifs, as SMARTS. Ordered by how unambiguous they are; the first match wins.
_BASE_SMARTS = (
    "[OX1-][CX3](=O)[OX1-]",  # carbonate
    # Bicarbonate. The pattern here was `[OX2H0-][CX3](=O)[OX2H0]`, which demands an oxygen with
    # **two** connections and a negative charge — and bicarbonate is `OC(=O)[O-]`, whose anionic
    # oxygen has one connection while the other carries the proton. It matched no bicarbonate
    # written any way, so every `NaHCO3` reaction — one of the commonest bases in a Suzuki corpus —
    # fell through every rule to `additive`, labelled rather than blank and so invisible.
    "[OX1-][CX3](=O)[OX2H1]",
    "[OH-]",  # hydroxide
    "[H-]",  # hydride: NaH, KH
    "[CX4][O-]",  # alkoxide: NaOMe, KOtBu
    "[F-]",  # fluoride: CsF, TBAF
    # Phosphate, at two deprotonations rather than three: K2HPO4 is charged as a base as often as
    # K3PO4 is, and demanding three `[O-]` excluded it. KH2PO4 — one `[O-]`, a buffer — stays out.
    "[PX4](=O)([OX1-])[OX1-]",
    "[NX2-]([Si])[Si]",  # silyl amide: LiHMDS, NaHMDS
    "[NX2-]([CX4])[CX4]",  # dialkylamide: LDA
    # Amidine and guanidine superbases: DBU, DBN, TMG, TBD.
    "[NX2]=[CX3]-[NX3]",
    # Pyridine-type aromatic nitrogen: pyridine, 2,6-lutidine, collidine, DMAP, quinoline. Written
    # as a whole six-ring with one nitrogen rather than as a bare `[nX2]`, which would also claim
    # the acidic azoles that sit in the same slot — HOBt is a coupling *additive* and would have
    # been counted as a base through its benzotriazole.
    "c1ccncc1",
    # Imidazole-type: imidazole, N-methylimidazole, benzimidazole — the basic ring nitrogen is the
    # two-connection one. The two adjacent ring carbons are what keeps the triazoles and tetrazole
    # out: neither has a c-c bond in the ring.
    "[nX2]1ccnc1",
    # Tertiary amine with no N-H: triethylamine, Hünig's base, DMAP, N-methylmorpholine. Last,
    # because a tertiary amine is also a great many substrates — which is why this rule is only
    # ever consulted for a species already in the agent slot or already known not to be a
    # substrate. See `classify`.
    "[NX3;H0](-[#6])(-[#6])-[#6]",
)


@dataclass(frozen=True)
class ReactionContext:
    """What the rest of the flask contributes to one species' classification.

    One field today, and it is a dataclass rather than a bare bool because the two context-dependent
    rules already disagree about *what* context they need — the ligand rule wants "is there a
    metal", and a future oxidant/reductant rule would want "what changed". A bool named
    `has_metal` threaded through four call sites is the thing that gets extended by adding a second
    bool.
    """

    has_transition_metal: bool


def context_of(species: list[str]) -> ReactionContext:
    """Read the reaction-wide facts the per-species rules need, once."""
    return ReactionContext(has_transition_metal=any(is_metal_complex(s) for s in species))


def is_metal_complex(smiles: str) -> bool:
    """Whether this species contains a transition metal — a catalyst or a precatalyst.

    Deliberately "contains", not "is": `Pd(OAc)2`, `Pd2(dba)3`, `[Pd(PPh3)4]` and a ferrocenyl
    phosphine all answer yes, and all four are what a chemist would point at when asked which
    catalyst was used. The ferrocene case is the one that costs something — dppf is a *ligand* with
    an iron atom in it — and it is handled by `classify` consulting the ligand rules first.
    """
    mol = read_molecule(smiles)
    if mol is None:
        return False
    return any(atom.GetSymbol() in TRANSITION_METALS for atom in mol.GetAtoms())


def is_solvent(smiles: str) -> bool:
    """Whether this species is one of the few dozen things a process chemist pours."""
    mol = read_molecule(smiles)
    return mol is not None and Chem.MolToSmiles(mol) in SOLVENTS


def is_ligand(smiles: str, context: ReactionContext) -> bool:
    """Whether this species is acting as a ligand — which needs a metal to be acting *on*.

    Triphenylphosphine is a ligand in a Suzuki and a stoichiometric reagent in a Mitsunobu, and the
    structure is identical in both. The rest of the flask is the only thing that distinguishes them,
    which is why this takes a context and `is_solvent` does not.
    """
    if not context.has_transition_metal:
        return False
    return _matches_any(smiles, _LIGAND_SMARTS)


def is_base(smiles: str) -> bool:
    """Whether this species is acting as a base.

    Consulted only for a species already known not to be a substrate — see `_BASE_SMARTS`'s last
    entry. A tertiary amine is a base *and* an enormous fraction of medicinal chemistry's
    substrates, so this rule outside that guard would classify half the corpus's products as bases.
    """
    return _matches_any(smiles, _BASE_SMARTS)


def _matches_any(smiles: str, patterns: tuple[str, ...]) -> bool:
    """Whether the structure matches any of the given SMARTS.

    A pattern that does not compile is skipped rather than raised on: these are constants in this
    file, so a bad one is a bug to fix in review — but failing every classification in the corpus
    because one pattern has a typo is a worse failure than silently narrowing the rules.

    **That leniency is only safe because every pattern is asserted individually, and for a long
    time it was not.** This sentence used to claim the tests as a fact; measured by mutation, 1 of
    the 7 ligand patterns and 8 of the 13 base patterns were caught when replaced with a string
    that does not compile, and `_SOLVENT_SMILES` caught 2 of its 40 tokens. A dropped pattern is
    silent in the expensive direction — the species falls through to `additive` or `reagent`, so it
    looks labelled. `tests/test_roles.py::TestEveryHandWrittenPatternIsChecked` is what makes the
    claim true.
    """
    mol = read_molecule(smiles)
    if mol is None:
        return False
    for pattern in patterns:
        query = Chem.MolFromSmarts(pattern)
        if query is not None and mol.HasSubstructMatch(query):
            return True
    return False
