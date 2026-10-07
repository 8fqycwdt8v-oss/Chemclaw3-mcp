"""Which bonds of a molecule can be broken, and what the two fragments are.

The field names (`atoms`, `bond`, `fragments`) match Chemclaw3's `BondCleavageSpec` so the output
passes straight into `survey_bond_strengths`; they are a cross-repository contract pinned by
`tests/test_cleavage.py`. Homolysis fragments carry their radicals in the SMILES (`[CH3]`);
heterolysis gives the electrons to the more electronegative end. Only acyclic single bonds:
breaking a ring bond gives one biradical, not two fragments.
"""

from __future__ import annotations

from typing import Literal

from mcp_server_kit.limits import echo
from pydantic import BaseModel, Field
from rdkit import Chem

from chemclaw_mcp_chem.engine.chem import require_canonical_smiles, require_molecule

__all__ = ["MAX_CLEAVAGES", "BondCleavage", "CleavageSet", "enumerate_cleavages"]

CleavageMode = Literal["homolytic", "heterolytic"]

# The bound on a whole-molecule survey; each entry costs one reaction energy downstream. Refused,
# never truncated: a ranking over an arbitrary subset would misreport the weakest bond.
MAX_CLEAVAGES = 48

# Pauling electronegativities, deciding which heterolysis fragment keeps the electrons; an absent
# element falls back to the atomic-number comparison.
_ELECTRONEGATIVITY: dict[str, float] = {
    "H": 2.20,
    "B": 2.04,
    "C": 2.55,
    "N": 3.04,
    "O": 3.44,
    "F": 3.98,
    "Si": 1.90,
    "P": 2.19,
    "S": 2.58,
    "Cl": 3.16,
    "Br": 2.96,
    "I": 2.66,
}


class BondCleavage(BaseModel):
    """One breakable bond and the two fragments breaking it produces.

    The three field names are Chemclaw3's `BondCleavageSpec`, verbatim — see the module docstring.
    """

    atoms: list[int] = Field(
        min_length=2,
        max_length=2,
        description=(
            "The two atom indices, into `parent` with hydrogens made explicit — that is, into "
            "`Chem.AddHs(Chem.MolFromSmiles(parent))`, so a hydrogen's index is past the heavy "
            "count. Not into the SMILES the caller wrote: the enumeration is done on the "
            "canonical form, which is the only molecule this tool hands back."
        ),
    )
    bond: str = Field(
        min_length=1,
        description="The bond as a chemist names it, e.g. 'C-H', 'C-O'. Elements, not indices.",
    )
    fragments: list[str] = Field(
        min_length=2,
        max_length=2,
        description=(
            "The two products as SMILES, with radical electrons or charges explicit so the "
            "calculation needs no separately declared spin state."
        ),
    )


class CleavageSet(BaseModel):
    """Every breakable bond of one molecule, under one cleavage mode."""

    parent: str
    mode: CleavageMode
    cleavages: list[BondCleavage]
    count: int


def _pair(mol: Chem.Mol, bond: Chem.Bond) -> tuple[Chem.Atom, Chem.Atom]:
    """The bond's two atoms, ordered so the *more* electronegative one comes second.

    Ties break on atomic number, then index, so the fragment assignment never depends on traversal.
    """

    def key(atom: Chem.Atom) -> tuple[float, int, int]:
        """Electronegativity first, then atomic number, then index — a total order."""
        return (
            _ELECTRONEGATIVITY.get(atom.GetSymbol(), 0.0),
            atom.GetAtomicNum(),
            atom.GetIdx(),
        )

    begin, end = bond.GetBeginAtom(), bond.GetEndAtom()
    return (begin, end) if key(begin) <= key(end) else (end, begin)


def _fragment_smiles(mol: Chem.Mol, bond: Chem.Bond, mode: CleavageMode) -> list[str] | None:
    """The two fragments as SMILES, or None if RDKit cannot make them into molecules.

    None so the caller drops that one bond and keeps the rest of the survey.
    """
    donor, acceptor = _pair(mol, bond)
    broken = Chem.FragmentOnBonds(mol, [bond.GetIdx()], addDummies=False)
    pieces = Chem.GetMolFrags(broken, asMols=True, sanitizeFrags=False)
    if len(pieces) != 2:
        # A ring bond, caught here as well as by the filter below: `FragmentOnBonds` on a ring
        # returns one piece, and treating that as a cleavage would report a biradical as a pair.
        return None
    # Which piece holds which end, so the charge or the radical lands on the right one.
    ends = {donor.GetIdx(): "donor", acceptor.GetIdx(): "acceptor"}
    assignment: list[tuple[str, Chem.Mol]] = []
    for piece in pieces:
        role = ""
        for atom in piece.GetAtoms():
            original = atom.GetPropsAsDict().get("_bond_origin_idx")
            if original is not None and int(original) in ends:
                role = ends[int(original)]
                break
        assignment.append((role, piece))
    return _apply_mode(assignment, mode)


