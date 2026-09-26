"""The substitution series of a molecule — the regioisomers a "which position" question ranks over.

**What this is for.** Chemclaw3's `rank_species` ranks a *set*, and the enumerators beside this one
produce the set for five kinds of question: which tautomer, which microstate, which stereoisomer,
which bond, which degradant. None produced one for the question a process chemist asks most often
about an aromatic ring — *which position*: "which regioisomer does this nitration give", "what does
moving the methyl do". Without an enumerator the candidate set is whatever compounds a model
happened to write down, which is the thing *enumerate, then compute, and never the reverse* exists
to stop. Chemclaw3's `docs/planning/BACKLOG.md` row "No substitution-product enumerator" is the
request; this module is the primitive, and the template that chains it into `rank_species` is
Chemclaw3's.

**Two questions, one graph operation.** Both put one group on one aromatic C-H and differ only in
where the group comes from:

- `move` — every substituent already on an aromatic carbon, moved one at a time to every other
  aromatic C-H of the same ring system. The products are **isomers of the input**, so the input is
  a member of its own set, first, exactly as a tautomer set carries its parent: "the isomer you drew
  is the most stable one" is the commonest answer and a universe without it cannot say so.
- `add` — a group the caller names, put on each symmetry-distinct aromatic C-H of the input once.
  The products are isomers of *each other* and not of the input (they have one more group), so the
  input is **not** a member — the same rule `enumerate_degradants` follows for the same reason, and
  a ranking that populated over the starting material and its products would be comparing different
  formulas.

**A candidate set, not a prediction, and emphatically not a regioselectivity.** Every entry says a
position *exists*; nothing here says the reaction goes there. And the ranking a caller will chain
this into is a **thermodynamic** one — the relative stability of the finished isomers — while the
regiochemistry of an electrophilic substitution is usually decided **kinetically**, at the sigma
complex, so the most stable isomer and the major product can differ. That caveat belongs in every
answer built on this set, and the tool's docstring says so.

**Aromatic rings only, single substitution only.** An aliphatic ring position creates a stereocentre
per move and a C-H that is not a regiochemistry question in the same sense; a disubstitution is 2^n
in the positions. Both are refused by construction rather than by a cap: the positions are aromatic
carbons carrying a hydrogen, and each product moves or adds one group.

**Priced before it runs.** Each candidate is one product, sanitised and canonicalised over the whole
graph, so the work is `candidates x heavy atoms` — the degradant enumerator's shape, and bounded the
same way: counted from the graph before any product is built, and refused past
`MAX_SUBSTITUTION_CANDIDATE_ATOM_PRODUCT` — and, because two costs of a call grow with the size of
the molecule rather than with the candidates, a molecule past `MAX_SUBSTITUTION_HEAVY_ATOMS` is
refused before it is canonicalised. The output is capped at `MAX_SUBSTITUTIONS` and refused
rather than truncated past it, for the reason `species.py` gives: a partial set is a fraction a
downstream population would report as a whole.
"""

from __future__ import annotations

from typing import Literal

from mcp_server_kit.limits import echo, env_bound
from pydantic import BaseModel, Field
from rdkit import Chem

from chemclaw_mcp_chem.engine.chem import InvalidSmilesError, require_molecule
from chemclaw_mcp_chem.engine.sites import Site, describe_atom_sites

__all__ = [
    "MAX_SUBSTITUTIONS",
    "MAX_SUBSTITUTION_CANDIDATE_ATOM_PRODUCT",
    "MAX_SUBSTITUTION_HEAVY_ATOMS",
    "SubstitutionMode",
    "SubstitutionSet",
    "enumerate_substitution_set",
]

SubstitutionMode = Literal["move", "add"]

#: The most regioisomers one call returns. The same number as the tautomer and stereoisomer caps,
#: for their reason: the next step is a free-energy ranking per member, and 64 is already a ranking
#: somebody should have to ask for deliberately.
MAX_SUBSTITUTIONS = 64

