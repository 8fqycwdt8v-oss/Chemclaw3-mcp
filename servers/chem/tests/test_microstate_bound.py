"""What `enumerate_microstates` costs, and the bound that is now the reason it cannot run away.

`test_depiction_bound.py` is this file's sibling: it bounds the one tool anybody had noticed was
expensive. This one bounds the tool that turned out to be an order of magnitude worse and had no
bound at all, plus the SMARTS cache the same commit added — and the cache is tested for *agreement*
rather than for speed, because a cache that is fast and wrong is the failure worth catching.

**The defect, measured before the fix.** `MAX_MICROSTATES` bounds the answer and nothing bounded
the work: every ionisable site was shifted, sanitised and canonicalised, and only then was the
species count compared with the cap. Two molecules inside every bound this server had:

    N + CCN*659           1,978 atoms, 660 sites  48,077 ms  then ValueError — nothing returned
    C1 + CNC*659 + CN1    1,980 atoms, 660 sites  15,647 ms  then answered, with two species

The second is the one an output cap can never reach, and it is why the bound had to go on the
**input**: the sites are equivalent, every microstate collapses to the same string, the answer is
comfortably inside `MAX_MICROSTATES`, and the tool still spends 15 s to return two structures. The
first exceeds `connector.yaml`'s own `request_timeout` of 30 s, so the caller has already gone.

**Why no second admission ceiling, which is the other thing `CLAUDE.md` says a slow tool owes.**
Driven rather than argued, on this container. `enumerate_microstates` does hold the GIL — four
concurrent calls of a 1.13 s molecule measured `cpu_util` 0.93x and `wall` 5.15 s, the same
signature `engine/admission.py` records for `Compute2DCoords` — but holding it does **not** starve
the loop the readiness probe is answered on, because the GIL is preemptive at a finer grain than a
call. Measured against a 0.1 s event-loop tick beside bursts of the worst molecule the bound admits:

    n=1  wall 0.67 s   loop lateness p50  12.7 ms  max  24.9 ms
    n=2  wall 1.27 s   loop lateness p50  14.9 ms  max 103.9 ms
    n=4  wall 2.96 s   loop lateness p50  47.6 ms  max 167.5 ms
    n=5  wall 3.67 s   loop lateness p50  53.4 ms  max 580.4 ms
    n=8  wall 5.23 s   loop lateness p50  70.1 ms  max 384.2 ms

and one *unbounded* 14 s call left the tick 46.0 ms late at worst. The probe's `timeoutSeconds` is
3, so nothing here comes near it, and the "N x worst-call" arithmetic that derives
`DEFAULT_MAX_CONCURRENT_RENDERS` is conservative by an order of magnitude for this shape. A ceiling
would also be the wrong instrument: the harm measured above is *one* call burning 48 s of
uncancellable CPU past its caller's timeout, and a ceiling bounds how many run, never how long one
runs. Only an input bound prices that. The open question this leaves — whether the five enumerators
should share one ceiling — is a `docs/BACKLOG.md` row with these numbers in it, not a knob added
here on the strength of a measurement that says it would not bind.

**The first bound written here priced the site count alone, and that is the wrong variable.** Each
site is one `_shift`, one `SanitizeMol` and one `_canonical` over the whole graph, so the cost is
the *product* of the site count and the molecule size — which `servers/chem/README.md` said in the
sentence beside the bound while the bound read one factor of it. Measured on the shipped entry
point, a site-only bound of 32 refused this:

    PAMAM G3 dendrimer      484 heavy atoms,  62 sites    128 ms   ->  6 species   REFUSED
    PAMAM G4 dendrimer      996 heavy atoms, 126 sites    587 ms   ->  7 species   REFUSED

while admitting a 1,891-atom polyamine at 31 sites for 413 ms. PAMAM dendrimers are catalogue
items and their sites are symmetric, so their microstates collapse and the answer is small and
cheap — degeneracy is the **normal** case for a symmetric real molecule, not the pathology the
site-only derivation treated it as. `MAX_SITE_ATOM_PRODUCT` prices the product instead; the class
below is what holds the two directions apart.
"""

from __future__ import annotations

import threading
import time

