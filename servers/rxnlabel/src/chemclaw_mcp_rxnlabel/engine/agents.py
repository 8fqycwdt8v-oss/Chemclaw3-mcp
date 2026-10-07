"""What a species *was doing*: the rules that turn a structure into a role.

Recorded vocabularies have no "ligand" or "base", yet those are what chemists ask for, so this
module decides them from structure. Rules and a dictionary rather than a model, so a chemist can
read and correct a decision that ends up as a count in a frequency table.

Two rules are context-dependent, which is why this takes a reaction rather than a molecule: a
phosphine (or diimine) is a ligand when the reaction also contains a transition metal and a reagent
when it does not (PPh3 in a Suzuki versus a Mitsunobu).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache

from rdkit import Chem

from chemclaw_mcp_rxnlabel.engine.chem import read_molecule

# Transition metals that make a reaction "catalysed" for the ligand rule. Main-group metals used as
# stoichiometric reagents (Li, Na, K, Mg, Zn) are deliberately absent.
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

# Solvents, by canonical SMILES. A list rather than a rule, because "is a solvent" is not structural
# (acetonitrile, water and DMF have other roles too). Kept here rather than read from the `props`
# server, which would be a request-time call. `tests/test_agent_tables.py` checks it for duplicates
# and unparseable tokens, which a `frozenset` and `_canonical_or_none` would otherwise swallow
# silently.
_SOLVENT_SMILES = """
O CO CCO CC(C)O CCCCO CC(C)(C)O
CC#N CC(C)=O CCOC(C)=O COC(C)=O
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
    # Phosphite and phosphoramidite: P(III) with heteroatom substituents.
    "[PX3](-[OX2])(-[OX2])-[OX2]",
    # N-heterocyclic carbene, and its imidazolium precursor — which is what is usually charged.
    "[#6X2-1]1:[#7]:[#6]:[#6]:[#7]:1",
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
    # Bicarbonate: the anionic oxygen has one connection, the other carries the proton.
    "[OX1-][CX3](=O)[OX2H1]",
    "[OH-]",  # hydroxide
    "[H-]",  # hydride: NaH, KH
    "[CX4][O-]",  # alkoxide: NaOMe, KOtBu
    "[F-]",  # fluoride: CsF, TBAF
    # Phosphate at two deprotonations, so K2HPO4 counts as a base; KH2PO4 (a buffer) stays out.
    "[PX4](=O)([OX1-])[OX1-]",
    "[NX2-]([Si])[Si]",  # silyl amide: LiHMDS, NaHMDS
    "[NX2-]([CX4])[CX4]",  # dialkylamide: LDA
    # Amidine and guanidine superbases: DBU, DBN, TMG, TBD.
    "[NX2]=[CX3]-[NX3]",
    # Pyridine-type nitrogen (pyridine, lutidine, DMAP, quinoline). A whole six-ring rather than a
    # bare `[nX2]`, which would also claim acidic azoles such as HOBt.
    "c1ccncc1",
    # Imidazole-type; the two adjacent ring carbons keep triazoles and tetrazole out.
    "[nX2]1ccnc1",
    # Tertiary amine with no N-H (Et3N, DIPEA, NMM). Last, because many substrates are tertiary
    # amines; only consulted for species known not to be substrates (see `classify`).
    "[NX3;H0](-[#6])(-[#6])-[#6]",
)


@dataclass(frozen=True)
class ReactionContext:
    """What the rest of the flask contributes to one species' classification.

    A dataclass rather than a bool so a further context-dependent rule adds a field, not a
    parameter.
    """

    has_transition_metal: bool


def context_of(species: list[str]) -> ReactionContext:
    """Read the reaction-wide facts the per-species rules need, once."""
    return ReactionContext(has_transition_metal=any(is_metal_complex(s) for s in species))


def is_metal_complex(smiles: str) -> bool:
    """Whether this species contains a transition metal — a catalyst or a precatalyst.

    "Contains", not "is", so precatalysts and complexes count. A ferrocenyl phosphine such as dppf
    is a ligand; `classify` consults the ligand rules first for that reason.
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
    """Whether this species is acting as a ligand — which needs a metal to be acting *on*."""
    if not context.has_transition_metal:
        return False
    return _matches_any(smiles, _LIGAND_SMARTS)


def is_base(smiles: str) -> bool:
    """Whether this species is acting as a base.

    Consult only for a species known not to be a substrate: many substrates are tertiary amines.
    """
    return _matches_any(smiles, _BASE_SMARTS)


def _matches_any(smiles: str, patterns: tuple[str, ...]) -> bool:
    """Whether the structure matches any of the given SMARTS.

    A pattern that does not compile is skipped rather than failing every classification; that
    leniency is safe only because `tests/test_agent_tables.py` checks every pattern compiles and
    matches the reagents its comment names.
    """
    mol = read_molecule(smiles)
    if mol is None:
        return False
    for pattern in patterns:
        query = _compiled(pattern)
        if query is not None and mol.HasSubstructMatch(query):
            return True
    return False


@cache
def _compiled(pattern: str) -> Chem.Mol | None:
    """One SMARTS from `_LIGAND_SMARTS` or `_BASE_SMARTS`, compiled once per process.

    Re-parsing the tables dominated the per-species cost. Keyed on the string so an uncompilable
    pattern stays a per-pattern skip; the key set is the literals in the two tables, and none is
    recursive, so the shared query is read-only under matching.
    """
    return Chem.MolFromSmarts(pattern)
