"""What one molecule *is*: its canonical form, its scaffold, and the groups it carries.

The functional-group vocabulary is first-party and always used, even where Rxn-INSIGHT is installed:
group names are stored and queried by exact array containment, so they are a wire contract that must
not depend on an optional extra. Deliberately short — the groups a process chemist filters on.
"""

from __future__ import annotations

from rdkit import Chem
from rdkit.Chem.Scaffolds import MurckoScaffold

from chemclaw_mcp_rxnlabel.engine.chem import read_molecule

# `(name, SMARTS)`, matched independently, so a molecule carries every group it matches. Order is
# presentation only. Each name is a wire contract; renaming one requires a `SERVER_VERSION` bump.
FUNCTIONAL_GROUPS: tuple[tuple[str, str], ...] = (
    ("carboxylic acid", "[CX3](=O)[OX2H1]"),
    ("carboxylate", "[CX3](=O)[OX1-]"),
    ("ester", "[CX3](=O)[OX2H0][#6]"),
    ("amide", "[NX3][CX3](=[OX1])[#6]"),
    ("sulfonamide", "[NX3][SX4](=[OX1])(=[OX1])[#6]"),
    ("sulfonyl chloride", "[SX4](=[OX1])(=[OX1])[Cl]"),
    ("acid chloride", "[CX3](=[OX1])[Cl]"),
    ("anhydride", "[CX3](=[OX1])[OX2][CX3]=[OX1]"),
    ("nitrile", "[NX1]#[CX2]"),
    ("aldehyde", "[CX3H1](=O)[#6]"),
    ("ketone", "[#6][CX3](=O)[#6]"),
    ("primary amine", "[NX3;H2;!$(N[#6]=[!#6])][#6]"),
    ("secondary amine", "[NX3;H1;!$(N[#6]=[!#6])]([#6])[#6]"),
    ("tertiary amine", "[NX3;H0;!$(N[#6]=[!#6]);!$(N=*)]([#6])([#6])[#6]"),
    ("aniline", "[NX3][c]"),
    ("alcohol", "[#6;!$(C=O)][OX2H1]"),
    ("phenol", "[c][OX2H1]"),
    ("ether", "[#6;!$(C=O)][OX2H0][#6;!$(C=O)]"),
    ("nitro", "[$([NX3](=O)=O),$([NX3+](=O)[O-])]"),
    ("aryl halide", "[c][F,Cl,Br,I]"),
    ("alkyl halide", "[CX4][F,Cl,Br,I]"),
    ("boronic acid", "[#6][BX3]([OX2H1])[OX2H1]"),
    ("boronate ester", "[#6][BX3]([OX2][#6])[OX2][#6]"),
    ("alkene", "[CX3]=[CX3]"),
    ("alkyne", "[CX2]#[CX2]"),
    ("arene", "c1ccccc1"),
    ("heteroaromatic", "[a;!c]"),
    ("thiol", "[#6][SX2H1]"),
    ("thioether", "[#6][SX2][#6]"),
    ("sulfone", "[#6][SX4](=[OX1])(=[OX1])[#6]"),
    ("azide", "[NX1-]=[NX2+]=[NX1-,NX2H0]"),
    ("carbamate", "[NX3][CX3](=[OX1])[OX2][#6]"),
    ("urea", "[NX3][CX3](=[OX1])[NX3]"),
    ("epoxide", "[OX2r3]1[#6r3][#6r3]1"),
    ("trifluoromethyl", "[CX4](F)(F)F"),
    ("silyl ether", "[OX2][Si]"),
)

_COMPILED = tuple(
    (name, query)
    for name, smarts in FUNCTIONAL_GROUPS
    if (query := Chem.MolFromSmarts(smarts)) is not None
)


def canonical_smiles(smiles: str) -> str | None:
    """RDKit's canonical form, or `None` unless it can be read **whole** (see `engine/chem.py`)."""
    mol = read_molecule(smiles)
    return Chem.MolToSmiles(mol) if mol is not None else None


def scaffold(smiles: str) -> str | None:
    """The Bemis-Murcko scaffold — the ring systems and the linkers between them.

    `None` for an acyclic molecule rather than RDKit's `""`, so acyclic species do not group under
    one empty scaffold.
    """
    mol = read_molecule(smiles)
    if mol is None:
        return None
    core = MurckoScaffold.GetScaffoldForMol(mol)  # type: ignore[no-untyped-call]
    written = Chem.MolToSmiles(core)
    return written or None


def functional_groups(smiles: str) -> list[str] | None:
    """Every group in the vocabulary this molecule carries, or `None` if it could not be read.

    Declaration order, so identical structures give byte-identical stored arrays. `[]` means read
    and carrying none; `None` means unreadable, which a query must not count as a negative.
    """
    mol = read_molecule(smiles)
    if mol is None:
        return None
    return [name for name, query in _COMPILED if mol.HasSubstructMatch(query)]