#: The largest molecule, in heavy atoms, whose substitution series this server enumerates.
#:
#: **The product bound below cannot price a large molecule on its own, and this is the half that
#: does.** Two costs of a call do not scale with the candidate count at all: naming the parent's
#: positions (`describe_sites`' whole-molecule pass) and canonicalising a product, both of which
#: are super-linear in the size of the graph. Measured with one methyl on one benzene ring and the
#: rest a chain — ten candidates whatever the size, CPU per call on a host at load average ~150
#: over eight threads, best of two:
#:
#:     toluene + C243 chain      250 atoms   10 candidates    2,500     162 ms  (sites    82 ms)
#:     toluene + C493 chain      500 atoms   10 candidates    5,000     538 ms  (sites   241 ms)
#:     toluene + C993 chain    1,000 atoms   10 candidates   10,000   1,820 ms  (sites   655 ms)
#:     toluene + C1985 chain   1,992 atoms   10 candidates   19,920   4,901 ms  (sites 1,639 ms)
#:
#: so the per-candidate-atom rate climbs from ~65 us to ~250 us across the parse bound's range, and
#: a product bound set for a drug-sized molecule admits the last row. 250 is ~6x the largest drug
#: measured (nilotinib, 39 heavy atoms) and keeps the fixed cost under a fifth of a second; a
#: substitution question about a larger molecule is a question about one of its rings.
MAX_SUBSTITUTION_HEAVY_ATOMS = env_bound(
    "CHEMCLAW_CHEM_MAX_SUBSTITUTION_HEAVY_ATOMS",
    default=250,
    # Toluene, the smallest molecule with a substituent to move, is 7 heavy atoms.
    minimum=7,
    consequence=(
        "below it even toluene would be refused; far above it naming one molecule's positions "
        "and canonicalising its products holds a worker thread for seconds"
    ),
)

#: The most `candidates x heavy atoms` one substitution enumeration may spend.
#:
#: **Each candidate is one product built, sanitised and canonicalised over the whole graph**, so
#: the work is the product of those two numbers and `MAX_SUBSTITUTIONS` is consulted only after all
#: of it — the defect `D-2026-09-18-an-output-cap-is-not-a-bound-on-the-work` fixed for the
#: microstates. The candidates are counted from the ring perception before any product is built:
#: in `move` mode, one per (substituent, aromatic C-H of its ring system) pair plus one per
#: substituent, since each group is named over the graph; in `add` mode, one per aromatic C-H.
#: The frontier, CPU per call on the same loaded host (so read the times as an upper estimate),
#: best of two, with `MAX_SUBSTITUTIONS` lifted so every row builds its whole set:
#:
#:     nilotinib, move                39 atoms    38 candidates    1,482      77 ms
#:     oligopyridine n=16, move       96 atoms   122 candidates   11,712     523 ms
#:     polyphenylene n=16, move       96 atoms   152 candidates   14,592     621 ms
#:     poly(benzyl) n=17, move       118 atoms   162 candidates   19,116     690 ms
#:     oligopyridine n=32, add Br    193 atoms    98 candidates   18,914   1,206 ms (the worst)
#:     oligopyridine n=40, add Br    241 atoms   122 candidates   29,402   1,762 ms refused
#:
#: The heteroaromatic `add` is the dearest shape per candidate-atom (~64 us, against ~36-45 us for
#: the carbocycles), because each product re-perceives aromaticity over every pyridine ring and the
#: fixed cost of naming 193 atoms' positions is a quarter of the call. 20,000 is ~13x the largest
#: drug measured and prices that shape at about a second.
MAX_SUBSTITUTION_CANDIDATE_ATOM_PRODUCT = env_bound(
    "CHEMCLAW_CHEM_MAX_SUBSTITUTION_CANDIDATE_ATOM_PRODUCT",
    default=20_000,
    # Toluene's methyl moved round its ring: one substituent and five C-H on seven heavy atoms, so
    # six candidates on seven atoms. Below it the smallest substitution question is refused.
    minimum=42,
    consequence=(
        "below it even toluene's methyl could not be moved round its own ring; far above it one "
        "call can hold a worker thread for seconds, past the readiness probe beside it"
    ),
)


