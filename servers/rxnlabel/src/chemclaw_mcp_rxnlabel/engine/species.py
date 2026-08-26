"""What one molecule *is*: its canonical form, its scaffold, and the groups it carries.

**The functional-group vocabulary is first-party and always used, even where Rxn-INSIGHT is
installed.** That is deliberate and it is the one place this server refuses to delegate. The group
names are stored in `reaction_species.functional_groups` and queried by *exact array containment* —
"a product carrying an aryl halide" is `functional_groups @> ARRAY['aryl halide']` — so the
vocabulary is a wire contract, not a convenience. Taking it from an optional dependency would mean
a corpus labelled with the extra installed and one labelled without it answer the same query
differently, which is precisely the silent-divergence failure this whole subsystem is built to
avoid. Rxn-INSIGHT names the *reaction*; this names the molecules.

The list is Ertl-flavoured — it covers what a process chemist filters on — and it is deliberately
short. A hundred groups nobody queries is a hundred rows of array per species; the ones here are
the ones that appear in the questions this index exists to answer.
"""

from __future__ import annotations

from rdkit import Chem
from rdkit.Chem.Scaffolds import MurckoScaffold

from chemclaw_mcp_rxnlabel.engine.chem import read_molecule

# `(name, SMARTS)`, matched independently — a molecule carries every group it matches, so an
# N-aryl amide is both "amide" and "aniline". Order is presentation only.
#
# Each name is a wire contract: it is what a caller passes to `product_functional_group`. Renaming
# one is a labeller-version bump, because every stored row carries the old spelling until it is
# re-labelled.
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

    `None` for an acyclic molecule, which is the honest answer rather than the empty string RDKit
    returns: a solvent has no scaffold, and grouping every acyclic species under `""` would make a
    "which scaffolds appear" roll-up mostly a count of ethanol.
    """
    mol = read_molecule(smiles)
    if mol is None:
        return None
    core = MurckoScaffold.GetScaffoldForMol(mol)
    written = Chem.MolToSmiles(core)
    return written or None


def functional_groups(smiles: str) -> list[str] | None:
    """Every group in the vocabulary this molecule carries, or `None` if it could not be read.

    Order is the declaration's, not the match's, so two identical structures always produce
    byte-identical arrays — which matters because the array is stored and compared.

    **`None` and `[]` are different answers and were the same one.** An empty list means the
    molecule was read and carries no group in this vocabulary; a string that could not be read
    returned the same empty list, and every later "which products carry an aryl halide" query
    counted that row as a negative rather than as unlabelled.
    """
    mol = read_molecule(smiles)
    if mol is None:
        return None
    return [name for name, query in _COMPILED if mol.HasSubstructMatch(query)]
