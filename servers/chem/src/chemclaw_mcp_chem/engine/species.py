"""The species set a multi-step calculation runs over — enumerated from the graph, never computed.

**What these four functions are for.** Chemclaw3's expensive jobs (`rank_species`,
`survey_bond_strengths`) rank a *set* of structures; something has to produce the set. Producing it
from the molecular graph is free and total, and producing it any other way is neither: a model
asked to "list the tautomers" invents plausible strings, and a calculation asked to find them has
to know where to look first. So the split Chemclaw3's own skills state — *enumerate, then compute,
and never the reverse* — needs an enumerator on this side that costs nothing.

**These are candidate sets, not predictions.** Every function here answers "what could this be,
structurally" and none answers "which one is it". A tautomer list says a proton can sit in several
places, not that it does; a degradant list says a transform matches, not that the chemistry
happens. That distinction is the reason `enumerate_degradants` returns transform *names* beside the
structures — a chemist reading "N-oxidation" can reject it for a substrate that will not oxidise,
and cannot reject a bare SMILES.

**The parent is always a member of its own set**, first, and that is not padding. A tautomer
ranking whose universe excludes the input cannot report "the form you gave me is the major one",
which is the answer more often than not; and a downstream `rank_species` populates over exactly the
list it is given. Excluding the parent would silently redefine the question.

**Caps, and why they are refusals rather than truncations.** Stereoisomer enumeration is
2^n — a molecule with 10 unassigned centres is 1024 structures, each of which a caller may then pay
a conformer search for. Every function here bounds its output, and past the bound *raises* rather
than returning a prefix: a truncated set silently redefines "the universe of forms" into "the first
N the algorithm happened to emit", and the population that a downstream ranking normalizes over
would then be a fraction reported as a whole. Chemclaw3's
`D-2026-08-08-a-partial-answer-must-say-so` is the same rule one repository over.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from rdkit import Chem
from rdkit.Chem import rdChemReactions, rdMolDescriptors
from rdkit.Chem.EnumerateStereoisomers import EnumerateStereoisomers, StereoEnumerationOptions
from rdkit.Chem.MolStandardize import rdMolStandardize

from chemclaw_mcp_chem.engine.chem import require_molecule

__all__ = [
    "MAX_DEGRADANTS",
    "MAX_MICROSTATES",
    "MAX_STEREOISOMERS",
    "MAX_TAUTOMERS",
    "Degradant",
    "DegradantSet",
    "SpeciesSet",
    "Topology",
    "describe_molecule",
    "enumerate_degradant_candidates",
    "enumerate_microstates",
    "enumerate_stereoisomer_set",
    "enumerate_tautomer_set",
]

# Each bound is a refusal, not a truncation — see the module docstring. The numbers are the point
# where the *next* step stops being affordable rather than where this one does: enumeration is
# milliseconds at any of these, and `rank_species` at `level="thorough"` is a conformer search per
# member, so 64 tautomers is already a search the caller should have to ask for deliberately.
MAX_TAUTOMERS = 64
MAX_MICROSTATES = 32
MAX_STEREOISOMERS = 64
MAX_DEGRADANTS = 64


class SpeciesSet(BaseModel):
    """A set of related structures, with the parent first.

    `smiles` is the field Chemclaw3's templates pass straight into `rank_species`, by value — see
    `data/templates/tautomer-resolution.yaml`, whose comment records why: *"A tautomer that is not
    here was not ranked, so this reference is what makes the distribution's universe the
    enumeration's universe rather than a guess."*
    """

    smiles: list[str] = Field(
        min_length=1, description="The species, canonical and de-duplicated, parent first."
    )
    labels: list[str] = Field(
        default_factory=list,
        description=(
            "A short name per species, positional against `smiles`. Empty where the enumerator "
            "has nothing to say beyond the structure."
        ),
    )
    count: int = Field(
        description="How many species. `len(smiles)`, carried so a prompt can read "
        "it without counting a list."
    )
    parent: str = Field(description="The input, canonicalised — always `smiles[0]`.")


class Topology(BaseModel):
    """What the molecular graph says about whether a search or an expansion is worth paying for.

    Every field is a count from the graph, and the tool's docstring says what each one implies.
    Deliberately *not* a recommendation: the numbers are stable and a recommendation is a judgement
    that belongs in a skill, where a chemist can disagree with it.
    """

    smiles: str
    atom_count: int
    heavy_atom_count: int
    rotatable_bonds: int
    rings: int
    formal_charge: int
    unassigned_stereocentres: int
    assigned_stereocentres: int
    unassigned_double_bonds: int
    ionisable_acidic_sites: int
    ionisable_basic_sites: int
    mobile_proton_sites: int
    tautomer_count: int | None = Field(
        default=None,
        description=(
            "How many tautomers the enumeration reached, or **null** when there are more than the "
            "cap. Null rather than the cap itself: `64` reported as a count is indistinguishable "
            "from an exact 64, and a reader comparing two molecules on this field would be "
            "comparing a real number with a ceiling. Above 1 — and null is above 1 — resolve the "
            "form before computing anything else about the molecule."
        ),
    )
    tautomer_count_saturated: bool = Field(
        default=False,
        description="True when the count above is null because the enumeration hit its cap.",
    )


# The three routes an ICH Q1A forced-degradation study covers. Named as a type rather than left
# as a bare string so the transform table below is checked against it: a typo in a condition
# would otherwise reach the model as a group nobody can filter on.
DegradationCondition = Literal["oxidative", "hydrolytic", "thermal"]


class Degradant(BaseModel):
    """One proposed degradation product, with the transform that proposed it.

    The transform name is the load-bearing half. A structure alone cannot be argued with; "the
    N-oxidation of this tertiary amine" can be rejected by a chemist who knows the amine is too
    hindered, which is exactly the triage the calling template asks for.
    """

    smiles: str
    transform: str
    condition: DegradationCondition


class DegradantSet(BaseModel):
    """Proposed degradants, grouped by nothing — the caller groups them by `condition`."""

    parent: str
    degradants: list[Degradant]
    count: int


def _canonical(mol: Chem.Mol) -> str:
    """Canonical SMILES for a molecule this module built, with no re-parse.

    Sanitisation is what a transform product needs and a parsed input has already had, so the two
    paths differ and this one is the transform path: a product RDKit cannot sanitise is not a
    structure anyone can calculate on, and is dropped by the caller rather than returned broken.
    """
    return str(Chem.MolToSmiles(mol))


def _ordered_unique(parent: str, found: list[str]) -> list[str]:
    """`found` with the parent first and duplicates removed, order otherwise preserved.

    Order is preserved rather than sorted because every enumerator here emits in an order that
    means something — RDKit's tautomer scoring, the stereo enumerator's centre ordering — and
    sorting would replace it with alphabetical, which means nothing.
    """
    seen = {parent}
    result = [parent]
    for smiles in found:
        if smiles not in seen:
            seen.add(smiles)
            result.append(smiles)
    return result


def _without_erased_twins(species: list[str]) -> list[str]:
    """Drop any member that differs from an earlier one only by an erased stereocentre.

    RDKit's `TautomerEnumerator` strips stereochemistry from a centre the transformation touches,
    so `C[C@H](O)C(C)=O` emits both `CC(=O)[C@H](C)O` and `CC(=O)C(C)O`. Those are two strings and
    one compound-with-and-without-a-specification, and `_ordered_unique` compares strings — so the
    keto form survived twice, and `rank_species`, which populates over exactly the list it is
    given, embedded it twice and split its population against the genuinely different tautomers.

    The *first* spelling wins, which is the specified one: the parent leads the list, so the
    member that carries the claim is kept and the erased twin is what goes. That an alpha centre
    epimerises through the enol is real chemistry — it is just not a second tautomer.
    """
    kept: list[str] = []
    seen_flat: set[str] = set()
    for member in species:
        mol = Chem.MolFromSmiles(member)
        flat = member if mol is None else str(Chem.MolToSmiles(mol, isomericSmiles=False))
        if flat in seen_flat:
            continue
        seen_flat.add(flat)
        kept.append(member)
    return kept


def _refuse_past(count: int, cap: int, what: str, smiles: str) -> None:
    """Raise when an enumeration exceeded its bound, saying what to do instead.

    A `ValueError`, so `connector_app` passes the wording through to the model rather than
    replacing it: the caller can act on this — narrow the molecule, assign the centres by hand —
    and cannot act on "internal error".
    """
    if count > cap:
        raise ValueError(
            f"{smiles!r} has {count} {what}, above the limit of {cap}. Returning the first {cap} "
            f"would make the set look complete while a ranking normalized populations over a "
            f"fraction of it. Narrow the molecule, or assign the ambiguous centres and ask again."
        )


def enumerate_tautomer_set(smiles: str) -> SpeciesSet:
    """Every tautomer RDKit's enumerator reaches from `smiles`, parent first.

    Raises:
        InvalidSmilesError: `smiles` is not a molecule.
        ValueError: more tautomers than `MAX_TAUTOMERS`.
    """
    mol = require_molecule(smiles)
    parent = _canonical(mol)
    enumerator = rdMolStandardize.TautomerEnumerator()
    enumerator.SetMaxTautomers(MAX_TAUTOMERS + 1)
    found = [_canonical(taut) for taut in enumerator.Enumerate(mol)]
    species = _without_erased_twins(_ordered_unique(parent, found))
    _refuse_past(len(species), MAX_TAUTOMERS, "tautomers", smiles)
    return SpeciesSet(smiles=species, count=len(species), parent=parent)


# Acidic and basic sites, as SMARTS over the *neutral* form. Deliberately a short, named list
# rather than a general pKa model: this module enumerates what could ionise, and `predict_pka` on
# the calculation server answers how readily. A site listed here that a chemist would not count is
# a species that ranks near-zero downstream; a site missing here is a form nobody ever sees, which
# is the worse error, so the patterns are inclusive.
_ACIDIC: tuple[tuple[str, str], ...] = (
    ("carboxylic acid", "[OX2H1][CX3]=O"),
    ("sulfonic acid", "[OX2H1][SX4](=O)=O"),
    ("phosphonic acid", "[OX2H1][PX4]=O"),
    ("phenol", "[OX2H1][c]"),
    ("thiol", "[SX2H1][#6]"),
    ("tetrazole", "[nH]1nnnc1"),
    ("sulfonamide N-H", "[NX3H1,NX3H2][SX4](=O)=O"),
    ("imide N-H", "[NX3H1]([CX3]=O)[CX3]=O"),
)
_BASIC: tuple[tuple[str, str], ...] = (
    # Amide nitrogen is excluded by the `!$(...)` guards: it is not basic in any useful sense, and
    # including it produced protonated amides as candidate microstates, which is a form that does
    # not exist at any pH a chemist works at.
    ("aliphatic amine", "[NX3;H2,H1,H0;!$(N[#6]=[O,N,S]);!$(N[S,P]=O);!$(N#*);!$([N-])]"),
    ("pyridine-type N", "[nX2;$(n1ccccc1),$(n1ccnc1),$(n1cccn1)]"),
    ("amidine/guanidine", "[NX2]=[CX3][NX3]"),
)


def _sites(mol: Chem.Mol, patterns: tuple[tuple[str, str], ...]) -> list[tuple[str, int]]:
    """`(group name, atom index)` for every match, de-duplicated by atom.

    **The ionisable atom is `match[0]`, which is why every pattern above is written to start on
    it.** The first version took "the first N, O or S in the match" instead, and that is wrong for
    exactly the group that matters most: in `[CX3](=O)[OX2H1]` the first hetero atom is the
    *carbonyl* oxygen, which carries no proton — so every carboxylic acid was found and then
    silently failed to deprotonate, and beta-alanine came back with its ammonium form and no
    carboxylate. Measured, not reasoned about: the enumeration returned two species where it should
    return three.

    De-duplicated by atom because the patterns overlap by design — a guanidine matches both the
    amidine pattern and the amine one — and counting one nitrogen twice would double the set.
    """
    found: dict[int, str] = {}
    for name, smarts in patterns:
        query = Chem.MolFromSmarts(smarts)
        if query is None:  # pragma: no cover - a malformed constant would fail every call
            continue
        for match in mol.GetSubstructMatches(query):
            if match and match[0] not in found:
                found[match[0]] = name
    return [(name, index) for index, name in sorted(found.items())]


def _shift(mol: Chem.Mol, index: int, delta: int) -> Chem.Mol | None:
    """`mol` with one proton added to or removed from atom `index`, or None if that is impossible.

    Returns None rather than raising, because "this site cannot lose a proton" is an ordinary
    outcome of walking a candidate list and not an error: the acid patterns match on the neutral
    form, and a site already deprotonated in the input has no hydrogen to take.
    """
    edited = Chem.RWMol(mol)
    atom = edited.GetAtomWithIdx(index)
    hydrogens = atom.GetTotalNumHs()
    if delta < 0 and hydrogens < 1:
        return None
    atom.SetNumExplicitHs(max(hydrogens + delta, 0))
    atom.SetFormalCharge(atom.GetFormalCharge() + delta)
    atom.SetNoImplicit(True)
    result = edited.GetMol()
    try:
        Chem.SanitizeMol(result)
    except (Chem.KekulizeException, Chem.AtomValenceException, ValueError):
        return None
    return result


def enumerate_microstates(smiles: str) -> SpeciesSet:
    """The protonation microstates of `smiles`: each ionisable site toggled, singly.

    **Singly, and that bound is the design.** A molecule with 4 ionisable sites has 16 combined
    charge states, most of which are never populated at any pH; the ones a chemist asks about are
    the parent and each single ionisation. Combined states are reachable by calling this on a
    result, which makes the expansion the caller's explicit decision rather than a silent 2^n.

    Raises:
        InvalidSmilesError: `smiles` is not a molecule.
        ValueError: more microstates than `MAX_MICROSTATES`.
    """
    mol = require_molecule(smiles)
    parent = _canonical(mol)
    species: list[str] = []
    labels: list[str] = []
    for name, index in _sites(mol, _ACIDIC):
        shifted = _shift(mol, index, -1)
        if shifted is not None:
            species.append(_canonical(shifted))
            labels.append(f"{name} deprotonated")
    for name, index in _sites(mol, _BASIC):
        shifted = _shift(mol, index, +1)
        if shifted is not None:
            species.append(_canonical(shifted))
            labels.append(f"{name} protonated")

    ordered = _ordered_unique(parent, species)
    _refuse_past(len(ordered), MAX_MICROSTATES, "protonation microstates", smiles)
    # Labels are re-derived against the de-duplicated list so the two stay positional: two sites
    # can produce one structure (a symmetric diacid), and zipping the raw lists would misalign
    # every label after the collision.
    by_smiles = dict(zip(species, labels, strict=True))
    return SpeciesSet(
        smiles=ordered,
        labels=["as given" if item == parent else by_smiles.get(item, "") for item in ordered],
        count=len(ordered),
        parent=parent,
    )


def enumerate_stereoisomer_set(smiles: str) -> SpeciesSet:
    """Every stereoisomer of `smiles` at its *unassigned* centres, parent first.

    **Unassigned only**, which is what makes this answer the question a chemist asks. A structure
    drawn with defined stereochemistry is a claim; re-enumerating over it would silently offer the
    enantiomer of a compound somebody specified. What is expanded is what the input left open.

    Raises:
        InvalidSmilesError: `smiles` is not a molecule.
        ValueError: more stereoisomers than `MAX_STEREOISOMERS`.
    """
    mol = require_molecule(smiles)
    parent = _canonical(mol)
    # Two `type: ignore`s, and one below in `describe_molecule`: all the same `rdkit-stubs` gap
    # that `engine/chem.py` records for `Descriptors.MolWt` — the stub marks these untyped, and
    # the ignore is about the stub rather than a claim about the call.
    # **`maxIsomers` bounds the work, and `_refuse_past` below bounds the answer.** They are not the
    # same bound and only the second used to exist: the enumerator ran unlimited (`maxIsomers=0`)
    # and the whole 2^n set was materialised before the cap was consulted, so refusing a 16-centre
    # molecule — an 82-character string — cost 28 s of GIL-holding CPU in a worker thread nothing
    # can cancel, past this server's own 30 s request budget. One past the cap is what makes the
    # refusal fire on exactly the same molecules as before, at 0.06 s.
    options = StereoEnumerationOptions(  # type: ignore[no-untyped-call]
        onlyUnassigned=True, unique=True, maxIsomers=MAX_STEREOISOMERS + 1
    )
    found = [
        _canonical(isomer)
        for isomer in EnumerateStereoisomers(mol, options=options)  # type: ignore[no-untyped-call]
    ]
    # **The parent is prepended only when it is one of the isomers**, which is the one place the
    # module-wide "parent first" rule does not apply. A structure drawn with its centres left open
    # is not a member of its own stereoisomer set — it is the underspecified question the set
    # answers. Prepending it anyway put an extra species in the list, and since `rank_species`
    # populates over exactly the list it is given, that species would have been embedded, optimised
    # and assigned a Boltzmann population of its own. Measured on `CC(Cl)C(Br)C`: five species for
    # a molecule with two centres.
    species = found if parent not in found else _ordered_unique(parent, found)
    if not species:  # a molecule with nothing to expand is its own only isomer
        species = [parent]
    _refuse_past(len(species), MAX_STEREOISOMERS, "stereoisomers", smiles)
    return SpeciesSet(smiles=species, count=len(species), parent=parent)


# Forced-degradation transforms, as reaction SMARTS grouped by the condition that drives them.
# **A short, named, defensible list rather than a comprehensive one.** ICH Q1A forced degradation
# is oxidative, hydrolytic and thermal; these are the transforms a formulation chemist looks for
# first, and each is named so the proposal can be rejected on chemical grounds. A transform this
# list lacks is a degradant nobody is offered — which is why the calling template's own prompt says
# in as many words that the set is structural and not a prediction.
_TRANSFORMS: tuple[tuple[DegradationCondition, str, str], ...] = (
    ("oxidative", "N-oxidation", "[NX3;H0;!$(N[#6]=[O,N,S]);!$(N=*):1]>>[N+:1][O-]"),
    ("oxidative", "S-oxidation to sulfoxide", "[SX2;$(S([#6])[#6]):1]>>[S:1]=O"),
    ("oxidative", "benzylic hydroxylation", "[CX4;H2;$(Cc):1]>>[C:1]O"),
    ("oxidative", "secondary alcohol to ketone", "[CX4;H1:1][OX2H1:2]>>[C:1]=[O:2]"),
    ("oxidative", "aldehyde to carboxylic acid", "[CX3;H1:1]=[OX1:2]>>[C:1](=[O:2])O"),
    ("hydrolytic", "amide hydrolysis", "[CX3:1](=[OX1:2])[NX3:3]>>[C:1](=[O:2])O.[N:3]"),
    (
        "hydrolytic",
        "ester hydrolysis",
        "[CX3:1](=[OX1:2])[OX2H0:3][#6:4]>>[C:1](=[O:2])O.[O:3][#6:4]",
    ),
    (
        "hydrolytic",
        "carbamate hydrolysis",
        "[NX3:1][CX3:2](=[OX1:3])[OX2:4][#6:5]>>[N:1].[C:2](=[O:3])([O:4])[#6:5]",
    ),
    ("hydrolytic", "nitrile hydration", "[CX2:1]#[NX1:2]>>[C:1](=O)[N:2]"),
    ("thermal", "decarboxylation", "[#6:1][CX3](=[OX1])[OX2H1]>>[#6:1]"),
    (
        "thermal",
        "dehydration of a beta-hydroxy carbonyl",
        "[OX2H1][CX4:1][CX4:2][CX3:3]=[OX1:4]>>[C:1]=[C:2][C:3]=[O:4]",
    ),
)


def enumerate_degradant_candidates(smiles: str) -> DegradantSet:
    """Structures a forced-degradation transform reaches from `smiles`.

    **A short list, not a ranking, and not a prediction.** Each entry says a transform *matches*
    the parent's graph; whether the chemistry happens is what the calling study decides. The parent
    is deliberately **not** a member here — unlike the three enumerators above — because a
    degradant set is a set of *products*, and including the starting material would make "how many
    degradation liabilities does this have" read one too high.

    Raises:
        InvalidSmilesError: `smiles` is not a molecule.
        ValueError: more candidates than `MAX_DEGRADANTS`.
    """
    mol = require_molecule(smiles)
    parent = _canonical(mol)
    seen: set[str] = {parent}
    degradants: list[Degradant] = []
    for condition, name, smarts in _TRANSFORMS:
        reaction = rdChemReactions.ReactionFromSmarts(smarts)
        if reaction is None:  # pragma: no cover - a malformed constant would fail every call
            continue
        for products in reaction.RunReactants((mol,)):
            for product in products:
                try:
                    Chem.SanitizeMol(product)
                except (Chem.KekulizeException, Chem.AtomValenceException, ValueError):
                    # A transform can produce a valence RDKit refuses on an unusual substrate. That
                    # is the transform not applying here, not an error worth failing the call over.
                    continue
                candidate = _canonical(product)
                if candidate in seen:
                    continue
                seen.add(candidate)
                degradants.append(Degradant(smiles=candidate, transform=name, condition=condition))
    _refuse_past(len(degradants), MAX_DEGRADANTS, "degradant candidates", smiles)
    return DegradantSet(parent=parent, degradants=degradants, count=len(degradants))


def describe_molecule(smiles: str) -> Topology:
    """The graph facts that decide whether an expensive search would find anything.

    Raises:
        InvalidSmilesError: `smiles` is not a molecule.
    """
    mol = require_molecule(smiles)
    unassigned = Chem.FindMolChiralCenters(  # type: ignore[no-untyped-call]
        mol, includeUnassigned=True, useLegacyImplementation=False
    )
    assigned = [centre for centre in unassigned if centre[1] != "?"]
    open_centres = [centre for centre in unassigned if centre[1] == "?"]
    open_bonds = sum(
        1
        for bond in mol.GetBonds()
        if bond.GetBondType() == Chem.BondType.DOUBLE
        and bond.GetStereo() == Chem.BondStereo.STEREOANY
    )
    # The tautomer count is the one field here that is not a bare descriptor read, and it is worth
    # the enumeration: "is this molecule tautomeric at all" is the question the calling skill says
    # to ask before paying for a resolution, and no count of heteroatoms answers it.
    try:
        tautomers: int | None = len(enumerate_tautomer_set(smiles).smiles)
    except ValueError:
        # Past the cap is emphatically tautomeric; answering rather than failing keeps this tool
        # free and total, which is the property its callers rely on. But the cap is not a count —
        # a molecule with 195 tautomers reported as having 64 invites a comparison with a molecule
        # that really has 64 — so the answer is "more than the cap" and says so in two fields.
        tautomers = None
    return Topology(
        smiles=_canonical(mol),
        atom_count=mol.GetNumAtoms(onlyExplicit=False),
        heavy_atom_count=mol.GetNumHeavyAtoms(),
        rotatable_bonds=rdMolDescriptors.CalcNumRotatableBonds(mol),
        rings=rdMolDescriptors.CalcNumRings(mol),
        formal_charge=Chem.GetFormalCharge(mol),
        unassigned_stereocentres=len(open_centres),
        assigned_stereocentres=len(assigned),
        unassigned_double_bonds=open_bonds,
        ionisable_acidic_sites=len(_sites(mol, _ACIDIC)),
        ionisable_basic_sites=len(_sites(mol, _BASIC)),
        mobile_proton_sites=len(_sites(mol, _ACIDIC)) + len(_sites(mol, _BASIC)),
        tautomer_count_saturated=tautomers is None,
        tautomer_count=tautomers,
    )