class SubstitutionSet(BaseModel):
    """A substitution series: regioisomers, positional against `smiles`.

    `smiles` and `labels` are the two fields Chemclaw3's `rank_species` takes (`species` and
    `labels`), in the same order and the same length, so a template passes both by value.
    """

    parent: str = Field(description="The input, canonicalised.")
    mode: SubstitutionMode
    smiles: list[str] = Field(
        description=(
            "The regioisomers, canonical and de-duplicated. In `move` mode the input is first "
            "(it is one of them); in `add` mode it is absent (it has one group fewer)."
        )
    )
    labels: list[str] = Field(
        description=(
            "What each entry is, positional against `smiles`: which group went to which position, "
            "the positions named on the parent the way `describe_sites` names them."
        )
    )
    sites: list[str | None] = Field(
        description=(
            "The `site_id` (as `describe_sites` reports it for `parent`) of the position the group "
            "landed on, positional against `smiles`; null for the input itself."
        )
    )
    groups: list[str] = Field(
        description=(
            "Every group this enumeration moved or added, written with `*` at the atom that bonds "
            "to the ring, e.g. `*C` for methyl, `*OC` for methoxy."
        )
    )
    count: int = Field(description="`len(smiles)`.")


def _refuse(message: str) -> None:
    """A caller-safe refusal. A `ValueError`, so `connector_app` passes the wording through."""
    raise ValueError(message)


def _ring_systems(mol: Chem.Mol) -> list[frozenset[int]]:
    """The fused aromatic ring systems: aromatic rings joined when they share an atom.

    Built from the fully aromatic rings only, so a tetralin's saturated ring does not join its
    benzene ring to anything and its CH2 positions are never targets.
    """
    rings = [
        frozenset(ring)
        for ring in mol.GetRingInfo().AtomRings()
        if all(mol.GetAtomWithIdx(one).GetIsAromatic() for one in ring)
    ]
    systems: list[set[int]] = []
    for ring in rings:
        joined = [system for system in systems if system & ring]
        merged = set(ring).union(*joined)
        systems = [system for system in systems if not system & ring]
        systems.append(merged)
    return [frozenset(system) for system in systems]


def _aromatic_ch(mol: Chem.Mol, system: frozenset[int]) -> list[int]:
    """The aromatic carbons of `system` that carry a hydrogen — the positions a group can take."""
    return sorted(
        index
        for index in system
        if mol.GetAtomWithIdx(index).GetAtomicNum() == 6
        and mol.GetAtomWithIdx(index).GetTotalNumHs() > 0
    )


def _movable(mol: Chem.Mol, systems: list[frozenset[int]]) -> list[tuple[int, int, frozenset[int]]]:
    """`(ring carbon, first group atom, ring system)` for every substituent that can be moved.

    A substituent is what hangs off an aromatic **carbon** by a single, acyclic bond — a bridge, so
    cutting it leaves the group on one side and the ring system on the other. A group on a ring
    nitrogen is not moved: taking it off leaves an N-H, which is a tautomer question and not a
    positional one, and the N-alkylation regiochemistry of an azole is answered by moving the
    *carbon* substituents instead (1-methyl-3-phenylpyrazole's phenyl to C5). A biaryl bond counts
    from both ends, since each ring is a substituent on the other.
    """
    found: list[tuple[int, int, frozenset[int]]] = []
    for system in systems:
        for index in sorted(system):
            atom = mol.GetAtomWithIdx(index)
            if atom.GetAtomicNum() != 6:
                continue
            for bond in atom.GetBonds():
                other = bond.GetOtherAtomIdx(index)
                if (
                    other in system
                    or bond.IsInRing()
                    or bond.GetBondType() != Chem.BondType.SINGLE
                    or mol.GetAtomWithIdx(other).GetAtomicNum() == 1
                ):
                    continue
                found.append((index, other, system))
    return found


