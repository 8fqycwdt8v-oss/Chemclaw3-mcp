"""What a chemist calls each atom of a molecule, so a per-atom number can be reported by name.

A per-atom result (`index=4`) is useless, and dangerous if mis-mapped, until a position has a
name. So each site gets a **handle** derived from the molecule rather than atom order, as
`torsion_handle` does for a bond (`D-2026-08-26-a-torsion-is-named-not-indexed`).

A site is a symmetry class, not an atom: toluene's two *ortho* carbons are one site, so a caller
reports their mean and spread rather than a spurious ordering. Symmetry is topological, so
resonance-equivalent atoms (a nitro group's two oxygens) stay separate; `_disambiguate` keeps
their labels distinct. Pure graph work: `read_only`, and askable before a plan is approved.
"""

from __future__ import annotations

import hashlib
from typing import Literal

import rdkit
from pydantic import BaseModel, Field
from rdkit import Chem

from chemclaw_mcp_chem.engine.chem import require_canonical_smiles, require_molecule

__all__ = [
    "SCOPES",
    "Site",
    "SiteKind",
    "SiteScope",
    "SiteSet",
    "describe_atom_sites",
    "site_handle",
]

# What sort of atom this is, in a chemist's words: what a request in words is matched against.
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
    "nitro_oxygen",
    "carbonyl_oxygen",
    "hydroxyl_oxygen",
    "ether_oxygen",
    "thioether_sulfur",
    "thiol_sulfur",
    "halogen",
    "heteroatom",
]

# Which question a site is a candidate answer to, so a caller can ask for the right rows rather
# than more rows.
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

# Each kind's environment, matched atom zero being the site itself, in priority order: the first
# pattern whose atom zero is this atom wins, so specific rows precede general ones.
_KINDS: tuple[tuple[SiteKind, str], ...] = (
    # Electrophilic carbons. The acyl rows are separate so a chemoselectivity answer can name which
    # carbonyl.
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
    # Nitro in the charge-separated form RDKit builds; a pentavalent pattern matches nothing.
    ("nitro_nitrogen", "[NX3+](=[OX1])[OX1-]"),
    ("amide_nitrogen", "[NX3][CX3]=[OX1]"),
    ("aromatic_nitrogen", "[n]"),
    ("amine_nitrogen", "[NX3;!$([NX3]=*)]"),
    # `[OX1]~[N+]` matches both nitro oxygens whichever resonance form RDKit picked.
    ("nitro_oxygen", "[OX1]~[NX3+]"),
    ("carbonyl_oxygen", "[OX1]=[CX3]"),
    ("hydroxyl_oxygen", "[OX2H1]"),
    ("ether_oxygen", "[OX2H0]"),
    ("thiol_sulfur", "[SX2H1]"),
    ("thioether_sulfur", "[SX2H0]"),
    ("halogen", "[F,Cl,Br,I]"),
)

# What each kind is called in a sentence a chemist reads.
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
    "nitro_oxygen": "nitro oxygen",
    "carbonyl_oxygen": "carbonyl oxygen",
    "hydroxyl_oxygen": "hydroxyl oxygen",
    "ether_oxygen": "ether oxygen",
    "thioether_sulfur": "thioether sulfur",
    "thiol_sulfur": "thiol sulfur",
    "halogen": "halogen",
    "heteroatom": "heteroatom",
}

# Classical relationship names by ring bonds from the reference; six-membered rings only.
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


def site_handle(
    mol: Chem.Mol,
    atom_index: int,
    classes: list[int] | None = None,
    written: str | None = None,
) -> str:
    """A content-addressed name for one symmetry class of `mol`.

    Stable under SMILES rewriting (named by canonical symmetry class, not index), shared by
    symmetry-equivalent atoms, and carrying the RDKit version so a handle fails loudly under a
    different build rather than resolving to a different atom.

    Args:
        mol: The molecule the atom belongs to.
        atom_index: Any atom of the class; every member gives the same handle.
        classes: The molecule's canonical symmetry classes, if already computed. Pass them when
            naming many atoms: the whole-molecule passes are the entire cost.
        written: The molecule's canonical SMILES, on the same terms.

    Returns:
        `site_` followed by sixteen hex characters.
    """
    if classes is None:
        classes = list(Chem.CanonicalRankAtoms(mol, breakTies=False))
    if written is None:
        written = str(Chem.MolToSmiles(mol))
    payload = f"{rdkit.__version__}|{written}|{classes[atom_index]}"
    return "site_" + hashlib.sha256(payload.encode()).hexdigest()[:16]


class SiteSet(BaseModel):
    """One molecule's sites, and the molecule they are numbered against.

    **The molecule travels with the indices, and it has to.** The sites are numbered from the
    canonical form — that is the join with every calculator in this family — and the caller
    typically typed something else. Returning a bare list handed back indices into a molecule the
    caller does not possess and cannot derive from this tool's own output: measured on
    2,5-dichloropyridine written `c1cc(Cl)ncc1Cl`, the site reported at atom 4 is an aromatic
    carbon of the canonical `Clc1ccc(Cl)nc1` and the ring **nitrogen** of the string the chemist
    typed. Nothing raises on that — the index is in range — and `render_structure` will draw the
    highlight on the nitrogen, offering the confirmation of a different atom.

    Same shape as `CleavageSet` and `SpeciesSet` for the same reason: the parent is stated once.
    """

    smiles: str = Field(
        description=(
            "The canonical SMILES every `atoms` index below numbers. Pass **this** string, not "
            "the one you typed, to anything that takes those indices — `render_structure`, or a "
            "per-atom calculation."
        )
    )
    sites: list[Site]
    count: int


