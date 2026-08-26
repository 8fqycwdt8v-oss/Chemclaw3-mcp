"""What a chemist calls each atom of a molecule, so a per-atom number can be reported by name.

**The problem this exists to remove is `enumerate_torsions`' problem, one dimension down.**
`predict_site_reactivity` ranks atoms and returns `index=4, element=C`; the chemist asked which ring
position is nitrated. Nothing maps between the two, so either the request stops or the model works
the mapping out in its head from a SMILES string — the second being the dangerous one, because a
mis-attributed index produces a well-formed sentence about the wrong atom with no error anywhere.

So a site needs a name that survives being written down: a **handle** derived from the molecule
rather than from the order its atoms happen to appear in, exactly as `torsion_handle` does for a
bond. `D-2026-08-26-a-torsion-is-named-not-indexed` is the decision this module sits under.

**A site is a symmetry class, not an atom, and that is the load-bearing choice.** Toluene's two
*ortho* carbons are one question, asked once. Reporting them separately invites a comparison between
two atoms that are the same atom — measured on phenol, the two *ortho* carbons' Fukui indices
differ by 0.0088 purely because the planar O-H makes one *syn* and the other *anti*, which is the
same size as the *ortho*-to-*meta* difference a reader would draw a conclusion from. Grouping is
what lets the caller report a mean and a spread instead of a spurious ordering.

**Nothing here calculates anything.** It is the molecular graph and a table of SMARTS, so it is
free,
it is `read_only`, and it can be asked *before* a plan is approved — which matters, because "which
positions would you even be comparing?" is a question a chemist wants answered before authorising
minutes of CPU.
"""

from __future__ import annotations

import hashlib
from typing import Literal

import rdkit
from pydantic import BaseModel, Field
from rdkit import Chem

from chemclaw_mcp_chem.engine.chem import require_molecule

__all__ = [
    "SCOPES",
    "Site",
    "SiteKind",
    "SiteScope",
    "describe_atom_sites",
    "site_handle",
]

# What sort of atom this is, in the words a chemist uses. The classification decides nothing on its
# own — it is what a request in words ("the carbonyl", "the ring positions") is matched against, and
# what a reader checks the choice by.
SiteKind = Literal[
    "aromatic_carbon",
    "aryl_halide_carbon",
    "carboxyl_carbon",
    "ester_carbon",
    "amide_carbon",
    "carbonyl_carbon",
    "nitrile_carbon",
    "michael_beta_carbon",
    "halide_carbon",
    "benzylic_carbon",
    "aliphatic_carbon",
    "aromatic_nitrogen",
    "amide_nitrogen",
    "amine_nitrogen",
    "nitro_nitrogen",
    "carbonyl_oxygen",
    "hydroxyl_oxygen",
    "ether_oxygen",
    "thioether_sulfur",
    "thiol_sulfur",
    "halogen",
    "heteroatom",
]

# Which question a site is a candidate answer to. A scope is how a caller asks for the *right rows*
# rather than more rows: `predict_site_reactivity` on phenol already returns every atom and still
# buries the para carbon at rank 6, behind the oxygen and four hydrogens, so truncation is not the
# knob that was missing.
SiteScope = Literal[
    "ring_carbons",
    "ch_sites",
    "heteroatoms",
    "electrophilic_carbons",
    "all",
]

SCOPES: tuple[SiteScope, ...] = (
    "ring_carbons",
    "ch_sites",
    "heteroatoms",
    "electrophilic_carbons",
    "all",
)