def _group_name(mol: Chem.Mol, ring_atom: int, first: int) -> str:
    """The group on the `first` side of the bond to `ring_atom`, with `*` where the ring was."""
    bond = mol.GetBondBetweenAtoms(ring_atom, first)
    pieces = Chem.FragmentOnBonds(mol, [bond.GetIdx()], addDummies=True, dummyLabels=[(0, 0)])
    membership: list[list[int]] = []
    fragments = Chem.GetMolFrags(pieces, asMols=True, fragsMolAtomMapping=membership)
    for fragment, atoms in zip(fragments, membership, strict=True):
        if first in atoms:
            return str(Chem.MolToSmiles(fragment))
    raise ValueError("unreachable: the cut group is always one fragment")  # pragma: no cover


def _group_from_spec(spec: str) -> tuple[Chem.Mol, str]:
    """The caller's group as a molecule with one `*` marking its attachment, and its canonical name.

    Two spellings are accepted, because both are how chemists write a group: `*OC` marks the
    attachment explicitly, and a bare `OC` bonds through its **first** atom (so `OC` is methoxy and
    `CO` is hydroxymethyl — the two differ, and the name returned says which was read).

    Raises:
        InvalidSmilesError: `spec` is not a structure.
        ValueError: more than one `*`, a `*` bonded to more than one atom, or an attachment atom
            with no hydrogen to give up.
    """
    group = require_molecule(spec)
    dummies = [atom for atom in group.GetAtoms() if atom.GetAtomicNum() == 0]
    if len(dummies) > 1:
        _refuse(
            f"the group {echo(spec)!r} has {len(dummies)} attachment points; mark exactly one with "
            "`*`, or none and it bonds through its first atom."
        )
    if dummies:
        if dummies[0].GetDegree() != 1:
            _refuse(f"the `*` in {echo(spec)!r} must be bonded to exactly one atom of the group.")
        return group, str(Chem.MolToSmiles(group))
    first = group.GetAtomWithIdx(0)
    if first.GetTotalNumHs() < 1:
        _refuse(
            f"the group {echo(spec)!r} bonds through its first atom, {first.GetSymbol()}, which "
            "has no hydrogen to give up for the bond. Write the attachment explicitly with `*` "
            "(e.g. `*[N+](=O)[O-]` for nitro) or put the attachment atom first."
        )
    edited = Chem.RWMol(group)
    if first.GetNumExplicitHs() > 0:
        edited.GetAtomWithIdx(0).SetNumExplicitHs(first.GetNumExplicitHs() - 1)
    dummy = edited.AddAtom(Chem.Atom(0))
    edited.AddBond(0, dummy, Chem.BondType.SINGLE)
    marked = edited.GetMol()
    Chem.SanitizeMol(marked)
    return marked, str(Chem.MolToSmiles(marked))


def _sanitised(edited: Chem.RWMol) -> str | None:
    """Canonical SMILES of an edited molecule, or None when RDKit will not sanitise it.

    None rather than raising, as `species._shift` does: a position that cannot carry the group (a
    ring that would no longer kekulise) is that position not applying, not a failed call.
    """
    product = edited.GetMol()
    try:
        Chem.SanitizeMol(product)
    except (Chem.KekulizeException, Chem.AtomValenceException, ValueError):
        return None
    return str(Chem.MolToSmiles(product))


def _moved(mol: Chem.Mol, ring_atom: int, first: int, target: int) -> str | None:
    """`mol` with the group on `first` cut from `ring_atom` and bonded to `target` instead."""
    edited = Chem.RWMol(mol)
    edited.RemoveBond(ring_atom, first)
    edited.AddBond(target, first, Chem.BondType.SINGLE)
    source = edited.GetAtomWithIdx(ring_atom)
    source.SetNumExplicitHs(source.GetNumExplicitHs() + 1)
    landing = edited.GetAtomWithIdx(target)
    if landing.GetNumExplicitHs() > 0:
        landing.SetNumExplicitHs(landing.GetNumExplicitHs() - 1)
    return _sanitised(edited)