def describe_atom_sites(smiles: str) -> SiteSet:
    """Every symmetry-distinct heavy atom of `smiles`, named the way a chemist names it.

    Hydrogens are not sites: a C-H question is answered on the carbon, with `hydrogens` carrying the
    indices a calculator puts its numbers on. The molecule is canonicalised first, since every
    calculator embeds the canonical form; indices from the caller's spelling would address different
    atoms. The canonical form is returned beside the sites.

    Raises:
        InvalidSmilesError: `smiles` is not a molecule.
    """
    mol = require_molecule(require_canonical_smiles(smiles))
    with_hydrogens = Chem.AddHs(mol)
    ranks = list(Chem.CanonicalRankAtoms(mol, breakTies=True))
    # The canonical view, computed once for the whole molecule and handed to every handle below.
    classes = list(Chem.CanonicalRankAtoms(mol, breakTies=False))
    written = str(Chem.MolToSmiles(mol))
    matched = {kind: _matched_atoms(mol, pattern) for kind, pattern in _KINDS}
    references = _ring_references(mol, ranks)
    hydrogens = _hydrogen_indices(with_hydrogens)

    by_handle: dict[str, list[int]] = {}
    for atom in mol.GetAtoms():
        # A hydrogen is never a site, including an explicit isotopic `[2H]` that `MolFromSmiles`
        # keeps in the graph; it is reported in its carbon's `hydrogens` instead.
        if atom.GetAtomicNum() == 1:
            continue
        handle = site_handle(mol, atom.GetIdx(), classes, written)
        by_handle.setdefault(handle, []).append(atom.GetIdx())

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
    ordered = _disambiguate(sorted(sites, key=lambda site: site.atoms[0]))
    return SiteSet(smiles=written, sites=ordered, count=len(ordered))


def _disambiguate(sites: list[Site]) -> list[Site]:
    """Make every label unique within the molecule, appending an index only where it has to.

    Callers name sites by `label`, so a collision would be two answers spelled identically. The
    representative atom index is the tiebreaker, appended only to labels that collide.
    """
    counts: dict[str, int] = {}
    for site in sites:
        counts[site.label] = counts.get(site.label, 0) + 1
    return [
        site
        if counts[site.label] == 1
        else site.model_copy(update={"label": f"{site.label} [atom {site.atoms[0]}]"})
        for site in sites
    ]


def _hydrogen_indices(with_hydrogens: Chem.Mol) -> dict[int, list[int]]:
    """Map each heavy atom to the indices its hydrogens carry once hydrogens are explicit.

    Read off the `AddHs` molecule rather than computed from an offset, and covering every hydrogen,
    including an isotopic one written explicitly inside the heavy-atom prefix.
    """
    attached: dict[int, list[int]] = {}
    for atom in with_hydrogens.GetAtoms():
        if atom.GetAtomicNum() == 1 and atom.GetNeighbors():
            attached.setdefault(atom.GetNeighbors()[0].GetIdx(), []).append(atom.GetIdx())
    return attached


def _matched_atoms(mol: Chem.Mol, pattern: str) -> set[int]:
    """The atoms this SMARTS puts in position zero — the site the pattern is about.

    Compiled per call: parsing is a small share of the call, and a shared compiled query would bring
    the `RDK_BUILD_THREADSAFE_SSS` dependency `species.py::_compiled` documents.
    """
    query = Chem.MolFromSmarts(pattern)
    return {match[0] for match in mol.GetSubstructMatches(query)}


def _classify(atom: Chem.Atom, matched: dict[SiteKind, set[int]]) -> SiteKind:
    """Which kind of site this is, in the order the patterns are written.

    Unmatched carbon is aliphatic and anything else a heteroatom — still true, and scopeable.
    """
    for kind, _ in _KINDS:
        if atom.GetIdx() in matched[kind]:
            return kind
    return "aliphatic_carbon" if atom.GetSymbol() == "C" else "heteroatom"


def _scopes(atom: Chem.Atom, kind: SiteKind) -> list[SiteScope]:
    """Which question scopes this site answers; scopes are questions, not a partition."""
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

    The lowest-ranked ring heteroatom (pyridine's C4), else the lowest-ranked substituted ring atom
    (phenol's *para*), else none (every benzene position is the same). Ranked rather than by index,
    so the choice does not depend on how the molecule was written.
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

    Distance is counted through the ring, since a fused system's shortest path can leave it.
    `relation` (*ortho*/*meta*/*para*) is set only where `_classical` allows; otherwise the distance
    and reference name are given.
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

    Only a six-ring whose reference is a substituted carbon or the ring's sole heteroatom
    (pyridine), and never a ring-fusion reference (naphthalene is alpha/beta).
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

    Only neighbours sharing a ring count, so an exocyclic amine is never taken for a ring nitrogen.
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

    A heteroatom as itself ("the ring N"), a substituted carbon by what it carries ("the OH
    substituent"), a fusion carbon as a fusion. An index is appended when the name alone is
    ambiguous (pyrimidine's two nitrogens).
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

    A ring position carries its placement ("the para aromatic carbon (para to the OH substituent)");
    where classical names do not apply, the distance is spelled out.
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