# The environment each kind is recognised by, **matched atom zero being the site itself**, in
# priority order: the first pattern whose first matched atom is this atom wins. Ordering is the
# whole specificity mechanism — a carboxylic acid carbon also matches the plain carbonyl pattern,
# and an amide nitrogen also matches the amine one, so the specific rows come first.
_KINDS: tuple[tuple[SiteKind, str], ...] = (
    # Carbons an electrophile-seeking question is about. The three acyl rows are separate because
    # "which of these two carbonyls reacts first" is the chemoselectivity question, and answering it
    # with one `carbonyl_carbon` label for both would make the answer unsayable.
    ("carboxyl_carbon", "[CX3](=[OX1])[OX2H1]"),
    ("ester_carbon", "[CX3](=[OX1])[OX2H0][#6]"),
    ("amide_carbon", "[CX3](=[OX1])[NX3]"),
    ("carbonyl_carbon", "[CX3]=[OX1]"),
    ("nitrile_carbon", "[CX2]#[NX1]"),
    # The beta carbon of a Michael acceptor: matched atom zero is the alkene carbon *distal* from
    # the carbonyl, which is the one a nucleophile adds to and the one a warhead is tuned at.
    ("michael_beta_carbon", "[CX3;!$([CX3]=[OX1])]=[CX3][CX3]=[OX1]"),
    # An aromatic carbon carrying a leaving group — the SNAr site. Ahead of `aromatic_carbon`
    # because that is exactly the distinction "which chlorine goes first" turns on.
    ("aryl_halide_carbon", "[c][F,Cl,Br,I]"),
    ("halide_carbon", "[CX4][F,Cl,Br,I]"),
    ("benzylic_carbon", "[CX4][a]"),
    ("aromatic_carbon", "[c]"),
    # Heteroatoms, specific before general.
    ("nitro_nitrogen", "[NX3](=[OX1])=[OX1]"),
    ("amide_nitrogen", "[NX3][CX3]=[OX1]"),
    ("aromatic_nitrogen", "[n]"),
    ("amine_nitrogen", "[NX3;!$([NX3]=*)]"),
    ("carbonyl_oxygen", "[OX1]=[CX3]"),
    ("hydroxyl_oxygen", "[OX2H1]"),
    ("ether_oxygen", "[OX2H0]"),
    ("thiol_sulfur", "[SX2H1]"),
    ("thioether_sulfur", "[SX2H0]"),
    ("halogen", "[F,Cl,Br,I]"),
)

# What each kind is called in a sentence. Kept beside the patterns rather than derived from the
# enum name, because "aryl_halide_carbon" is a classification and "the aromatic carbon bearing the
# leaving group" is what a chemist reads.
_NOUNS: dict[SiteKind, str] = {
    "aromatic_carbon": "aromatic carbon",
    "aryl_halide_carbon": "aromatic carbon bearing the leaving group",
    "carboxyl_carbon": "carboxylic acid carbon",
    "ester_carbon": "ester carbonyl carbon",
    "amide_carbon": "amide carbonyl carbon",
    "carbonyl_carbon": "carbonyl carbon",
    "nitrile_carbon": "nitrile carbon",
    "michael_beta_carbon": "Michael acceptor beta carbon",
    "halide_carbon": "carbon bearing the halide",
    "benzylic_carbon": "benzylic carbon",
    "aliphatic_carbon": "aliphatic carbon",
    "aromatic_nitrogen": "aromatic nitrogen",
    "amide_nitrogen": "amide nitrogen",
    "amine_nitrogen": "amine nitrogen",
    "nitro_nitrogen": "nitro nitrogen",
    "carbonyl_oxygen": "carbonyl oxygen",
    "hydroxyl_oxygen": "hydroxyl oxygen",
    "ether_oxygen": "ether oxygen",
    "thioether_sulfur": "thioether sulfur",
    "thiol_sulfur": "thiol sulfur",
    "halogen": "halogen",
    "heteroatom": "heteroatom",
}

# The classical relationship names, by how many ring bonds separate a position from the reference.
# Six-membered rings only: "meta" on a five-ring is not a thing anyone says, and inventing a name
# for it would be worse than leaving the field empty.
_RELATIONS: dict[int, str] = {0: "ipso", 1: "ortho", 2: "meta", 3: "para"}