def _added(mol: Chem.Mol, group: Chem.Mol, target: int) -> str | None:
    """`mol` with `group` bonded to `target` through the atom its `*` marks."""
    combined = Chem.RWMol(Chem.CombineMols(mol, group))
    offset = mol.GetNumAtoms()
    dummy = next(atom for atom in group.GetAtoms() if atom.GetAtomicNum() == 0)
    attach = dummy.GetNeighbors()[0].GetIdx() + offset
    combined.AddBond(target, attach, Chem.BondType.SINGLE)
    combined.RemoveAtom(dummy.GetIdx() + offset)
    landing = combined.GetAtomWithIdx(target)
    if landing.GetNumExplicitHs() > 0:
        landing.SetNumExplicitHs(landing.GetNumExplicitHs() - 1)
    return _sanitised(combined)


def _refuse_past_cost(candidates: int, atoms: int, smiles: str) -> None:
    """Raise when building every candidate would cost more than one call may spend."""
    work = candidates * atoms
    if work > MAX_SUBSTITUTION_CANDIDATE_ATOM_PRODUCT:
        _refuse(
            f"{echo(smiles)!r} has {candidates} substitution candidates on {atoms} heavy atoms. "
            "Each candidate is one product sanitised and canonicalised over the whole graph, so "
            f"the work is the product of those two numbers — {work:,} here, "
            f"{work / MAX_SUBSTITUTION_CANDIDATE_ATOM_PRODUCT:.1f}x the "
            f"{MAX_SUBSTITUTION_CANDIDATE_ATOM_PRODUCT:,} one call on this server may spend. This "
            "refuses the cost, not the answer. Name the group to move with `substituent`, ask "
            "about the ring on its own, or raise "
            "CHEMCLAW_CHEM_MAX_SUBSTITUTION_CANDIDATE_ATOM_PRODUCT on this deployment."
        )


def _refuse_past_cap(count: int, smiles: str) -> None:
    """Raise when the series is larger than one ranking should be asked to populate."""
    if count > MAX_SUBSTITUTIONS:
        _refuse(
            f"{echo(smiles)!r} has {count} regioisomers in this series, above the limit of "
            f"{MAX_SUBSTITUTIONS}. Returning the first {MAX_SUBSTITUTIONS} would make the set look "
            "complete while a ranking normalized populations over a fraction of it. Name the "
            "group to move with `substituent`, or ask about the ring on its own."
        )


def enumerate_substitution_set(
    smiles: str, substituent: str | None = None, mode: SubstitutionMode = "move"
) -> SubstitutionSet:
    """The substitution series of `smiles` — see the module docstring for the two modes.

    Raises:
        InvalidSmilesError: `smiles` or `substituent` is not a structure.
        ValueError: more heavy atoms than `MAX_SUBSTITUTION_HEAVY_ATOMS`; `add` without a
            `substituent`; a `move` whose `substituent` is not on an
            aromatic carbon of the input; an `add` onto a molecule with no aromatic C-H; more
            `candidates x heavy atoms` than `MAX_SUBSTITUTION_CANDIDATE_ATOM_PRODUCT`; or more
            regioisomers than `MAX_SUBSTITUTIONS`.
    """
    given = require_molecule(smiles)
    atoms = given.GetNumHeavyAtoms()
    # Before the canonicalisation, which is itself one of the super-linear costs this bound prices.
    if atoms > MAX_SUBSTITUTION_HEAVY_ATOMS:
        _refuse(
            f"{echo(smiles)!r} has {atoms} heavy atoms, above the {MAX_SUBSTITUTION_HEAVY_ATOMS} "
            "this server enumerates substitutions for: naming a molecule's positions and "
            "canonicalising each product grow faster than the molecule, so this refuses the cost, "
            "not the answer. Ask about the ring on its own (with its neighbours), or raise "
            "CHEMCLAW_CHEM_MAX_SUBSTITUTION_HEAVY_ATOMS on this deployment."
        )
    parent = str(Chem.MolToSmiles(given))
    # Re-parsed from the canonical form, so every index below is the canonical numbering and the
    # site names `describe_atom_sites` returns for `parent` join on it with no translation.
    mol = require_molecule(parent)
    systems = _ring_systems(mol)

    if mode == "add":
        if substituent is None:
            _refuse(
                "mode='add' needs `substituent`, the group to put on the ring (e.g. `Cl`, "
                "`*[N+](=O)[O-]`); mode='move' moves the groups the molecule already carries."
            )
        return _add(smiles, parent, mol, atoms, systems, substituent or "")
    return _move(smiles, parent, mol, atoms, systems, substituent)


