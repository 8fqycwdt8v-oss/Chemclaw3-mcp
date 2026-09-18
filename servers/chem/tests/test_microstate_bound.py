"""What `enumerate_microstates` costs, and the bound that is now the reason it cannot run away.

`test_depiction_bound.py` is this file's sibling: it bounds the one tool anybody had noticed was
expensive. This one bounds the tool that turned out to be an order of magnitude worse and had no
bound at all, plus the SMARTS cache the same commit added — and the cache is tested for *agreement*
rather than for speed, because a cache that is fast and wrong is the failure worth catching.

**The defect, measured before the fix.** `MAX_MICROSTATES` bounds the answer and nothing bounded
the work: every ionisable site was shifted, sanitised and canonicalised, and only then was the
species count compared with the cap. Two molecules inside every bound this server had:

    N + CCN*659     1,978 atoms, 660 sites  48,077 ms  then ValueError — nothing returned
    C1 + CNC*659    1,980 atoms, 660 sites  15,647 ms  then answered, with two species

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
"""

from __future__ import annotations

import time

import pytest
from chemclaw_mcp_chem.engine import species
from chemclaw_mcp_chem.engine.admission import PROBE_TIMEOUT_SECONDS
from chemclaw_mcp_chem.engine.species import (
    _ACIDIC,
    _BASIC,
    MAX_IONISABLE_SITES,
    MAX_MICROSTATES,
    _compiled,
    _sites,
    describe_molecule,
    enumerate_microstates,
)
from mcp_server_kit.testing import reimported
from rdkit import Chem

#: What the refusal of an unbounded molecule may cost. Both pathological cases below took 15 s and
#: 48 s before the bound; a second is three orders of magnitude of headroom over what they cost now
#: (33 ms and 135 ms, measured) and still two orders inside what they cost then, so this fails on
#: the bound being removed rather than on a slow runner.
MAX_REFUSAL_SECONDS = 1.0


def distinct_sites(count: int, atoms: int = 1900) -> str:
    """A ~`atoms`-atom chain carrying `count` amines that each give a *different* microstate."""
    segment = "C" * max(1, (atoms - count) // max(count, 1))
    return (segment + "N") * count


def equivalent_sites(count: int) -> str:
    """A macrocycle whose `count` amines are all equivalent, so every microstate collapses to one.

    This is the shape no cap on the *answer* can reach, and the reason the bound is on the input.
    """
    return "C1" + "CNC" * (count - 1) + "CN1"


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
        would be too large", the other is "finding out would cost more than the answer is worth",
        and only the second can honestly name a number it has actually measured.
        """
        with pytest.raises(ValueError) as raised:
            enumerate_microstates(equivalent_sites(660))
        message = str(raised.value)
        assert "660 ionisable sites" in message
        assert "microstates" not in message
        # The remedy has to be something the caller can do; "narrow the molecule" is the other
        # refusal's advice and does not fit a polyelectrolyte.
        assert "fragment" in message

    def test_a_molecule_at_the_bound_is_not_refused_by_the_bound(self) -> None:
        """The last molecule the site bound admits must reach the enumeration.

        At exactly `MAX_IONISABLE_SITES` distinct sites the answer is `1 + MAX_IONISABLE_SITES`
        species, which the *microstate* cap then refuses — so what this asserts is which of the two
        bounds fired, not that the call succeeds. A site bound that was off by one would refuse
        here and the message is what says so.
        """
        with pytest.raises(ValueError) as raised:
            enumerate_microstates(distinct_sites(MAX_IONISABLE_SITES, atoms=200))
        assert "protonation microstates" in str(raised.value)

    def test_the_worst_call_the_bound_admits_stays_inside_the_probe_budget(self) -> None:
        """The bound's cost derivation, driven rather than transcribed.

        The largest molecule that gets past *both* bounds is `MAX_MOLECULE_ATOMS` worth of graph
        carrying one site fewer than the microstate cap. Measured at 580 ms on this container
        against `PROBE_TIMEOUT_SECONDS` of 3; the assertion is the probe budget itself rather than
        that figure, so a slower runner does not red the gate while a bound raised past what this
        server can afford does.
        """
        smiles = distinct_sites(MAX_MICROSTATES - 1)
        started = time.perf_counter()
        found = enumerate_microstates(smiles)
        elapsed = time.perf_counter() - started
        assert found.count == MAX_MICROSTATES
        assert elapsed < PROBE_TIMEOUT_SECONDS, (
            f"the worst call the bounds admit took {elapsed:.2f} s, longer than the readiness "
            f"probe's own timeout of {PROBE_TIMEOUT_SECONDS} s"
        )

    def test_the_free_tool_still_answers_for_a_molecule_the_enumeration_refuses(self) -> None:
        """`describe_topology` is deliberately outside the bound, and that is what makes it usable.

        It is the tool a caller consults to decide whether an enumeration is worth asking for, so
        "this has 660 ionisable sites" is precisely the answer that should reach them. Perceiving
        the sites is 7.4 ms at 1,978 atoms; walking them is what cost 48 s.
        """
        described = describe_molecule(equivalent_sites(660))
        assert described.mobile_proton_sites == 660
        assert described.ionisable_basic_sites == 660

    def test_ordinary_chemistry_is_untouched_by_the_bound(self) -> None:
        """The molecules this tool exists for, with their answers written out.

        A bound is only as good as the evidence that it refuses nothing real, and a site count is
        the kind of quantity that is easy to set an order of magnitude too low.
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
        monkeypatch.delenv("CHEMCLAW_CHEM_MAX_IONISABLE_SITES", raising=False)
        assert reimported(species).MAX_IONISABLE_SITES == 32
        monkeypatch.setenv("CHEMCLAW_CHEM_MAX_IONISABLE_SITES", "4")
        assert reimported(species).MAX_IONISABLE_SITES == 4

    def test_a_bound_that_would_refuse_every_ionisable_molecule_stops_the_import(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`env_bound`'s floor: `0` must be a startup failure, not a pod that serves nothing."""
        monkeypatch.setenv("CHEMCLAW_CHEM_MAX_IONISABLE_SITES", "0")
        with pytest.raises(ValueError, match="CHEMCLAW_CHEM_MAX_IONISABLE_SITES=0"):
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
        """A shared query molecule must not be mutated by matching against it.

        Sharing one compiled pattern across every call is the whole mechanism, and it is only safe
        because `GetSubstructMatches` treats the query as read-only. Nothing asserted that.
        """
        mol = Chem.MolFromSmiles(smiles)
        assert mol is not None
        first = [_sites(mol, _ACIDIC), _sites(mol, _BASIC)]
        for _ in range(3):
            assert [_sites(mol, _ACIDIC), _sites(mol, _BASIC)] == first

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