# Which kinds are electrophilic carbons for scope purposes. Written as a set rather than inferred
# from the name so that adding a kind is a deliberate decision about which questions it answers.
_ELECTROPHILIC: frozenset[SiteKind] = frozenset(
    {
        "carboxyl_carbon",
        "ester_carbon",
        "amide_carbon",
        "carbonyl_carbon",
        "nitrile_carbon",
        "michael_beta_carbon",
        "halide_carbon",
        "aryl_halide_carbon",
    }
)


class Site(BaseModel):
    """One symmetry-distinct atom of a molecule, named so the name survives a rewritten SMILES."""

    site_id: str = Field(
        description="The handle for this site — stable across every way of writing the molecule."
    )
    atoms: list[int] = Field(
        description=(
            "Every heavy atom in this symmetry class, as the canonical SMILES numbers them. "
            "Reporting one number for the class is the point: they are the same atom, and a "
            "difference between them is geometry noise rather than chemistry."
        )
    )
    hydrogens: list[int] = Field(
        description=(
            "The indices of the hydrogens attached to this site, numbered as a calculator numbers "
            "them — heavy atoms first in canonical order, then hydrogens in order of the atom they "
            "hang off. This is the join key for a C-H question: the ranking is read on the "
            "hydrogen, the answer is reported on the carbon."
        )
    )
    element: str
    label: str = Field(description="What a chemist calls this site.")
    kind: SiteKind
    smarts: str = Field(description="The environment that was matched, so the label is checkable.")
    scopes: list[SiteScope] = Field(
        description="Which question scopes this site belongs to, so a caller can filter without "
        "re-deriving the chemistry."
    )
    aromatic: bool
    ring_size: int | None = Field(
        default=None, description="Size of the smallest ring this atom is in, or null if acyclic."
    )
    ring_position: str | None = Field(
        default=None,
        description="ipso / ortho / meta / para relative to the ring's reference atom. "
        "Six-membered rings only; null elsewhere, because the classical names mean nothing on a "
        "five-ring, and null for the reference atom itself when that is a ring heteroatom.",
    )
    ring_bonds_from_reference: int | None = Field(
        default=None,
        description="How many ring bonds separate this atom from the reference, counted the short "
        "way round and through the ring. A relationship, deliberately not a locant: reproducing "
        "IUPAC ring numbering needs a direction and a substituent-priority rule, and a locant that "
        "is subtly wrong reads exactly like one that is right.",
    )
    ring_reference: str | None = Field(
        default=None,
        description="What the position is measured from, so the answer is checkable rather than "
        "assumed.",
    )
    adjacent_ring_heteroatoms: int = Field(
        default=0,
        description="Ring heteroatoms bonded to this atom. The distinguishing fact in an azine: "
        "in 2,4-dichloropyrimidine both C-Cl carbons are ortho to a ring nitrogen and only one "
        "sits between two, which is what decides which chlorine goes first.",
    )
    hybridisation: str
    hydrogen_count: int = Field(description="Hydrogens on each atom of this class.")
    heavy_degree: int = Field(description="Heavy-atom neighbours of each atom of this class.")
    formal_charge: int


def site_handle(mol: Chem.Mol, atom_index: int) -> str:
    """A content-addressed name for one symmetry class of `mol`.

    The three properties are `torsion_handle`'s, for the same three reasons one dimension down:

    - **It does not change when the SMILES is rewritten.** The atom is named by its canonical
      symmetry class rather than by its index, so `CC(=O)Nc1ccccc1` and `c1ccc(NC(C)=O)cc1` give the
      amide nitrogen one handle while the indices differ.
    - **Symmetry-equivalent atoms share it.** Toluene's two *ortho* carbons are one site, and get
      one handle, so they cannot be reported as two competing answers.
    - **It fails loudly after a toolchain bump.** The RDKit version is in the payload, because the
      canonical ranking is a function of that build, and a handle that quietly resolved to a
      different atom under a new build is the silent failure this module exists to remove.

    Args:
        mol: The molecule the atom belongs to.
        atom_index: Any atom of the class; every member gives the same handle.

    Returns:
        `site_` followed by sixteen hex characters.
    """
    classes = list(Chem.CanonicalRankAtoms(mol, breakTies=False))
    payload = f"{rdkit.__version__}|{Chem.MolToSmiles(mol)}|{classes[atom_index]}"
    return "site_" + hashlib.sha256(payload.encode()).hexdigest()[:16]


