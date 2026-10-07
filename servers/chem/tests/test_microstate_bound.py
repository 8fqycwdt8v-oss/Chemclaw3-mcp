"""What `enumerate_microstates` costs, and the bound that stops it running away.

`MAX_MICROSTATES` bounds the answer, not the work: every ionisable site is shifted, sanitised and
canonicalised before the count is compared. A molecule of hundreds of equivalent sites collapses
to two species and still costs seconds, so the bound must be on the input.

Each site costs one pass over the whole graph, so cost is the product of site count and molecule
size; `MAX_SITE_ATOM_PRODUCT` prices that product. A site-only bound would refuse cheap symmetric
molecules (PAMAM dendrimers, whose microstates collapse) while admitting costlier chains.

No separate admission ceiling for this tool: holding the GIL here does not starve the event loop
enough to threaten the readiness probe, and a ceiling bounds how many calls run, not how long one
runs. The species tools share the band's ceiling anyway (`engine/admission.py`).

The SMARTS cache is tested for agreement with an uncached implementation, not for speed.
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

#: What the refusal of an unbounded molecule may cost: generous over the refusal's real cost, far
#: under the unbounded enumeration, so it fails on the bound being removed, not on a slow runner.
MAX_REFUSAL_SECONDS = 1.0


def distinct_sites(count: int, atoms: int = 1900) -> str:
    """A ~`atoms`-atom chain carrying `count` amines that each give a *different* microstate."""
    segment = "C" * max(1, (atoms - count) // max(count, 1))
    return (segment + "N") * count


def aromatic_sites(rings: int) -> str:
    """A poly(pyridine) chain: `rings` basic nitrogens on an aromatic backbone.

    Every toggle re-runs aromaticity perception over the conjugated graph, so it costs more per
    site-atom than aliphatic shapes, increasingly so near the bound.
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

    A routine catalogue item whose symmetric sites collapse to a handful of microstates: many sites,
    cheap answer. Sites are `2 + 4 x (2^(g+1) - 1)`; G5 exceeds `MAX_MOLECULE_ATOMS`, so G4 is the
    largest this server can see.
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
        """A molecule whose sites all collapse is refused by the cost bound.

        Its answer is inside `MAX_MICROSTATES`, so an output cap never fires; only an input bound
        can.
        """
        smiles = equivalent_sites(660)
        started = time.perf_counter()
        with pytest.raises(ValueError, match="ionisable sites"):
            enumerate_microstates(smiles)
        assert time.perf_counter() - started < MAX_REFUSAL_SECONDS

    def test_the_refusal_names_the_sites_rather_than_the_species_it_did_not_count(self) -> None:
        """The refusal names the sites rather than a species count it did not compute.

        "Too large an answer" and "too costly to find out" are different sentences; this one names
        both factors of the product it priced. The test name is cited by a merged record and is
        kept.
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
        """The refusal states the overrun as a factor, not an untimed cost.

        A factor is exact for every input and cannot go stale on a faster pod.
        """
        with pytest.raises(ValueError) as raised:
            enumerate_microstates(equivalent_sites(660))
        message = str(raised.value)
        assert "tens of seconds" not in message
        assert "seconds" not in message
        assert "too large" not in message

    def test_the_refusal_names_a_way_forward_the_caller_can_act_on(self) -> None:
        """The refusal names a way forward the caller can act on.

        Where the question is the whole molecule, the remedies are a different tool or a deployment
        allowing more, and the `ValueError` itself must say so.
        """
        with pytest.raises(ValueError) as raised:
            enumerate_microstates(equivalent_sites(660))
        message = str(raised.value)
        assert "describe_topology" in message
        assert "repeat unit" in message
        assert "CHEMCLAW_CHEM_MAX_SITE_ATOM_PRODUCT" in message

    def test_a_molecule_at_the_bound_is_not_refused_by_the_bound(self) -> None:
        """The last molecule the cost bound admits must reach the enumeration.

        Here the microstate cap fires, not the cost bound; the message says which.
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
        """The bound prices the work and not the site count.

        A dendrimer with twice the sites costs less than a long chain, because cost is the product.
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
        """PAMAM G3 and G4 are answered rather than refused.

        Both exceed a site-only limit yet are cheap and return a handful of structures.
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
        """The worst call the bound admits stays inside the probe budget, on several shapes.

        `sites x atoms` under-prices aromatic molecules relative to aliphatic ones, increasingly
        near the bound, so both kinds are driven and neither is named the worst; the assertion is
        the probe budget the bound must respect.
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
        """`describe_topology` is outside the bound and is not cheaper than the enumeration.

        Its `tautomer_count` is itself an enumeration. Asserted as an ordering, so a slower runner
        cannot red it and a `describe_molecule` that stopped enumerating can.
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
        """`describe_topology` answers for a molecule the enumeration refuses.

        It is the tool a caller consults before asking for an enumeration, and perceiving sites is
        cheap where walking them is not. The test name is cited by a merged record and is kept.
        """
        described = describe_molecule(equivalent_sites(660))
        assert described.mobile_proton_sites == 660
        assert described.ionisable_basic_sites == 660

    def test_ordinary_chemistry_is_untouched_by_the_bound(self) -> None:
        """The molecules this tool exists for, with their answers written out.

        These carry few sites; the high-site range is covered by the PAMAM test, while these hold
        the answers themselves.
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
        """A deployment can loosen the bound for a real polyelectrolyte without editing this image.

        Read off the module under two environments rather than re-typed here.
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
        """`_sites` without the cache, re-parsing each pattern on every call.

        An independent implementation to compare against; a mock would only prove the function calls
        what it calls.
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

        Sharing one compiled pattern across calls is the mechanism; why that is safe is the next
        test.
        """
        mol = Chem.MolFromSmiles(smiles)
        assert mol is not None
        first = [_sites(mol, _ACIDIC), _sites(mol, _BASIC)]
        for _ in range(3):
            assert [_sites(mol, _ACIDIC), _sites(mol, _BASIC)] == first

    def test_the_cache_rests_on_a_threadsafe_rdkit_build_and_not_on_an_immutable_query(
        self,
    ) -> None:
        """The cache rests on a thread-safe RDKit build, not on an immutable query.

        Two patterns are recursive SMARTS, and RDKit caches a recursive query's match set on the
        query object. Tool bodies run in `asyncio.to_thread`, so concurrent calls share the query;
        without `RDK_BUILD_THREADSAFE_SSS` that would be a silent data race.
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
        """`describe_molecule` perceives each table once.

        Asserted through the cache's counters, the only place a duplicate pass would be visible.
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