def _sites_by_atom(parent: str) -> dict[int, Site]:
    """Each heavy atom of `parent` mapped to the site `describe_sites` reports it under."""
    return {index: site for site in describe_atom_sites(parent).sites for index in site.atoms}


def _move(
    smiles: str,
    parent: str,
    mol: Chem.Mol,
    atoms: int,
    systems: list[frozenset[int]],
    substituent: str | None,
) -> SubstitutionSet:
    """Every substituent on an aromatic carbon moved, singly, to every other C-H of its system."""
    movable = _movable(mol, systems)
    targets = {system: _aromatic_ch(mol, system) for system in systems}
    candidates = len(movable) + sum(len(targets[system]) for _, _, system in movable)
    _refuse_past_cost(candidates, atoms, smiles)

    names = {
        (ring_atom, first): _group_name(mol, ring_atom, first) for ring_atom, first, _ in movable
    }
    wanted = None
    if substituent is not None:
        _group, wanted = _group_from_spec(substituent)
        if wanted not in names.values():
            present = sorted(set(names.values()))
            _refuse(
                f"{echo(substituent)!r} (read as {wanted}) is not a group on an aromatic carbon of "
                f"{echo(smiles)!r}. The movable groups there are: "
                f"{', '.join(present) if present else 'none'}."
            )

    by_atom = _sites_by_atom(parent)
    found: dict[str, tuple[str, str]] = {}
    for ring_atom, first, system in movable:
        name = names[(ring_atom, first)]
        if wanted is not None and name != wanted:
            continue
        for target in targets[system]:
            product = _moved(mol, ring_atom, first, target)
            if product is None or product == parent or product in found:
                continue
            label = f"{name} moved from {by_atom[ring_atom].label} to {by_atom[target].label}"
            found[product] = (label, by_atom[target].site_id)
    _refuse_past_cap(len(found) + 1, smiles)
    ordered = [parent, *found]
    return SubstitutionSet(
        parent=parent,
        mode="move",
        smiles=ordered,
        labels=["as given", *(label for label, _ in found.values())],
        sites=[None, *(site for _, site in found.values())],
        groups=sorted({names[key] for key in names if wanted is None or names[key] == wanted}),
        count=len(ordered),
    )


def _add(
    smiles: str,
    parent: str,
    mol: Chem.Mol,
    atoms: int,
    systems: list[frozenset[int]],
    substituent: str,
) -> SubstitutionSet:
    """`substituent` put on each symmetry-distinct aromatic C-H of the input, once."""
    group, name = _group_from_spec(substituent)
    positions = sorted({index for system in systems for index in _aromatic_ch(mol, system)})
    if not positions:
        _refuse(
            f"{echo(smiles)!r} has no aromatic C-H, so there is no position to put {name} on. "
            "This enumerator substitutes aromatic rings only."
        )
    _refuse_past_cost(len(positions), atoms + group.GetNumHeavyAtoms(), smiles)

    by_atom = _sites_by_atom(parent)
    found: dict[str, tuple[str, str]] = {}
    tried: set[str] = set()
    for target in positions:
        site = by_atom[target]
        # One product per symmetry class: toluene's two *ortho* carbons give one compound, and
        # building the second only to discard it as a duplicate is work with no answer in it.
        if site.site_id in tried:
            continue
        tried.add(site.site_id)
        product = _added(mol, group, target)
        if product is None or product in found:
            continue
        found[product] = (f"{name} at {site.label}", site.site_id)
    if not found:  # pragma: no cover - every aromatic C-H takes a single-bonded group
        raise InvalidSmilesError(f"{name} could not be bonded to any position of {echo(smiles)!r}")
    _refuse_past_cap(len(found), smiles)
    return SubstitutionSet(
        parent=parent,
        mode="add",
        smiles=list(found),
        labels=[label for label, _ in found.values()],
        sites=[site for _, site in found.values()],
        groups=[name],
        count=len(found),
    )