def describe_atom_sites(smiles: str) -> list[Site]:
    """Every symmetry-distinct heavy atom of `smiles`, named the way a chemist names it.

    Hydrogens are not sites of their own: a C-H question is asked about the carbon and answered on
    the carbon, with `hydrogens` carrying the indices a calculator will have put its numbers on.
    That is the same refusal `enumerate_torsion_candidates` makes for a symmetric top — a hydrogen
    index is meaningful only inside one particular explicit-H numbering, and handing one out invites
    it to be carried somewhere it means something else.

    Raises:
        InvalidSmilesError: `smiles` is not a molecule.
    """
    mol = require_molecule(smiles)
    with_hydrogens = Chem.AddHs(mol)
    ranks = list(Chem.CanonicalRankAtoms(mol, breakTies=True))
    matched = {kind: _matched_atoms(mol, pattern) for kind, pattern in _KINDS}
    references = _ring_references(mol, ranks)
    hydrogens = _hydrogen_indices(with_hydrogens, mol.GetNumAtoms())

    by_handle: dict[str, list[int]] = {}
    for atom in mol.GetAtoms():
        by_handle.setdefault(site_handle(mol, atom.GetIdx()), []).append(atom.GetIdx())

    sites: list[Site] = []
    for handle, members in by_handle.items():
        # The representative is the lowest canonically-ranked member, so which atom of a class is
        # described does not depend on how the molecule was written.
        index = min(members, key=lambda one: ranks[one])
        atom = mol.GetAtomWithIdx(index)
        kind = _classify(atom, matched)
        placement = _ring_placement(mol, index, references)
        sites.append(
            Site(
                site_id=handle,
                atoms=sorted(members),
                hydrogens=sorted(one for member in members for one in hydrogens.get(member, [])),
                element=atom.GetSymbol(),
                label=_label(atom, kind, placement),
                kind=kind,
                smarts=dict(_KINDS).get(kind, "[*]"),
                scopes=_scopes(atom, kind),
                aromatic=atom.GetIsAromatic(),
                ring_size=_ring_size(mol, index),
                ring_position=placement.relation,
                ring_bonds_from_reference=placement.distance,
                ring_reference=placement.reference,
                adjacent_ring_heteroatoms=_adjacent_ring_heteroatoms(mol, index),
                hybridisation=str(atom.GetHybridization()),
                hydrogen_count=atom.GetTotalNumHs(),
                heavy_degree=atom.GetDegree(),
                formal_charge=atom.GetFormalCharge(),
            )
        )
    # Sorted so two runs, and two writings, list the same sites in the same order.
    return sorted(sites, key=lambda site: site.atoms[0])


def _hydrogen_indices(with_hydrogens: Chem.Mol, heavy_count: int) -> dict[int, list[int]]:
    """Map each heavy atom to the indices its hydrogens carry once hydrogens are explicit.

    Read off the `AddHs` molecule rather than computed from an offset. The arithmetic happens to
    work — `AddHs` preserves the heavy prefix and appends hydrogens in parent order — but that is a
    property of an RDKit implementation, and every calculator in this family numbers atoms by
    calling the same function, so reading the answer is both free and exact.
    """
    attached: dict[int, list[int]] = {}
    for atom in with_hydrogens.GetAtoms():
        if atom.GetAtomicNum() == 1 and atom.GetIdx() >= heavy_count:
            attached.setdefault(atom.GetNeighbors()[0].GetIdx(), []).append(atom.GetIdx())
    return attached


