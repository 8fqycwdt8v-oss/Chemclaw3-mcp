"""Which bonds of a molecule can be broken, and what the two fragments are.

**The consumer decides the shape.** Chemclaw3's `survey_bond_strengths` job takes a list of
`BondCleavageSpec` — `atoms`, `bond`, `fragments` — and its calling template passes this tool's
output straight through with a comment saying why: *"The field names on each cleavage match the
job's own spec exactly, so this list passes through unchanged — a template cannot rename a field,
and a near-miss here would need a model in the middle to re-type it."* So the field names below are
a cross-repository contract, not a local choice, and `tests/test_cleavage.py` pins them.

**Fragments carry their open shell explicitly.** A homolysis produces two radicals and the SMILES
says so (`[CH3]`, `[OH]`), which is what lets the calculation run without a separately declared
spin state — the job spec's own docstring records this as the reason. A heterolysis produces an
anion and a cation, and which end takes the electrons is decided by electronegativity rather than
left to the caller.

**Only acyclic single bonds, and that is a real restriction rather than a simplification.** Breaking
one bond of a ring does not produce two fragments; it produces one open-chain biradical, whose
energy is not a bond dissociation energy in the sense anyone asks about ("which bond breaks first").
Reporting ring bonds here would put entries in the survey that the survey's own arithmetic — a
balanced reaction per bond — cannot express.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from rdkit import Chem

from chemclaw_mcp_chem.engine.chem import require_canonical_smiles, require_molecule

__all__ = ["MAX_CLEAVAGES", "BondCleavage", "CleavageSet", "enumerate_cleavages"]

CleavageMode = Literal["homolytic", "heterolytic"]

# The bound on a whole-molecule survey. Every entry costs one reaction energy downstream — the job
# spec calls a drug-sized survey "the expensive case this job exists for" — so past this the caller
# should be naming the bonds rather than asking for all of them. A refusal, not a truncation: a
# ranking over "the first 48 bonds the traversal reached" would report a weakest bond that is only
# the weakest of an arbitrary subset.
MAX_CLEAVAGES = 48

# Pauling electronegativities, for deciding which fragment of a heterolysis keeps the electrons.
# Only the elements a bench organic molecule breaks bonds between; an element absent here falls
# back to the atomic-number comparison below, which orders the halogens and chalcogens correctly
# and is the property that actually matters.
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

    Ties break on atomic number, then on index, so the ordering is total and the same molecule
    always yields the same fragment assignment — a heterolysis whose anion depended on traversal
    order would give two different answers for one bond.
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

    None rather than an exception, because a bond whose fragments will not sanitise is one bond of
    a survey and not a failed call — the caller drops it and keeps the rest, which is what makes a
    whole-molecule survey robust on an unusual substrate.
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
    """Whether this bond is one a dissociation survey can express.

    Single, acyclic, and between two real atoms. The ring exclusion is argued in the module
    docstring; the single-bond one is the same argument — breaking one component of a double bond
    is not a dissociation into two fragments.
    """
    return bond.GetBondType() == Chem.BondType.SINGLE and not bond.IsInRing()


def _distinct(mol: Chem.Mol, bonds: list[Chem.Bond]) -> list[Chem.Bond]:
    """One representative per symmetry-equivalent class of bond.

    **This is a cost decision with a correctness consequence, not a tidiness one.** Ethanol has
    three methyl C-H bonds that are the same bond by symmetry; enumerated separately they are three
    entries in the survey, and `survey_bond_strengths` pays *one reaction energy each* for three
    identical numbers. Worse than the waste: the ranking then reports three joint-weakest bonds and
    a chemist reading it cannot tell whether that means "a degenerate set" or "the calculation ran
    three times".

    Equivalence is RDKit's canonical ranking with `breakTies=False`, which is the same mechanism
    `torsions.py` uses to mint a stable handle — atoms related by symmetry share a rank. Two bonds
    are one class when their ranked endpoints match as an unordered pair.

    The representative is the lowest-indexed member, so the answer does not depend on traversal
    order.
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

    Hydrogens are made explicit first, because C-H bonds are the ones a radical-abstraction
    question is usually about and an implicit-hydrogen graph has none of them to break.

    Raises:
        InvalidSmilesError: `smiles` is not a molecule.
        ValueError: more breakable bonds than `MAX_CLEAVAGES`.
    """
    # **Canonicalised before anything is enumerated, and that is the whole join.** `parent` is the
    # only molecule the caller receives, so an index derived from the caller's own spelling names a
    # different atom of it: measured, `OCC` reported `atoms=[0, 1], bond="O-C"`, and atoms 0 and 1
    # of the returned `CCO` are C0-C1 — really bonded, in range, no error anywhere. That is the
    # same failure `describe_atom_sites` records for phenol, and the reason it canonicalises first.
    parent = require_canonical_smiles(smiles)
    mol = Chem.AddHs(require_molecule(parent))
    # Each atom remembers its own index before fragmentation, which is how a fragment is matched
    # back to the end of the bond it came from. RDKit renumbers within a fragment, so nothing else
    # survives the split.
    for atom in mol.GetAtoms():
        atom.SetIntProp("_bond_origin_idx", atom.GetIdx())

    candidates = _distinct(mol, [bond for bond in mol.GetBonds() if _breakable(bond)])
    if len(candidates) > MAX_CLEAVAGES:
        raise ValueError(
            f"{smiles!r} has {len(candidates)} breakable bonds, above the limit of "
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