import pytest
from chemclaw_mcp_chem.engine import species
from chemclaw_mcp_chem.engine.admission import PROBE_TIMEOUT_SECONDS
from chemclaw_mcp_chem.engine.species import (
    _ACIDIC,
    _BASIC,
    MAX_MICROSTATES,
    MAX_SITE_ATOM_PRODUCT,
    _compiled,
    _sites,
    describe_molecule,
    enumerate_microstates,
)
from mcp_server_kit.testing import reimported
from rdkit import Chem, rdBase

#: What the refusal of an unbounded molecule may cost. Both pathological cases below took 15 s and
#: 48 s before the bound; a second is three orders of magnitude of headroom over what they cost now
#: (33 ms and 135 ms, measured) and still two orders inside what they cost then, so this fails on
#: the bound being removed rather than on a slow runner.
MAX_REFUSAL_SECONDS = 1.0


def distinct_sites(count: int, atoms: int = 1900) -> str:
    """A ~`atoms`-atom chain carrying `count` amines that each give a *different* microstate."""
    segment = "C" * max(1, (atoms - count) // max(count, 1))
    return (segment + "N") * count


def aromatic_sites(rings: int) -> str:
    """A poly(pyridine) chain: `rings` basic nitrogens on an aromatic backbone.

    The shape the cost derivation did not price. Every site sits in a ring, so each toggle re-runs
    aromaticity perception over the whole conjugated graph rather than over a chain segment — which
    is why it costs more per site-atom than any aliphatic shape, and why that cost *grows* with the
    product instead of staying flat.
    """
    return "c1ccncc1" * rings


def equivalent_sites(count: int) -> str:
    """A macrocycle whose `count` amines are all equivalent, so every microstate collapses to one.

    This is the shape no cap on the *answer* can reach, and the reason the bound is on the input.
    """
    return "C1" + "CNC" * (count - 1) + "CN1"


def _pamam_arm(generation: int) -> str:
    """One amidoamine branch of a PAMAM dendrimer, branching `generation` times."""
    if generation == 0:
        return "CCC(=O)NCCN"
    inner = _pamam_arm(generation - 1)
    return f"CCC(=O)NCCN({inner}){inner}"


def pamam(generation: int) -> str:
    """An ethylenediamine-core, amine-terminated PAMAM dendrimer, as SMILES.

    A catalogue item rather than a fixture shape: PAMAM G0-G10 are sold by the gram and are routine
    in formulation and delivery work. They are here because they are the molecules the *site-only*
    bound refused — symmetric, so every microstate collapses to a handful of structures, which is
    exactly the case a reader of "660 sites" imagines to be pathological and is not.

    Sites are `2 + 4 x (2^(g+1) - 1)`: 30 at G2, 62 at G3, 126 at G4. G5 is 2,004 heavy atoms and
    `MAX_MOLECULE_ATOMS` refuses it at the parse, so G4 is the largest this server can see at all.
    """
    arm = _pamam_arm(generation)
    return f"N({arm})({arm})CCN({arm}){arm}"


class TestTheSiteBound:
    """The input bound: what it refuses, what it must not refuse, and how fast it refuses."""

    def test_the_refusal_is_reached_without_walking_what_it_refuses(self) -> None:
        """660 distinct sites cost 48,077 ms and then raised; the bound must make that cheap."""
        smiles = "N" + "CCN" * 659
        started = time.perf_counter()
        with pytest.raises(ValueError, match="ionisable sites"):
            enumerate_microstates(smiles)
        elapsed = time.perf_counter() - started
        assert elapsed < MAX_REFUSAL_SECONDS, (
            f"in: a {len(smiles)}-character polyamine with 660 ionisable sites  out: the refusal "
            f"took {elapsed:.2f} s — walking the sites is what the bound exists to avoid paying for"
        )

    def test_a_molecule_whose_sites_collapse_is_refused_by_the_same_bound(self) -> None:
        """The case `MAX_MICROSTATES` cannot reach, because its answer is inside the cap.

        Every one of the 660 amines is equivalent, so the enumeration returns two species — after
        15,647 ms, measured. An output cap never fires here at any value, which is the whole
        argument for bounding the input instead.
        """
        smiles = equivalent_sites(660)
        started = time.perf_counter()
        with pytest.raises(ValueError, match="ionisable sites"):
            enumerate_microstates(smiles)
        assert time.perf_counter() - started < MAX_REFUSAL_SECONDS

    def test_the_refusal_names_the_sites_rather_than_the_species_it_did_not_count(self) -> None:
        """A caller told "660 microstates" about a molecule that yields two is told something false.

        The two refusals in this module are deliberately different sentences: one is "the answer
        would be too large", the other is "finding out would cost more than one call may spend",
        and only the second can honestly name a number it has actually counted. It names both
        factors of the product it priced, and no species count.

        The name is `D-2026-09-18-an-output-cap-is-not-a-bound-on-the-work`'s and stays, because a
        merged record cites it and `tests/test_decision_log.py` is what makes a rename visible. What
        the bound prices changed; what this asserts about the refusal did not.
        """
        with pytest.raises(ValueError) as raised:
            enumerate_microstates(equivalent_sites(660))
        message = str(raised.value)
        assert "660 ionisable sites on 1980 heavy atoms" in message
        assert f"{MAX_SITE_ATOM_PRODUCT:,}" in message
        # The overrun as a factor, derived here rather than transcribed, for the reason
        # `test_the_bound_is_read_from_the_environment_and_is_not_a_constant` gives about the
        # bound itself: a test that restates the expression under test compares it to itself.
        assert f"{660 * 1980 / MAX_SITE_ATOM_PRODUCT:.1f}x" in message

    def test_the_refusal_states_no_cost_it_has_not_timed(self) -> None:
        """The sentence this replaces was false about every molecule the old bound refused.

        It said enumerating would "hold this server for tens of seconds - past the timeout you are
        waiting on - to produce a set this tool would then refuse as too large". Measured on PAMAM
        G3, which that bound refused: 128 ms and six structures. A refusal states the overrun as a
        **factor**, which is exact for every input, needs no wall clock and cannot go stale on a
        faster pod.
        """
        with pytest.raises(ValueError) as raised:
            enumerate_microstates(equivalent_sites(660))
        message = str(raised.value)
        assert "tens of seconds" not in message
        assert "seconds" not in message
        assert "too large" not in message

    def test_the_refusal_names_a_way_forward_the_caller_can_act_on(self) -> None:
        """A fragment is no way forward for a dendrimer, where the question is the whole molecule.

        Where the protonation question *is* about the whole molecule, the only real remedies are a
        different tool and a deployment that allows more — and the old message named the first only
        in `tools.py`'s docstring, never in the `ValueError` the caller actually receives.
        """
        with pytest.raises(ValueError) as raised:
            enumerate_microstates(equivalent_sites(660))
        message = str(raised.value)
        assert "describe_topology" in message
        assert "repeat unit" in message
        assert "CHEMCLAW_CHEM_MAX_SITE_ATOM_PRODUCT" in message

    def test_a_molecule_at_the_bound_is_not_refused_by_the_bound(self) -> None:
        """The last molecule the cost bound admits must reach the enumeration.

        `MAX_MICROSTATES + 1` distinct sites on a molecule small enough to stay under the product
        is refused by the *microstate* cap, not by this one — so what this asserts is which of the
        two bounds fired, not that the call succeeds. A cost bound that was off would refuse here
        and the message is what says so.
        """
        sites = MAX_MICROSTATES + 1
        # 1,500 atoms rather than `MAX_SITE_ATOM_PRODUCT // sites`, which is 4,545 and past the
        # parse bound: the product this admits is wider than the molecules this server accepts.
        atoms = 1500
        assert sites * atoms < MAX_SITE_ATOM_PRODUCT
        with pytest.raises(ValueError) as raised:
            enumerate_microstates(distinct_sites(sites, atoms=atoms))
        assert "protonation microstates" in str(raised.value)

    def test_the_bound_prices_the_work_and_not_the_site_count(self) -> None:
        """The defect this bound replaced, stated as the inequality that makes it a defect.

        A site-only bound is monotone in the site count alone, so it necessarily refuses *some*
        molecule that is cheaper than one it admits. These two are that pair, measured: the
        dendrimer carries twice the sites and costs about a third of the time (128 ms against
        413 ms), because cost is the product and the chain carries 1,891 atoms under each of its 31.
        """
        cheap_but_many_sites = pamam(3)  # 484 atoms, 62 sites, 30,008 product, ~128 ms
        dear_but_few_sites = distinct_sites(31, atoms=1900)  # 1,891 atoms, 58,621 product, ~413 ms
        assert enumerate_microstates(cheap_but_many_sites).count == 6
        # Both are admitted now. What the assertion is about is the *ordering* a site-only bound
        # got backwards: the one with twice the sites is the cheaper call.
        started = time.perf_counter()
        enumerate_microstates(cheap_but_many_sites)
        dendrimer = time.perf_counter() - started
        started = time.perf_counter()
        enumerate_microstates(dear_but_few_sites)
        chain = time.perf_counter() - started
        assert dendrimer < chain, (
            f"the 62-site dendrimer took {dendrimer * 1000:.0f} ms and the 31-site chain "
            f"{chain * 1000:.0f} ms — if that ordering ever inverts, the product is no longer "
            "what this call costs and the bound needs re-deriving"
        )

    @pytest.mark.parametrize(
        ("generation", "atoms", "sites", "species"),
        [(2, 228, 30, 5), (3, 484, 62, 6), (4, 996, 126, 7)],
    )
    def test_a_pamam_dendrimer_is_answered_rather_than_refused(
        self, generation: int, atoms: int, sites: int, species: int
    ) -> None:
        """The regression the site-only bound shipped, on the molecules it took away.

        G3 and G4 both carry more than 32 ionisable sites and were refused; measured on the shipped
        entry point they cost 128 ms and 587 ms and answer with six and seven structures. This is
        the evidence `test_ordinary_chemistry_is_untouched_by_the_bound` claimed to be and was not:
        its three molecules carry 3, 6 and 3 sites, and the bound bit from 33 upwards.
        """
        smiles = pamam(generation)
        mol = Chem.MolFromSmiles(smiles)
        assert mol is not None
        assert mol.GetNumHeavyAtoms() == atoms
        assert len(_sites(mol, _ACIDIC)) + len(_sites(mol, _BASIC)) == sites
        found = enumerate_microstates(smiles)
        assert found.count == species
        assert found.smiles[0] == found.parent

    def test_the_worst_call_the_bound_admits_stays_inside_the_probe_budget(self) -> None:
        """The bound's cost derivation, driven rather than transcribed — on every shape, not three.

        **This drove one shape and named it the worst.** The derivation priced three aliphatic
        molecules — 4.3-4.7 us per site-atom on a dendrimer, 5.6 on a macrocycle, up to 8.4 on a
        long chain — and concluded "the bound prices the worst of the three", which is true and is
        not the same sentence as "the bound prices the worst". Re-measured on this container with a
        fourth ordinary shape, a poly(pyridine) at the same product:

            branched dendrimer        367 at, 123 si,  45,141     197 ms    4.37 us/site-atom
            chain, 100 amines       1,401 at, 100 si, 140,100   1,194 ms    8.52 us/site-atom
            macrocycle, 223 amines    670 at, 223 si, 149,410   1,483 ms    9.92 us/site-atom
            oligopyridine n=158       948 at, 158 si, 149,784   1,986 ms   13.26 us/site-atom

        The three aliphatic figures reproduce the derivation's to within a few percent, which is
        what makes the fourth comparable: it is **1.6x** the shape called the worst, and its cost
        per site-atom *rises* with the product (10.75, 11.97, 13.26 us at n=100, 130, 158), so
        `sites x atoms` under-prices an aromatic molecule by more the closer it gets to the bound.

        Both shapes are driven here, and neither is named the worst — the assertion is the probe
        budget, which is what the bound has to respect. A test that drives the shape its own
        docstring calls worst can only ever confirm that choice.
        """
        sites = 100
        worst = {
            "aliphatic chain": distinct_sites(sites, atoms=MAX_SITE_ATOM_PRODUCT // sites),
            # Six atoms and one site per ring, so `6n x n` is the product and `sqrt(bound / 6)`
            # is the ring count that sits just under it — derived, so raising the bound moves this
            # fixture to the new frontier instead of leaving it at the old one.
            "aromatic backbone": aromatic_sites(int((MAX_SITE_ATOM_PRODUCT / 6) ** 0.5)),
        }
        for shape, smiles in worst.items():
            started = time.perf_counter()
            with pytest.raises(ValueError, match="protonation microstates"):
                enumerate_microstates(smiles)
            elapsed = time.perf_counter() - started
            assert elapsed < PROBE_TIMEOUT_SECONDS, (
                f"the worst call the bounds admit on an {shape} took {elapsed:.2f} s, longer than "
                f"the readiness probe's own timeout of {PROBE_TIMEOUT_SECONDS} s"
            )

    def test_the_topology_tool_is_not_the_cheap_substitute_its_docstring_claimed(self) -> None:
        """`describe_topology` is outside the bound and is **not** free, which it was advertised as.

        It is the right tool to ask first, because it answers for a molecule the enumeration
        refuses. It is not the cheaper call: `tautomer_count` is an enumeration rather than a
        descriptor read, so on PAMAM G3 it measures 839 ms against the enumeration's 128 ms, and on
        G4 2,793 ms against 587 ms. Asserted as an ordering rather than as either number, so a
        slower runner cannot red it and a `describe_molecule` that stopped enumerating can.
        """
        smiles = pamam(3)
        started = time.perf_counter()
        describe_molecule(smiles)
        topology = time.perf_counter() - started
        started = time.perf_counter()
        enumerate_microstates(smiles)
        enumeration = time.perf_counter() - started
        assert topology > enumeration, (
            f"describe_topology took {topology * 1000:.0f} ms and the enumeration it is offered as "
            f"a cheap alternative to took {enumeration * 1000:.0f} ms — the docstrings claiming it "
            "is free are what this guards"
        )

    def test_the_free_tool_still_answers_for_a_molecule_the_enumeration_refuses(self) -> None:
        """`describe_topology` is deliberately outside the bound, and that is what makes it usable.

        It is the tool a caller consults to decide whether an enumeration is worth asking for, so
        "this has 660 ionisable sites" is precisely the answer that should reach them. Perceiving
        the sites is 7.4 ms at 1,978 atoms; walking them is what cost 48 s.

        **"Free" in this name is wrong and the name stays**, because
        `D-2026-09-18-an-output-cap-is-not-a-bound-on-the-work` cites it and
        `tests/test_decision_log.py` is what makes a rename visible. What the tool costs is
        `test_the_topology_tool_is_not_the_cheap_substitute_its_docstring_claimed` above; what
        this asserts — that it answers where the enumeration refuses — is unchanged and is the
        property its callers actually rely on.
        """
        described = describe_molecule(equivalent_sites(660))
        assert described.mobile_proton_sites == 660
        assert described.ionisable_basic_sites == 660

    def test_ordinary_chemistry_is_untouched_by_the_bound(self) -> None:
        """The molecules this tool exists for, with their answers written out.

        A bound is only as good as the evidence that it refuses nothing real. **These three are not
        that evidence on their own and this docstring used to claim they were**: tyrosine, EDTA and
        lysine carry 3, 6 and 3 sites, and the bound they were written under bit from 33 upwards —
        so they could not have seen it refuse a dendrimer at 62. The molecules in the 20-126 site
        range are in `test_a_pamam_dendrimer_is_answered_rather_than_refused`; these hold the
        answers themselves, which that one cannot.
        """
        tyrosine = enumerate_microstates("N[C@@H](Cc1ccc(O)cc1)C(=O)O")
        assert tyrosine.smiles == [
            "N[C@@H](Cc1ccc(O)cc1)C(=O)O",
            "N[C@@H](Cc1ccc([O-])cc1)C(=O)O",
            "N[C@@H](Cc1ccc(O)cc1)C(=O)[O-]",
            "[NH3+][C@@H](Cc1ccc(O)cc1)C(=O)O",
        ]
        assert tyrosine.labels == [
            "as given",
            "phenol deprotonated",
            "carboxylic acid deprotonated",
            "aliphatic amine protonated",
        ]
        # Four carboxylic acids and two tertiary amines — six sites, well inside the bound, and
        # three species after the symmetric acids collapse.
        assert enumerate_microstates("OC(=O)CN(CC(=O)O)CCN(CC(=O)O)CC(=O)O").count == 3
        assert enumerate_microstates("NCCCC[C@H](N)C(=O)O").count == 4

    def test_the_bound_is_read_from_the_environment_and_is_not_a_constant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A deployment can loosen it for a real polyelectrolyte without editing this image.

        Read off the module under two environments rather than re-typed here, for the reason
        `test_admission.py` gives about the ceiling beside it: a test that restates the expression
        under test compares the test to itself.
        """
        monkeypatch.delenv("CHEMCLAW_CHEM_MAX_SITE_ATOM_PRODUCT", raising=False)
        assert reimported(species).MAX_SITE_ATOM_PRODUCT == 150_000
        monkeypatch.setenv("CHEMCLAW_CHEM_MAX_SITE_ATOM_PRODUCT", "400")
        assert reimported(species).MAX_SITE_ATOM_PRODUCT == 400

    def test_a_bound_that_would_refuse_every_ionisable_molecule_stops_the_import(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`env_bound`'s floor: a product below glycine's must be a startup failure.

        Glycine is 5 heavy atoms and 2 ionisable sites, so 10 is the smallest product anybody asks
        about; `0` is still the value an operator reaches for and is still where this is driven.
        """
        monkeypatch.setenv("CHEMCLAW_CHEM_MAX_SITE_ATOM_PRODUCT", "0")
        with pytest.raises(ValueError, match="CHEMCLAW_CHEM_MAX_SITE_ATOM_PRODUCT=0"):
            reimported(species)


#: Molecules the cache is proven *equivalent* over, spanning both tables and their overlaps: an
#: amino acid with three site types, a guanidine that matches the amine and amidine patterns at
#: once, a sulfonamide, an imide, a tetrazole, a thiol, a pyridine and a molecule with nothing.
_AGREEMENT_CORPUS = (
    "N[C@@H](Cc1ccc(O)cc1)C(=O)O",
    "NC(=N)NCCC[C@H](N)C(=O)O",
    "Cc1ccc(S(N)(=O)=O)cc1",
    "O=C1NC(=O)c2ccccc21",
    "c1ccc(-c2nn[nH]n2)cc1",
    "SCCO",
    "c1ccncc1",
    "CCOCC",
    "OC(=O)CN(CC(=O)O)CCN(CC(=O)O)CC(=O)O",
    "OS(=O)(=O)c1ccccc1",
    "OP(=O)(O)c1ccccc1",
)


class TestTheCompiledPatternCache:
    """The SMARTS pre-compile: that it answers the same thing, and that it compiles once."""

    @staticmethod
    def _sites_uncompiled(
        mol: Chem.Mol, patterns: tuple[tuple[str, str], ...]
    ) -> list[tuple[str, int]]:
        """`_sites` as it was before the cache, re-parsing each pattern on every call.

        Written out here rather than imported, because the point is to compare the shipped function
        against an *independent* implementation of the same rule. A mock of `MolFromSmarts` would
        only prove the shipped function calls what the shipped function calls.
        """
        found: dict[int, str] = {}
        for name, smarts in patterns:
            query = Chem.MolFromSmarts(smarts)
            if query is None:
                continue
            for match in mol.GetSubstructMatches(query):
                if match and match[0] not in found:
                    found[match[0]] = name
        return [(name, index) for index, name in sorted(found.items())]

    @pytest.mark.parametrize("smiles", _AGREEMENT_CORPUS)
    def test_the_cache_answers_exactly_what_a_fresh_compile_answers(self, smiles: str) -> None:
        """A cache that is fast and wrong is worse than the per-call parse it replaced."""
        mol = Chem.MolFromSmiles(smiles)
        assert mol is not None
        for table in (_ACIDIC, _BASIC):
            assert _sites(mol, table) == self._sites_uncompiled(mol, table), smiles

    @pytest.mark.parametrize("smiles", _AGREEMENT_CORPUS)
    def test_a_cached_pattern_gives_the_same_matches_on_its_second_use(self, smiles: str) -> None:
        """A shared query must answer the same thing the second time it is used.

        Sharing one compiled pattern across every call is the whole mechanism. The reason first
        written down for why that is safe — "`GetSubstructMatches` treats the query as read-only" —
        is the wrong reason; see the two tests below.
        """
        mol = Chem.MolFromSmiles(smiles)
        assert mol is not None
        first = [_sites(mol, _ACIDIC), _sites(mol, _BASIC)]
        for _ in range(3):
            assert [_sites(mol, _ACIDIC), _sites(mol, _BASIC)] == first

    def test_the_cache_rests_on_a_threadsafe_rdkit_build_and_not_on_an_immutable_query(
        self,
    ) -> None:
        """What actually makes one shared compiled query safe across concurrent calls.

        `_compiled`'s docstring said `GetSubstructMatches` does not mutate the query. Two of the
        eleven patterns are **recursive** SMARTS — the aliphatic-amine pattern with its four
        `!$(...)` guards, and the pyridine-type one — and RDKit caches a recursive query's match set
        against the current target *on the query object*. That is why RDKit has a
        `RDK_BUILD_THREADSAFE_SSS` build flag at all, and it is load-bearing here rather than
        incidental: every tool body runs in `asyncio.to_thread`, so two concurrent calls match
        against one shared query by construction. The cache is what introduced that dependency, and
        nothing asserted it — so a wheel built without the flag would be a silent data race under
        concurrency, not a test failure.
        """
        recursive = [smarts for _, smarts in (*_ACIDIC, *_BASIC) if "$(" in smarts]
        assert recursive, "no recursive pattern left: this guard has lost its subject"
        assert rdBase._multithreadedEnabled, (
            "this RDKit is built without RDK_BUILD_THREADSAFE_SSS, so the per-target match set a "
            f"recursive SMARTS writes onto the shared query ({len(recursive)} of "
            f"{len(_ACIDIC) + len(_BASIC)} patterns here) is unguarded across threads"
        )

    def test_the_shared_queries_answer_correctly_under_concurrency(self) -> None:
        """The build flag, driven rather than read off a module attribute.

        16 threads x 400 matches over the warm shared queries; measured, zero wrong answers.
        """
        expected = {}
        for smiles in _AGREEMENT_CORPUS:
            mol = Chem.MolFromSmiles(smiles)
            assert mol is not None
            expected[smiles] = (_sites(mol, _ACIDIC), _sites(mol, _BASIC))
        wrong: list[str] = []

        def worker(offset: int) -> None:
            for step in range(400):
                smiles = _AGREEMENT_CORPUS[(offset + step) % len(_AGREEMENT_CORPUS)]
                mol = Chem.MolFromSmiles(smiles)
                if (_sites(mol, _ACIDIC), _sites(mol, _BASIC)) != expected[smiles]:
                    wrong.append(smiles)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not wrong, f"{len(wrong)} wrong answers from a shared recursive query under threads"

    def test_every_pattern_in_both_tables_compiles(self) -> None:
        """A `None` here is a site type that silently stops being perceived at all.

        `_sites` skips a pattern RDKit will not parse, which is the right thing at runtime and
        means a typo in either table would cost a whole group of chemistry with nothing raised.
        """
        for name, smarts in (*_ACIDIC, *_BASIC):
            assert _compiled(smarts) is not None, f"{name}: {smarts!r} does not compile"

    def test_each_pattern_is_compiled_once_per_process(self) -> None:
        """The mechanism itself, read off the real cache rather than off a counted mock."""
        _compiled.cache_clear()
        mol = Chem.MolFromSmiles("N[C@@H](Cc1ccc(O)cc1)C(=O)O")
        assert mol is not None
        for _ in range(5):
            _sites(mol, _ACIDIC)
            _sites(mol, _BASIC)
        info = _compiled.cache_info()
        assert info.misses == len(_ACIDIC) + len(_BASIC)
        assert info.currsize == len(_ACIDIC) + len(_BASIC)
        assert info.hits == 4 * (len(_ACIDIC) + len(_BASIC))

    def test_the_topology_tool_perceives_each_table_once(self) -> None:
        """`describe_molecule` read both tables twice for three numbers, two of them a sum.

        Asserted through the cache's own counters, which is the only place the duplication was
        ever visible: with the cache warm the duplicate passes cost matching rather than parsing,
        so nothing else in this server would have gone red if they came back.
        """
        _compiled.cache_clear()
        describe_molecule("N[C@@H](Cc1ccc(O)cc1)C(=O)O")
        info = _compiled.cache_info()
        patterns = len(_ACIDIC) + len(_BASIC)
        assert info.misses == patterns
        # One pass per table for the site counts. `describe_molecule` also enumerates tautomers,
        # which does not consult either table, so anything above this is a duplicate pass.
        assert info.hits == 0, (
            f"{info.hits} cache hits means both tables were perceived more than once for the "
            "three site counts, which is the duplication this asserts is gone"
        )