def _matched_atoms(mol: Chem.Mol, pattern: str) -> set[int]:
    """The atoms this SMARTS puts in position zero — the site the pattern is about."""
    query = Chem.MolFromSmarts(pattern)
    return {match[0] for match in mol.GetSubstructMatches(query)}


def _classify(atom: Chem.Atom, matched: dict[SiteKind, set[int]]) -> SiteKind:
    """Which kind of site this is, in the order the patterns are written.

    The fallbacks are deliberate rather than a default branch: an unmatched carbon is aliphatic, and
    an unmatched anything-else is a heteroatom — which is still a true statement a caller can scope
    on, where `"other"` would not be.
    """
    for kind, _ in _KINDS:
        if atom.GetIdx() in matched[kind]:
            return kind
    return "aliphatic_carbon" if atom.GetSymbol() == "C" else "heteroatom"


def _scopes(atom: Chem.Atom, kind: SiteKind) -> list[SiteScope]:
    """Which question scopes this site answers.

    A site can be in several — the beta carbon of an acrylamide is an electrophilic carbon *and*,
    if it carries a hydrogen, a C-H site — because the scopes are questions, not a partition.
    """
    found: list[SiteScope] = []
    if atom.GetSymbol() == "C" and atom.IsInRing():
        found.append("ring_carbons")
    if atom.GetSymbol() == "C" and atom.GetTotalNumHs() > 0:
        found.append("ch_sites")
    if atom.GetAtomicNum() not in (1, 6):
        found.append("heteroatoms")
    if kind in _ELECTROPHILIC:
        found.append("electrophilic_carbons")
    found.append("all")
    return found


def _ring_size(mol: Chem.Mol, index: int) -> int | None:
    """The smallest ring this atom belongs to, or None if it is acyclic."""
    rings = [ring for ring in mol.GetRingInfo().AtomRings() if index in ring]
    return min(len(ring) for ring in rings) if rings else None


def _ring_references(mol: Chem.Mol, ranks: list[int]) -> dict[tuple[int, ...], int]:
    """Choose the atom each ring's positions are counted from.

    A locant is meaningless without saying what it counts from, and the two conventions a chemist
    actually uses are different: a heteroaromatic is numbered **from its heteroatom** (pyridine's
    C4), and a substituted carbocycle is described **relative to its substituent** (phenol's
    *para*).
    So the reference is the lowest-canonically-ranked heteroatom if the ring has one, else the
    lowest-ranked substituted ring atom, else nothing — benzene has no reference because every
    position of benzene is the same position, and inventing one would number six identical atoms.

    Ranked rather than lowest-index throughout, for `torsion_handle`'s reason: an index depends on
    how the molecule was written.
    """
    references: dict[tuple[int, ...], int] = {}
    for ring in mol.GetRingInfo().AtomRings():
        heteroatoms = [one for one in ring if mol.GetAtomWithIdx(one).GetAtomicNum() not in (1, 6)]
        substituted = [one for one in ring if _has_substituent(mol, one, ring)]
        candidates = heteroatoms or substituted
        if candidates:
            references[tuple(ring)] = min(candidates, key=lambda one: ranks[one])
    return references


def _has_substituent(mol: Chem.Mol, index: int, ring: tuple[int, ...]) -> bool:
    """Does this ring atom carry a heavy substituent outside the ring?"""
    return any(
        neighbour.GetIdx() not in ring and neighbour.GetAtomicNum() > 1
        for neighbour in mol.GetAtomWithIdx(index).GetNeighbors()
    )


class _Placement(BaseModel):
    """Where one atom sits in its ring, relative to that ring's reference atom.

    All three are None together for an acyclic atom, for a ring with no distinguishable reference
    (benzene, where every position is the same position), and for a ring heteroatom that *is* its
    own ring's reference — "ipso to itself" is not a thing anyone says.
    """

    relation: str | None = None
    distance: int | None = None
    reference: str | None = None


