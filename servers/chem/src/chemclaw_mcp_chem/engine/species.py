"""The species set a multi-step calculation runs over — enumerated from the graph, never computed.

These produce the sets Chemclaw3's `rank_species` and `survey_bond_strengths` rank, so a model never
invents one (*enumerate, then compute*). They are candidate sets, not predictions; degradants carry
transform names so a chemist can reject one on chemical grounds.

The parent is a member of its own set, first (except for degradants and an underspecified stereo
input), so "the form you gave me is the major one" stays a possible answer. Every bound is a
refusal, never a truncation: a downstream population normalised over a prefix would report a
fraction as a whole.
"""

from __future__ import annotations

from functools import cache
from typing import Literal

from mcp_server_kit.limits import echo, env_bound
from pydantic import BaseModel, Field
from rdkit import Chem
from rdkit.Chem import rdChemReactions, rdMolDescriptors
from rdkit.Chem.EnumerateStereoisomers import EnumerateStereoisomers, StereoEnumerationOptions
from rdkit.Chem.MolStandardize import rdMolStandardize

from chemclaw_mcp_chem.engine.chem import require_molecule

__all__ = [
    "MAX_DEGRADANTS",
    "MAX_DEGRADANT_MATCH_ATOM_PRODUCT",
    "MAX_MICROSTATES",
    "MAX_SITE_ATOM_PRODUCT",
    "MAX_STEREOISOMERS",
    "MAX_STEREO_ISOMER_ATOM_PRODUCT",
    "MAX_TAUTOMERS",
    "MAX_TAUTOMER_HEAVY_ATOMS",
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

# Output caps, each a refusal (see the module docstring). Set where the next step — a conformer
# search per member in `rank_species` — stops being affordable.
MAX_TAUTOMERS = 64
MAX_MICROSTATES = 32
MAX_STEREOISOMERS = 64
MAX_DEGRADANTS = 64

#: How much work `enumerate_microstates` will do before it refuses: `ionisable sites x heavy atoms`.
#:
#: Each site is one shift, sanitise and canonicalisation over the whole graph, so the cost is the
#: product; `MAX_MICROSTATES` only bounds the answer, after the work. Pricing sites alone would
#: refuse cheap symmetric dendrimers and admit expensive long chains. Set at the costliest aliphatic
#: shape measured, about a second of CPU, inside the readiness-probe and request budgets; aromatic
#: sites are dearer per unit and super-linear, so the worst admitted call is about two seconds
#: (`tests/test_enumeration_cost_bounds.py`). Overridable; `env_bound` refuses a value that would
#: refuse everything.
MAX_SITE_ATOM_PRODUCT = env_bound(
    "CHEMCLAW_CHEM_MAX_SITE_ATOM_PRODUCT",
    default=150_000,
    # Glycine is 5 heavy atoms and 2 ionisable sites, so 10 is the product of the smallest
    # ionisable molecule anybody asks about: below it this tool answers for nothing at all.
    minimum=10,
    consequence=(
        "below it even glycine — 5 heavy atoms, 2 ionisable sites — would be refused and "
        "enumerate_protonation_states would answer only for molecules that have no protonation "
        "microstates; far above it one call can hold this pod's interpreter for tens of seconds, "
        "past the request timeout its caller is waiting on"
    ),
)


#: The largest molecule, in heavy atoms, whose tautomers this server enumerates.
#:
#: The enumeration canonicalises up to `MAX_TAUTOMERS + 1` forms over the whole graph, which is
#: super-linear in size, so the output cap bounds nothing about cost. A heavy-atom count rather than
#: a mobile-proton count, because the enumerator's own transforms decide which sites exist (the site
#: perception cannot see a ketone's enol); so a large molecule with no tautomer question is refused
#: too.
MAX_TAUTOMER_HEAVY_ATOMS = env_bound(
    "CHEMCLAW_CHEM_MAX_TAUTOMER_HEAVY_ATOMS",
    default=500,
    # Acetylacetone, the textbook tautomeric case, is 7 heavy atoms.
    minimum=7,
    consequence=(
        "below it even acetylacetone would be refused; far above it one call can hold a worker "
        "thread for tens of seconds, past the request timeout its caller is waiting on"
    ),
)

#: The most `transform matches x heavy atoms` one degradant enumeration may spend.
#:
#: Each match is a product canonicalised over the whole graph, and `MAX_DEGRADANTS` is consulted only
#: after all of it. Matches are counted by substructure search before any product is built.
MAX_DEGRADANT_MATCH_ATOM_PRODUCT = env_bound(
    "CHEMCLAW_CHEM_MAX_DEGRADANT_MATCH_ATOM_PRODUCT",
    default=100_000,
    # A drug-sized parent with one liability — paracetamol is 11 heavy atoms and one amide match.
    minimum=11,
    consequence=(
        "below it even a drug-sized parent with one liability would be refused; far above it one "
        "call can hold a worker thread for tens of seconds, past the request timeout its caller is "
        "waiting on"
    ),
)


#: The most `stereoisomers the enumerator would build x heavy atoms` one stereoisomer enumeration
#: may spend.
#:
#: `maxIsomers` bounds how many isomers are built, not their cost: each is canonicalised over the
#: whole graph. The count is `min(2^n, MAX_STEREOISOMERS + 1)` for `n` open stereo elements, read by
#: the same `FindPotentialStereo` the enumerator uses, before any isomer is built. Priced on that
#: product rather than atoms alone, since a large molecule with no open centre is cheap. The product
#: under-prices long unbranched chains (their canonical ranking is super-linear), accepted so
#: drug-size molecules are not refused by cost. Mostly this makes a certain refusal cheap rather than
#: changing which molecules are answered.
MAX_STEREO_ISOMER_ATOM_PRODUCT = env_bound(
    "CHEMCLAW_CHEM_MAX_STEREO_ISOMER_ATOM_PRODUCT",
    default=6_000,
    # 2-butanol, the textbook single stereocentre, is 5 heavy atoms and 2 isomers.
    minimum=10,
    consequence=(
        "below it even 2-butanol — 5 heavy atoms, one open centre, two isomers — would be refused; "
        "far above it one call can hold a worker thread for tens of seconds, past the request "
        "timeout its caller is waiting on"
    ),
)


class TautomerCostRefused(ValueError):
    """A tautomer enumeration refused for its cost before it ran, not for the size of its answer."""


def _refuse_tautomer_enumeration_past(heavy_atoms: int, smiles: str) -> None:
    """Raise when enumerating tautomers of a molecule this size would cost more than one call may.

    `TautomerCostRefused` is a `ValueError` (reaches the model verbatim) and lets
    `describe_molecule` tell "not computed" from "past the count cap".
    """
    if heavy_atoms > MAX_TAUTOMER_HEAVY_ATOMS:
        raise TautomerCostRefused(
            f"{echo(smiles)!r} has {heavy_atoms} heavy atoms, above the "
            f"{MAX_TAUTOMER_HEAVY_ATOMS} this server enumerates tautomers for: each form is "
            "canonicalised over the whole graph, so "
            "the work grows faster than the molecule and a set this size is not something one call "
            "here may spend. This refuses the cost, not the answer. Ask about the tautomeric unit "
            "on its own (the repeat unit of a polymer, the heterocycle of a larger drug), or raise "
            "CHEMCLAW_CHEM_MAX_TAUTOMER_HEAVY_ATOMS on this deployment."
        )


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
    tautomer_count_computed: bool = Field(
        default=True,
        description=(
            "False when the molecule is too large for this server to enumerate its tautomers at "
            "all, so the count above is null because nobody counted — which says nothing about "
            "whether the molecule is tautomeric. Distinct from `tautomer_count_saturated`, which "
            "means it was counted and is emphatically tautomeric."
        ),
    )


# The three ICH Q1A forced-degradation routes, typed so the transform table is checked against it.
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

    A transform product RDKit cannot sanitise is dropped by the caller, never returned broken.
    """
    return str(Chem.MolToSmiles(mol))


def _ordered_unique(parent: str, found: list[str]) -> list[str]:
    """`found` with the parent first and duplicates removed, order otherwise preserved.

    Enumerator order is meaningful (tautomer scoring, centre ordering); sorting would discard it.
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

    `TautomerEnumerator` strips stereo from a centre it touches, emitting the same compound with and
    without a specification; `rank_species` would count it twice. The first (specified) spelling
    wins.
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

    A `ValueError`, so the remedy (narrow the molecule, assign centres) reaches the model verbatim.
    """
    if count > cap:
        raise ValueError(
            f"{echo(smiles)!r} has {count} {what}, above the limit of {cap}. Returning the "
            f"first {cap} "
            f"would make the set look complete while a ranking normalized populations over a "
            f"fraction of it. Narrow the molecule, or assign the ambiguous centres and ask again."
        )


def _refuse_past_enumeration_cost(sites: int, atoms: int, smiles: str) -> None:
    """Raise when walking a molecule's ionisable sites would cost more than one call may spend.

    Separate from `_refuse_past`: that says the answer is too large; this says finding out costs too
    much, with different remedies. The message names the two factors and the ratio to the bound,
    never seconds, so it is exact for every input. A `ValueError`, so it reaches the model verbatim.
    """
    product = sites * atoms
    if product > MAX_SITE_ATOM_PRODUCT:
        raise ValueError(
            f"{echo(smiles)!r} has {sites} ionisable sites on {atoms} heavy atoms. Each site is "
            "toggled, "
            f"sanitised and canonicalised over the whole graph, so the work is the product of "
            f"those two numbers — {product:,} here, {product / MAX_SITE_ATOM_PRODUCT:.1f}x the "
            f"{MAX_SITE_ATOM_PRODUCT:,} one call on this server may spend. How many microstates "
            f"that would yield is not known and may well be small; this refuses the cost, not the "
            f"answer. Three ways on: describe_topology reports the site count without moving a "
            f"proton, ask about the repeat unit where the sites repeat by symmetry, or raise "
            f"CHEMCLAW_CHEM_MAX_SITE_ATOM_PRODUCT on this deployment."
        )


def enumerate_tautomer_set(smiles: str) -> SpeciesSet:
    """Every tautomer RDKit's enumerator reaches from `smiles`, parent first.

    Raises:
        InvalidSmilesError: `smiles` is not a molecule.
        TautomerCostRefused: more heavy atoms than `MAX_TAUTOMER_HEAVY_ATOMS`.
        ValueError: more tautomers than `MAX_TAUTOMERS`.
    """
    mol = require_molecule(smiles)
    _refuse_tautomer_enumeration_past(mol.GetNumHeavyAtoms(), smiles)
    parent = _canonical(mol)
    enumerator = rdMolStandardize.TautomerEnumerator()
    enumerator.SetMaxTautomers(MAX_TAUTOMERS + 1)
    found = [_canonical(taut) for taut in enumerator.Enumerate(mol)]
    species = _without_erased_twins(_ordered_unique(parent, found))
    _refuse_past(len(species), MAX_TAUTOMERS, "tautomers", smiles)
    return SpeciesSet(smiles=species, count=len(species), parent=parent)


# Acidic and basic sites, as SMARTS over the *neutral* form: what could ionise, not how readily.
# Inclusive by design, since a missing site is a form nobody sees.
#
# `servers/calc`'s `engine/pka.py` perceives sites too, deliberately narrow and frozen because its
# calibration was fitted over it. The two disagree on purpose (an amide N-H is acidic here, never
# basic there); neither may be "corrected" to match the other.
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
    # The guards exclude amide nitrogen, which is not basic at any working pH.
    ("aliphatic amine", "[NX3;H2,H1,H0;!$(N[#6]=[O,N,S]);!$(N[S,P]=O);!$(N#*);!$([N-])]"),
    ("pyridine-type N", "[nX2;$(n1ccccc1),$(n1ccnc1),$(n1cccn1)]"),
    ("amidine/guanidine", "[NX2]=[CX3][NX3]"),
)


@cache
def _compiled(smarts: str) -> Chem.Mol | None:
    """One SMARTS, compiled once per process. `None` for a pattern RDKit will not parse.

    Cached on the string rather than compiled at import, so a bad constant is a per-pattern skip and
    not an import failure; the keys are the literals in `_ACIDIC` and `_BASIC`.

    Sharing a compiled query across threads is safe only because RDKit is built with
    `RDK_BUILD_THREADSAFE_SSS`: a recursive SMARTS caches its match set on the query object, and
    every tool body runs in `asyncio.to_thread`. `tests/test_microstate_bound.py` asserts
    `rdBase._multithreadedEnabled`.
    """
    return Chem.MolFromSmarts(smarts)


def _sites(mol: Chem.Mol, patterns: tuple[tuple[str, str], ...]) -> list[tuple[str, int]]:
    """`(group name, atom index)` for every match, de-duplicated by atom.

    The ionisable atom is `match[0]`, so every pattern starts on it (a carboxylic acid's first
    heteroatom is the carbonyl oxygen). De-duplicated because patterns overlap by design (a
    guanidine matches amidine and amine).
    """
    found: dict[int, str] = {}
    for name, smarts in patterns:
        query = _compiled(smarts)
        if query is None:  # pragma: no cover - a malformed constant would fail every call
            continue
        for match in mol.GetSubstructMatches(query):
            if match and match[0] not in found:
                found[match[0]] = name
    return [(name, index) for index, name in sorted(found.items())]


def _shift(mol: Chem.Mol, index: int, delta: int) -> Chem.Mol | None:
    """`mol` with one proton added to or removed from atom `index`, or None if that is impossible.

    None is an ordinary outcome (a site already deprotonated has no proton to give), not an error.
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

    Singly by design: combined states are reachable by calling this on a result, so a 2^n expansion
    is the caller's explicit decision. The cost (`sites x heavy atoms`, `MAX_SITE_ATOM_PRODUCT`) is
    checked before any proton is moved.

    Raises:
        InvalidSmilesError: `smiles` is not a molecule.
        ValueError: more `sites x heavy atoms` than `MAX_SITE_ATOM_PRODUCT`, or more microstates
            than `MAX_MICROSTATES`.
    """
    mol = require_molecule(smiles)
    parent = _canonical(mol)
    acidic = _sites(mol, _ACIDIC)
    basic = _sites(mol, _BASIC)
    _refuse_past_enumeration_cost(len(acidic) + len(basic), mol.GetNumHeavyAtoms(), smiles)
    species: list[str] = []
    labels: list[str] = []
    for name, index in acidic:
        shifted = _shift(mol, index, -1)
        if shifted is not None:
            species.append(_canonical(shifted))
            labels.append(f"{name} deprotonated")
    for name, index in basic:
        shifted = _shift(mol, index, +1)
        if shifted is not None:
            species.append(_canonical(shifted))
            labels.append(f"{name} protonated")

    ordered = _ordered_unique(parent, species)
    _refuse_past(len(ordered), MAX_MICROSTATES, "protonation microstates", smiles)
    # Labels follow the de-duplicated list, since two sites can produce one structure.
    by_smiles = dict(zip(species, labels, strict=True))
    return SpeciesSet(
        smiles=ordered,
        labels=["as given" if item == parent else by_smiles.get(item, "") for item in ordered],
        count=len(ordered),
        parent=parent,
    )


def _open_stereo_elements(mol: Chem.Mol) -> int:
    """How many stereo elements `EnumerateStereoisomers(onlyUnassigned=True)` would flip.

    Counted with the enumerator's own perception (`FindPotentialStereo`, unspecified and unknown
    centres and double bonds, plus non-absolute stereo groups), so price and work cannot disagree.
    """
    elements = sum(
        1
        for info in Chem.FindPotentialStereo(mol)
        if info.specified in (Chem.StereoSpecified.Unspecified, Chem.StereoSpecified.Unknown)
        and info.type in (Chem.StereoType.Atom_Tetrahedral, Chem.StereoType.Bond_Double)
    )
    groups = sum(
        1
        for group in mol.GetStereoGroups()
        if group.GetGroupType() != Chem.StereoGroupType.STEREO_ABSOLUTE
    )
    return elements + groups


def _refuse_stereo_enumeration_past(mol: Chem.Mol, smiles: str) -> None:
    """Raise when building this molecule's stereoisomers would cost more than one call may.

    Priced as `MAX_STEREO_ISOMER_ATOM_PRODUCT` describes. A `ValueError`, so the wording reaches the
    model.
    """
    elements = _open_stereo_elements(mol)
    # The exponent is clamped first, so a 994-centre polyol never builds 2^994 to compare it.
    built = min(1 << min(elements, MAX_STEREOISOMERS.bit_length()), MAX_STEREOISOMERS + 1)
    atoms = mol.GetNumHeavyAtoms()
    if built * atoms > MAX_STEREO_ISOMER_ATOM_PRODUCT:
        raise ValueError(
            f"{echo(smiles)!r} has {elements} open stereo elements on {atoms} heavy atoms. The "
            f"enumeration would build {built} isomers and canonicalise each over the whole graph, "
            f"so the work is the product of those two numbers — {built * atoms:,} here, "
            f"{built * atoms / MAX_STEREO_ISOMER_ATOM_PRODUCT:.1f}x the "
            f"{MAX_STEREO_ISOMER_ATOM_PRODUCT:,} one call on this server may spend. This refuses "
            "the cost, not the answer. Assign the centres the question does not turn on, ask "
            "about the stereogenic fragment on its own, or raise "
            "CHEMCLAW_CHEM_MAX_STEREO_ISOMER_ATOM_PRODUCT on this deployment."
        )


def enumerate_stereoisomer_set(smiles: str) -> SpeciesSet:
    """Every stereoisomer of `smiles` at its *unassigned* centres, parent first.

    Defined stereochemistry is a claim and is never re-enumerated; only what the input left open is
    expanded.

    Raises:
        InvalidSmilesError: `smiles` is not a molecule.
        ValueError: more `isomers it would build x heavy atoms` than
            `MAX_STEREO_ISOMER_ATOM_PRODUCT`, or more stereoisomers than `MAX_STEREOISOMERS`.
    """
    mol = require_molecule(smiles)
    # Priced before the parent's own canonical SMILES, which on the worst shape is itself half a
    # second: a refusal costs the parse and the count, nothing more.
    _refuse_stereo_enumeration_past(mol, smiles)
    parent = _canonical(mol)
    # The `type: ignore`s here and in `describe_molecule` are `rdkit-stubs` gaps, not claims about
    # the calls. `maxIsomers` (one past the cap) bounds the work; `_refuse_past` below bounds the
    # answer.
    options = StereoEnumerationOptions(  # type: ignore[no-untyped-call]
        onlyUnassigned=True, unique=True, maxIsomers=MAX_STEREOISOMERS + 1
    )
    found = [
        _canonical(isomer)
        for isomer in EnumerateStereoisomers(mol, options=options)  # type: ignore[no-untyped-call]
    ]
    # The parent is prepended only when it is one of the isomers: a structure with open centres is
    # the question, not a member, and `rank_species` would give it a population of its own.
    species = found if parent not in found else _ordered_unique(parent, found)
    if not species:  # a molecule with nothing to expand is its own only isomer
        species = [parent]
    _refuse_past(len(species), MAX_STEREOISOMERS, "stereoisomers", smiles)
    return SpeciesSet(smiles=species, count=len(species), parent=parent)


# Forced-degradation transforms, as reaction SMARTS grouped by the condition that drives them.
# A short, named list (ICH Q1A oxidative, hydrolytic, thermal) so each proposal can be rejected on
# chemical grounds; the set is structural, not a prediction.
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


#: Ceiling on the pricing substructure search, so pricing cannot itself be the expensive step.
_MATCH_COUNT_LIMIT = 100_000


#: One `_TRANSFORMS` row with its SMARTS compiled: condition, transform name, reaction.
_CompiledTransform = tuple[DegradationCondition, str, rdChemReactions.ChemicalReaction]


@cache
def _compiled_transforms() -> tuple[_CompiledTransform, ...]:
    """`_TRANSFORMS`, each reaction SMARTS compiled and initialised once per process.

    Compiling was a large share of a small call. `Initialize()` runs here because `RunReactants`
    would otherwise initialise lazily — a write to a shared object from concurrent worker threads;
    afterwards every call only reads. Recursive reactant templates rely on
    `RDK_BUILD_THREADSAFE_SSS` as `_compiled` does, and `tests/test_species.py` drives the shared
    reactions from threads. An unparsable pattern is dropped (the tests assert all compile).
    """
    compiled: list[_CompiledTransform] = []
    for condition, name, smarts in _TRANSFORMS:
        reaction = rdChemReactions.ReactionFromSmarts(smarts)
        if reaction is None:  # pragma: no cover - a malformed constant would fail every call
            continue
        reaction.Initialize()
        compiled.append((condition, name, reaction))
    return tuple(compiled)


def enumerate_degradant_candidates(smiles: str) -> DegradantSet:
    """Structures a forced-degradation transform reaches from `smiles`.

    Each entry says a transform matches, not that the chemistry happens. The parent is not a member:
    a degradant set is a set of products.

    Raises:
        InvalidSmilesError: `smiles` is not a molecule.
        ValueError: more `transform matches x heavy atoms` than `MAX_DEGRADANT_MATCH_ATOM_PRODUCT`,
            or more candidates than `MAX_DEGRADANTS`.
    """
    mol = require_molecule(smiles)
    reactions = _compiled_transforms()
    matches = sum(
        len(mol.GetSubstructMatches(reaction.GetReactantTemplate(0), maxMatches=_MATCH_COUNT_LIMIT))
        for _, _, reaction in reactions
    )
    atoms = mol.GetNumHeavyAtoms()
    if matches * atoms > MAX_DEGRADANT_MATCH_ATOM_PRODUCT:
        raise ValueError(
            f"{echo(smiles)!r} matches the degradation transforms {matches} times on {atoms} heavy "
            "atoms. Each match is one product sanitised and canonicalised over the whole graph, so "
            f"the work is the product of those two numbers — {matches * atoms:,} here, "
            f"{matches * atoms / MAX_DEGRADANT_MATCH_ATOM_PRODUCT:.1f}x the "
            f"{MAX_DEGRADANT_MATCH_ATOM_PRODUCT:,} one call on this server may spend. This refuses "
            "the cost, not the answer. Ask about the repeat unit where the liabilities repeat, or "
            "raise CHEMCLAW_CHEM_MAX_DEGRADANT_MATCH_ATOM_PRODUCT on this deployment."
        )
    parent = _canonical(mol)
    seen: set[str] = {parent}
    degradants: list[Degradant] = []
    for condition, name, reaction in reactions:
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
    # Sites are perceived once per table and counted three ways. Not bounded by
    # `MAX_SITE_ATOM_PRODUCT`: this is the tool a caller consults before an enumeration, and
    # perceiving sites is cheap where walking them is not. The tautomer count does enumerate; past
    # `MAX_TAUTOMER_HEAVY_ATOMS` it is null with `tautomer_count_computed` false rather than a
    # refusal.
    acidic = _sites(mol, _ACIDIC)
    basic = _sites(mol, _BASIC)
    computed = True
    try:
        tautomers: int | None = len(enumerate_tautomer_set(smiles).smiles)
    except TautomerCostRefused:
        # Too large to count at all: null, and a flag saying nobody counted — not "saturated",
        # which would claim the molecule is emphatically tautomeric.
        tautomers, computed = None, False
    except ValueError:
        # Past the cap the answer is "more than the cap", in two fields, rather than a failure or a
        # count that would read as exact.
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
        ionisable_acidic_sites=len(acidic),
        ionisable_basic_sites=len(basic),
        mobile_proton_sites=len(acidic) + len(basic),
        tautomer_count_saturated=computed and tautomers is None,
        tautomer_count_computed=computed,
        tautomer_count=tautomers,
    )