def _apply_mode(assignment: list[tuple[str, Chem.Mol]], mode: CleavageMode) -> list[str] | None:
    """Turn two open-valence pieces into two real molecules under the given mode."""
    out: list[str] = []
    for role, piece in assignment:
        editable = Chem.RWMol(piece)
        target = None
        for atom in editable.GetAtoms():
            if atom.GetPropsAsDict().get("_open_valence"):
                target = atom
                break
        if target is None:
            return None
        if mode == "homolytic":
            target.SetNumRadicalElectrons(target.GetNumRadicalElectrons() + 1)
        else:
            target.SetFormalCharge(target.GetFormalCharge() + (-1 if role == "acceptor" else 1))
        target.SetNoImplicit(True)
        molecule = editable.GetMol()
        try:
            Chem.SanitizeMol(molecule)
        except (Chem.KekulizeException, Chem.AtomValenceException, ValueError):
            return None
        out.append(str(Chem.MolToSmiles(molecule)))
    return out


def _breakable(bond: Chem.Bond) -> bool:
    """Whether a dissociation survey can express this bond: single, acyclic, between real atoms."""
    return bond.GetBondType() == Chem.BondType.SINGLE and not bond.IsInRing()


def _distinct(mol: Chem.Mol, bonds: list[Chem.Bond]) -> list[Chem.Bond]:
    """One representative per symmetry-equivalent class of bond.

    Equivalent bonds would each cost a reaction energy and show up as joint-weakest entries.
    Equivalence is RDKit's canonical ranking with `breakTies=False`; two bonds are one class when
    their ranked endpoints match as an unordered pair. The representative is the lowest-indexed
    member.
    """
    ranks = list(Chem.CanonicalRankAtoms(mol, breakTies=False))
    seen: dict[tuple[int, int], Chem.Bond] = {}
    for bond in bonds:
        begin, end = bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()
        key = (min(ranks[begin], ranks[end]), max(ranks[begin], ranks[end]))
        current = seen.get(key)
        if current is None or bond.GetIdx() < current.GetIdx():
            seen[key] = bond
    return sorted(seen.values(), key=lambda bond: bond.GetIdx())


def enumerate_cleavages(smiles: str, mode: CleavageMode = "homolytic") -> CleavageSet:
    """Every acyclic single bond of `smiles`, with the fragments breaking it produces.

    Hydrogens are made explicit first so C-H bonds can be broken.

    Raises:
        InvalidSmilesError: `smiles` is not a molecule.
        ValueError: more breakable bonds than `MAX_CLEAVAGES`.
    """
    # Canonicalise before enumerating: the indices returned must address `parent`, the only molecule
    # the caller receives, not the caller's own spelling.
    parent = require_canonical_smiles(smiles)
    mol = Chem.AddHs(require_molecule(parent))
    # Each atom records its index before fragmentation, since RDKit renumbers within a fragment.
    for atom in mol.GetAtoms():
        atom.SetIntProp("_bond_origin_idx", atom.GetIdx())

    candidates = _distinct(mol, [bond for bond in mol.GetBonds() if _breakable(bond)])
    if len(candidates) > MAX_CLEAVAGES:
        raise ValueError(
            f"{echo(smiles)!r} has {len(candidates)} breakable bonds, above the limit of "
            f"{MAX_CLEAVAGES}. Every one costs a reaction energy downstream, and a ranking over "
            "an arbitrary subset would report a weakest bond that is only the weakest of that "
            "subset. Name the bonds you want, or ask about a fragment of the molecule."
        )

    cleavages: list[BondCleavage] = []
    for bond in candidates:
        begin, end = bond.GetBeginAtom(), bond.GetEndAtom()
        for atom in (begin, end):
            atom.SetBoolProp("_open_valence", True)
        fragments = _fragment_smiles(mol, bond, mode)
        for atom in (begin, end):
            atom.SetBoolProp("_open_valence", False)
        if fragments is None:
            continue
        cleavages.append(
            BondCleavage(
                atoms=[begin.GetIdx(), end.GetIdx()],
                bond=f"{begin.GetSymbol()}-{end.GetSymbol()}",
                fragments=fragments,
            )
        )
    return CleavageSet(parent=parent, mode=mode, cleavages=cleavages, count=len(cleavages))