def _ring_placement(
    mol: Chem.Mol, index: int, references: dict[tuple[int, ...], int]
) -> _Placement:
    """Where in its ring this atom sits, relative to the reference `_ring_references` chose.

    The distance is counted **through the ring**, not through the molecule, because a fused
    system's shortest path between two positions of one ring can leave that ring.

    **`relation` is emitted only for the substituent convention.** *Ortho*, *meta* and *para* name
    positions relative to a substituent on a six-membered ring; applying them to a ring heteroatom's
    own numbering mixes that convention with IUPAC locants, and the two disagree. So a heteroatom
    reference yields the distance and the reference name — "two ring bonds from the ring N" is true
    and checkable — while `relation` stays None unless the ring is a six-ring numbered from a
    substituent. Pyridine is the exception worth having: with a single ring heteroatom and no
    competing convention, "para to the ring N" *is* what a chemist says, so a six-ring whose
    reference is its only heteroatom keeps the classical names.
    """
    for ring in mol.GetRingInfo().AtomRings():
        if index not in ring or ring not in references:
            continue
        reference = references[ring]
        steps = _ring_distance(ring, mol, reference, index)
        if steps is None:
            continue
        if index == reference and mol.GetAtomWithIdx(reference).GetAtomicNum() not in (1, 6):
            return _Placement()
        return _Placement(
            relation=_RELATIONS.get(steps) if _classical(mol, ring, reference) else None,
            distance=steps,
            reference=_reference_label(mol, reference, ring),
        )
    return _Placement()


def _classical(mol: Chem.Mol, ring: tuple[int, ...], reference: int) -> bool:
    """May this ring's positions carry the *ortho*/*meta*/*para* names?

    Three conditions, each removing a case where the classical names would be read as saying more
    than they do. The ring must be a six-ring. There must be one convention in play — either the
    reference is a substituted carbon (benzene chemistry) or it is the ring's *sole* heteroatom
    (pyridine, where "para to N" is standard); a ring with two heteroatoms is numbered rather than
    related, so pyrimidine gets a distance instead. And the reference must not be a **ring fusion**:
    naphthalene's positions are alpha and beta, not *ortho* and *para*, and calling a fusion carbon
    a substituent would be a claim about a bond that is not there.
    """
    if len(ring) != 6 or _is_fusion(mol, reference, ring):
        return False
    heteroatoms = [one for one in ring if mol.GetAtomWithIdx(one).GetAtomicNum() not in (1, 6)]
    return not heteroatoms or (len(heteroatoms) == 1 and heteroatoms[0] == reference)


def _is_fusion(mol: Chem.Mol, index: int, ring: tuple[int, ...]) -> bool:
    """Is this ring atom shared with another ring, rather than carrying a substituent?"""
    return (
        any(
            neighbour.GetIdx() not in ring and neighbour.IsInRing()
            for neighbour in mol.GetAtomWithIdx(index).GetNeighbors()
        )
        and mol.GetRingInfo().NumAtomRings(index) > 1
    )


def _adjacent_ring_heteroatoms(mol: Chem.Mol, index: int) -> int:
    """Ring heteroatoms bonded to this atom.

    Counted over neighbours that share a ring with it, so an exocyclic amine on an aromatic carbon
    is not mistaken for a ring nitrogen — the two have opposite electronic effects, and confusing
    them would invert exactly the answer this field exists to support.
    """
    atom = mol.GetAtomWithIdx(index)
    if not atom.IsInRing():
        return 0
    rings = [ring for ring in mol.GetRingInfo().AtomRings() if index in ring]
    shared = {one for ring in rings for one in ring}
    return sum(
        1
        for neighbour in atom.GetNeighbors()
        if neighbour.GetIdx() in shared and neighbour.GetAtomicNum() not in (1, 6)
    )


def _ring_distance(ring: tuple[int, ...], mol: Chem.Mol, start: int, target: int) -> int | None:
    """How many ring bonds separate two atoms of one ring, going the short way round."""
    order = _ring_order(ring, mol)
    if order is None or start not in order or target not in order:
        return None
    offset = abs(order.index(start) - order.index(target))
    return min(offset, len(order) - offset)


def _ring_order(ring: tuple[int, ...], mol: Chem.Mol) -> list[int] | None:
    """The ring's atoms in connectivity order, since `AtomRings` gives a set, not a walk."""
    remaining = set(ring)
    walk = [next(iter(remaining))]
    remaining.discard(walk[0])
    while remaining:
        following = next(
            (one for one in remaining if mol.GetBondBetweenAtoms(walk[-1], one) is not None),
            None,
        )
        if following is None:  # pragma: no cover - an AtomRings ring is always a cycle
            return None
        walk.append(following)
        remaining.discard(following)
    return walk


def _reference_label(mol: Chem.Mol, reference: int, ring: tuple[int, ...]) -> str:
    """Name the atom a position is measured from, in the terms each convention uses.

    A heteroatom reference is named as itself ("the ring N"); a substituted-carbon reference is
    named by *what it carries* ("the OH substituent"), because "para to the OH" is the sentence a
    chemist reads and "para to C1" is not; a ring-fusion carbon is named as a fusion, because it
    carries no substituent at all.

    **The index is appended when the name alone does not identify the atom** — a pyrimidine has two
    ring nitrogens, and "two ring bonds from the ring N" is ambiguous without saying which. An index
    is a poor name and a fine disambiguator, which is the only role it has here.
    """
    atom = mol.GetAtomWithIdx(reference)
    if _is_fusion(mol, reference, ring):
        return "the ring fusion"
    if atom.GetAtomicNum() not in (1, 6):
        name = f"the ring {atom.GetSymbol()}"
        same = [one for one in ring if mol.GetAtomWithIdx(one).GetSymbol() == atom.GetSymbol()]
        return name if len(same) == 1 else f"{name} at atom {reference}"
    substituent = next(
        (
            neighbour
            for neighbour in atom.GetNeighbors()
            if neighbour.GetIdx() not in ring and neighbour.GetAtomicNum() > 1
        ),
        None,
    )
    if substituent is None:  # pragma: no cover - a carbon reference is substituted by construction
        return f"the substituent at atom {reference}"
    hydrogens = substituent.GetTotalNumHs()
    tail = "H" if hydrogens else ""
    count = str(hydrogens) if hydrogens > 1 else ""
    return f"the {substituent.GetSymbol()}{tail}{count} substituent"


def _label(atom: Chem.Atom, kind: SiteKind, placement: _Placement) -> str:
    """What to call this site in a sentence a chemist can check the choice against.

    A ring position gets the placement that makes it identifiable — `"the para aromatic carbon
    (para to the OH substituent)"` — because the whole purpose of this module is that an answer
    names a position rather than an index. Where the classical names do not apply, the distance is
    spelled out instead of being dressed up as a locant.
    """
    noun = _NOUNS[kind]
    if placement.reference is None or placement.distance is None:
        return f"the {noun}"
    if placement.distance == 0:
        # The atom *is* the reference. A fusion carbon bears no substituent, so "ipso, bearing" —
        # true of a substituted ring — would name a bond that is not there.
        if placement.reference == "the ring fusion":
            return f"the ring-fusion {noun}"
        return f"the {noun} (ipso, bearing {placement.reference})"
    if placement.relation is not None:
        return f"the {placement.relation} {noun} ({placement.relation} to {placement.reference})"
    bonds = "one ring bond" if placement.distance == 1 else f"{placement.distance} ring bonds"
    return f"the {noun} ({bonds} from {placement.reference})"
