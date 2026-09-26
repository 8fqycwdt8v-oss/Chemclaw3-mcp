"""What stops a call costing more than anybody agreed to pay: a size bound and a wall clock.

`docs/adding-a-server.md` states what a slow tool owes the fleet — "a bound on its input so the cost
cannot run away unpriced, a `request_timeout` stating the real budget". This server had neither on
the paths its shipped image actually runs, and the two gaps compound:

- **No atom bound.** `compute_hessian` refused above `xtb_hessian_max_atoms`; nothing else did. A
  `Structure` was validated for internal consistency and never for size, so a 33,000-52,000-atom
  `relax_structure` (well inside the 1 MB body cap) reached `make_calculator` with no refusal.
  Measured on the optimizer of the day — an ANC preconditioner, a dense `(3N, 3N)`
  eigendecomposition rebuilt per leg: 3.6 s at 120 atoms, 11.6 s at 240, 32.9 s and 18.7 MB at 510,
  and at 40,000 atoms a 127 GB array that takes the whole uvicorn process down and with it every
  other connected turn. **That preconditioner is gone (geomeTRIC) and the constants moved by about
  fifty times, not by a rounding** — `D-2026-09-18-a-ceiling-is-derived-from-the-pod-it-protects`
  has the tables. The *shape* is unchanged, because geomeTRIC's coordinate system is quadratic in
  the atom count as well and the body cap is linear in it; what changed is that the ceiling is now
  derived from the pod rather than read off a table of sizes, which is what
  `test_a_full_pod_of_calculations_at_the_ceiling_fits_the_memory_limit_it_declares` performs.
- **No wall clock.** `xtb_cli_timeout_seconds` and `crest_timeout_seconds` bound a *subprocess*, and
  `Containerfile` pins `CHEMCLAW_XTB_ENGINE=tblite`, so the shipped image takes the in-process path
  for every `opt` and `hess` and neither timeout applies. `max_steps` bounds iterations, and one
  iteration on a large substrate is unbounded in seconds.

The manifest's `request_timeout: 900` does not close that: it bounds the caller's *wait*. Cancelling
the awaiting coroutine does not stop the worker thread (`CLAUDE.md`, and Chemclaw3's
`D-2026-08-26-a-request-timeout-bounds-the-wait-not-the-work`), so a caller that has given up leaves
the CPU burning — and `cached_compute` is check-then-act, so the retry starts a second identical
burn beside the first. `xtb_cli.run_isolated` already does this correctly for a subprocess by
killing the process group; these tests are the in-process half.
"""

from __future__ import annotations

import time
import types
from pathlib import Path
from typing import Any

import chemclaw_mcp_calc.engine.budget as budget_module
import chemclaw_mcp_calc.engine.xtb_opt as xtb_opt
import numpy as np
import pytest
import yaml
from chemclaw_mcp_calc.engine.config import settings
from chemclaw_mcp_calc.engine.structure import Structure, structure_from_smiles
from chemclaw_mcp_calc.engine.xtb_engine import evaluate_point
from chemclaw_mcp_calc.engine.xtb_hessian import HessianSpec, compute_hessian
from chemclaw_mcp_calc.engine.xtb_opt import OptSpec, optimize_structure
from chemclaw_mcp_calc.engine.xtb_props import PropertiesSpec, compute_properties
from mcp_server_kit.sessions import DEFAULT_MAX_SESSIONS, SESSION_COST_BYTES


def a_structure_of(atom_count: int) -> Structure:
    """A valid `Structure` of exactly `atom_count` atoms, built without RDKit.

    Helium rather than hydrogen: every atom is a closed shell on its own, so any count is a valid
    electron count and the size check is what refuses, not the multiplicity validator.
    """
    return Structure(
        elements=[2] * atom_count,
        positions=[[3.0 * index, 0.0, 0.0] for index in range(atom_count)],
    )


def test_a_structure_larger_than_the_ceiling_is_refused_before_any_engine_sees_it() -> None:
    """The bound is on `Structure`, so every primitive inherits it as the electron count is.

    Put there rather than on each tool for the reason the electron-count check is there: four tools
    take a structure and a fifth will, and a per-tool check is one somebody forgets. The message
    names the count and the limit, so a caller can act on it.
    """
    with pytest.raises(ValueError, match=r"exceeds this server's limit of"):
        a_structure_of(settings.xtb_max_atoms + 1)

    at_the_limit = a_structure_of(settings.xtb_max_atoms)
    assert len(at_the_limit.elements) == settings.xtb_max_atoms


def test_the_ceiling_s_refusal_names_the_way_forward_its_own_docstring_states() -> None:
    """A refusal that leaves the caller nowhere is a refusal they cannot act on.

    `Structure._normalize_and_validate`'s docstring already said what the options are — "a smaller
    system or a deployment configured for a larger one" — and the message named neither, while this
    server's other deliberately worded refusal names three ("run a smaller system, relax it first,
    or raise CHEMCLAW_XTB_INLINE_TIMEOUT_SECONDS"). `connector_app` passes a `ValueError` to the
    model verbatim, so the message is the whole of what the agent and the chemist ever see: the
    docstring is not in the loop.

    The environment variable is asserted by name because it is the only one of the two remedies the
    caller cannot guess.
    """
    with pytest.raises(ValueError) as raised:
        a_structure_of(settings.xtb_max_atoms + 1)
    message = str(raised.value)
    assert "CHEMCLAW_XTB_MAX_ATOMS" in message
    assert "smaller system" in message


#: This server's own Deployment, which is where the memory the ceiling is derived from is declared.
_DEPLOYMENT = Path(__file__).resolve().parents[1] / "deploy" / "deployment.yaml"

#: What one whole in-process relaxation peaks at, as MiB per atom squared.
#:
#: Quadratic and not cubic because geomeTRIC's `DelocalizedInternalCoordinates` eigendecomposes an
#: (`nprim`, `nprim`) G matrix whose `nprim` is linear in the atom count — *time* is the cubic one.
#: Measured out of process, one call per interpreter, absolute peak RSS from `getrusage`, the
#: optimizer stopped after one cycle because the peak is reached in the initial single point, the
#: coordinate-system build and the first gradients rather than after a hundred cycles:
#:
#:   atoms   119     239     509      (linear alkane, the densest primitive set of four shapes)
#:   peak    67.2    253.7   978.9    MiB above the process's own resident set
#:   /N**2   4.74e-3 4.44e-3 3.78e-3
#:
#: The coefficient falls with the atom count — a fixed part of the peak is not quadratic — so the
#: value at the size the ceiling is near is the one that bounds it, not the largest of the three.
PEAK_MIB_PER_ATOM_SQUARED = 3.78e-3

#: What a *finished* calculation does not give back. The admission gate releases a slot the moment
#: the work completes, so the next call is admitted against a count rather than against the pod's
#: resident set — and glibc does not return the arena. Measured over three sequential 239-atom
#: relaxations in one process: baseline 182.4 MiB resident, then 318.5 / 335.3 / 335.0 after each
#: call, with the process peak 430.1 / 461.8 / 461.8. The arena is reused rather than added to, so
#: the steady state costs 6% above a cold call's peak, which is what a slot is charged.
RETAINED_ARENA_FACTOR = 1.06

#: What the server's own process holds before any calculation. Measured on this repository's real
#: `chemclaw_mcp_calc.app` — the served ASGI object and its whole import graph — at 181.1 MiB, and
#: 195.2 MiB after one relaxation of water. Rounded up, because a server that has taken traffic is
#: not the process this was measured in.
SERVER_RESIDENT_MIB = 200

#: The densest primitive set geomeTRIC builds per atom, over the shapes a caller can realistically
#: send. Measured: linear alkane 8.83 at 119 atoms and 8.96 at 509, branched alkane (poly-isobutene)
#: 8.93 at 293, polyphenylalanine 8.47 at 123, polyglycine 7.89 at 122 and 7.97 at 507. The memory
#: coefficient above is measured on the first of those, so this is the guard on it: a geomeTRIC that
#: built a denser set, or a `_coordinate_system` that stopped passing `addcart=True`, would move the
#: peak by the square of this ratio and leave the coefficient describing a library nobody runs.
PRIMITIVES_PER_ATOM = 9.0


def _container_memory_limit_mib() -> int:
    """The `limits.memory` this server's own Deployment declares, in MiB."""
    manifest = yaml.safe_load(_DEPLOYMENT.read_text(encoding="utf-8"))
    containers = manifest["spec"]["template"]["spec"]["containers"]
    declared = str(containers[0]["resources"]["limits"]["memory"])
    assert declared.endswith("Gi"), f"unhandled memory unit in {declared!r}"
    return int(declared[:-2]) * 1024


def _peak_mib_for(atoms: int) -> float:
    """What one admitted slot costs the pod at `atoms` atoms, retention included."""
    return RETAINED_ARENA_FACTOR * PEAK_MIB_PER_ATOM_SQUARED * atoms**2


def test_a_full_pod_of_calculations_at_the_ceiling_fits_the_memory_limit_it_declares() -> None:
    """The ceiling's whole job is that an allocation cannot take the pod down, so it is derived.

    Four numbers decide it and this repository declares every one of them: the container's
    `limits.memory`, `calc_max_concurrent_requests`, `mcp_server_kit`'s own session backlog budget,
    and what one relaxation peaks at. Written as an inequality over those rather than as a number,
    because the failure this replaces is a ceiling whose basis was a table of sizes measured against
    an optimizer that had since been deleted — and a number cannot say which of its inputs moved.

    Measured, the ceiling it replaces did not satisfy this: at 500 atoms a slot costs 1,001 MiB and
    four of them plus the resident set and the session budget is **4,262 MiB against a 4,096 MiB
    limit**. The inequality below puts the bound at 489 atoms; `xtb_max_atoms` is the largest fifty
    under it, because every constant here carries a few percent — the shape of the molecule, the
    allocator, the installed geomeTRIC — and a ceiling set at its own bound is not a bound.

    Raising `xtb_max_atoms` or `calc_max_concurrent_requests`, or lowering the Deployment's memory
    limit, fails here instead of in an OOMKill that takes every other connected turn with it.
    """
    limit = _container_memory_limit_mib()
    sessions_mib = DEFAULT_MAX_SESSIONS * SESSION_COST_BYTES / 1024**2
    concurrent = settings.calc_max_concurrent_requests
    needed = SERVER_RESIDENT_MIB + sessions_mib + concurrent * _peak_mib_for(settings.xtb_max_atoms)
    assert needed <= limit, (
        f"{concurrent} concurrent relaxations at {settings.xtb_max_atoms} atoms need "
        f"{needed:.0f} MiB — the resident set ({SERVER_RESIDENT_MIB} MiB) and the session backlog "
        f"({sessions_mib:.0f} MiB) included — against the {limit} MiB this pod's Deployment "
        "declares. That is an OOMKill, and it takes the four-hour CREST search beside it too"
    )


def test_the_primitive_set_geometric_builds_is_still_what_that_bound_assumes() -> None:
    """The memory coefficient is measured; the primitive count it rests on is re-derived here.

    `PEAK_MIB_PER_ATOM_SQUARED` cannot be re-measured in-process — `getrusage` reports a high-water
    mark the rest of the suite has already raised, and `tracemalloc` sees numpy's allocations but
    not LAPACK's own workspace (measured: 3.0 copies of the G matrix against 6.2 by resident set).
    So the constant is recorded with its measurement and *this* is the guard on it: the peak is a
    multiple of an (`nprim`, `nprim`) matrix, so it moves with the square of the primitive density,
    and a geomeTRIC release that added a primitive family would invalidate the ceiling in silence.

    Driven on the shape the coefficient was measured on, through the same `_coordinate_system` the
    optimizer calls, so a change to its `connect`/`addcart` arguments lands here too.
    """
    structure = structure_from_smiles("C" * 39)
    numbers, positions = structure.arrays()
    molecule = xtb_opt._geometric_molecule(numbers, positions)
    coordinates: Any = xtb_opt._coordinate_system(molecule, ())
    density = len(coordinates.Prims.Internals) / len(numbers)
    assert density <= PRIMITIVES_PER_ATOM, (
        f"geomeTRIC builds {density:.2f} primitives per atom where the atom ceiling was derived "
        f"against {PRIMITIVES_PER_ATOM}. The peak goes as the square of this, so "
        f"`PEAK_MIB_PER_ATOM_SQUARED` needs re-measuring before `xtb_max_atoms` can be believed"
    )


def test_the_hessian_cap_is_the_tighter_bound_inside_the_general_one() -> None:
    """Two bounds, and the tighter one still bites first where it applies.

    A Hessian is 6N + 1 single points, so its ceiling is far below the size at which a structure
    stops being representable at all. The general bound is not a claim that anything under it is
    affordable — the wall clock below is what prices the work.
    """
    assert settings.xtb_hessian_max_atoms < settings.xtb_max_atoms


def test_an_oversized_structure_cannot_be_smuggled_in_as_a_tool_argument() -> None:
    """The refusal has to happen at the boundary, where a payload arrives as JSON.

    `Structure.model_validate` is what every structure-in tool and `calculation_key` run on the
    incoming dict, so a caller cannot construct one over the ceiling by sending it rather than
    building it.
    """
    payload = a_structure_of(settings.xtb_max_atoms).model_dump()
    payload["elements"] = [*payload["elements"], 2]
    payload["positions"] = [*payload["positions"], [1.0, 1.0, 1.0]]
    with pytest.raises(ValueError, match=r"exceeds this server's limit of"):
        Structure.model_validate(payload)


def test_the_optimizer_stops_when_the_inline_budget_is_spent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wall clock on the in-process optimisation, checked where the cost is: per gradient.

    Per SCF rather than per optimizer cycle, because a single cycle is itself unbounded in seconds
    — the step count bounds cycles, not time. Water with a microsecond of budget is the cheapest
    possible proof that the clock is consulted at all; the real budget is
    `CHEMCLAW_XTB_INLINE_TIMEOUT_SECONDS`, which defaults to the manifest's `request_timeout`
    less the caller margin — see
    `test_every_budget_here_is_strictly_tighter_than_the_bound_its_caller_waits`.

    A `ValueError`, so the message reaches the model verbatim: it is the same refusal as the atom
    cap — this is too expensive to run inside a turn — only discovered late.
    """
    monkeypatch.setattr(settings, "xtb_inline_timeout_seconds", 1e-6)
    water = structure_from_smiles("O")
    with pytest.raises(ValueError, match=r"exceeded this server's inline budget"):
        optimize_structure(OptSpec(engine="tblite"), water)


def test_a_relaxation_the_budget_stops_says_how_far_it_got(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal reports the gradients it completed and the gradient it was left at.

    At the 450-atom ceiling a relaxation gets on the order of eleven optimizer cycles before the
    budget stops it (`D-2026-09-18-a-ceiling-is-derived-from-the-pod-it-protects` measured the
    per-cycle cost), so "exceeded the budget" alone
    cannot tell a run one cycle from converging from one nowhere near — and the refusal one atom
    above the ceiling says this server does not start what it will abandon. Reporting how far it
    got is what makes an abandoned relaxation visible rather than silent.

    Driven rather than asserted on a string: the budget's clock is replaced so the *second* gradient
    past the input is refused, and the figures in the message are checked against the gradient
    this relaxation actually evaluated last — so a message reporting the input's gradient, or a
    stale count, fails here.
    """
    # A water with one bond stretched, so the optimizer certainly runs past its first gradient.
    strained = Structure(
        elements=[8, 1, 1],
        positions=[[0.0, 0.0, 0.0], [1.3, 0.0, 0.0], [-0.3, 0.9, 0.0]],
    )
    real_clock = time.monotonic
    checks = {"count": 0}

    def clock() -> float:
        # `Deadline.check` reads `elapsed` once to decide; the first decision passes and every
        # later one finds the budget spent.
        checks["count"] += 1
        return real_clock() + (0.0 if checks["count"] <= 1 else 1e9)

    evaluated: list[np.ndarray] = []

    def spy(calculator: Any, positions: np.ndarray) -> Any:
        answer = evaluate_point(calculator, positions)
        evaluated.append(np.asarray(answer[1], dtype=float))
        return answer

    monkeypatch.setattr(budget_module, "time", types.SimpleNamespace(monotonic=clock))
    monkeypatch.setattr(xtb_opt, "evaluate_point", spy)
    spec = OptSpec(engine="tblite")
    with pytest.raises(ValueError, match=r"exceeded this server's inline budget") as refused:
        optimize_structure(spec, strained)

    message = str(refused.value)
    assert len(evaluated) == 2, f"expected the input and one gradient past it, got {len(evaluated)}"
    assert "stopped after 1 gradient evaluation past the input geometry" in message, message
    reached = float(np.max(np.abs(evaluated[-1])))
    assert reached > spec.gradient_tolerance, "the relaxation had converged, so this proves nothing"
    assert f"max |gradient| {reached:.2e} Hartree/Angstrom" in message, message
    assert f"against the {spec.gradient_tolerance:.2e} it had to reach" in message, message


def test_the_finite_difference_hessian_stops_when_the_inline_budget_is_spent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same clock on the other loop, which is the more expensive one.

    6N + 1 single points at the 150-atom cap is 901 SCFs — scaling this repository's own measurement
    of 76 atoms in 218 s, about 25 minutes. The caller gives up at 900 s and the thread keeps
    going, which is why the budget that stops it has to expire first.
    """
    monkeypatch.setattr(settings, "xtb_inline_timeout_seconds", 1e-6)
    water = structure_from_smiles("O")
    with pytest.raises(ValueError, match=r"exceeded this server's inline budget"):
        compute_hessian(HessianSpec(engine="tblite"), water)


#: What Chemclaw3 waits for each tier of this server, from `core/config/calculators.py`. Written as
#: literals rather than imported: that repository is not installed here, and transcribing the number
#: the *caller* actually sends is the whole point — a constant imported from this side would agree
#: with itself and say nothing about the pair. `tests/test_identity_contract.py` makes the same
#: argument for the header spellings, for the same reason.
CHEMCLAW3_CALLER_BUDGETS = {
    "calc_server_timeout_seconds": 900.0,
    "calc_atomic_timeout_seconds": 3600.0,
    "calc_sampling_timeout_seconds": 14400.0,
}


def test_every_budget_here_is_strictly_tighter_than_the_bound_its_caller_waits() -> None:
    """The refusal must be the answer that arrives, and equality guarantees it is not.

    All three of this server's budgets shipped **equal** to the Chemclaw3 setting matched to them —
    900/900, 3600/3600, 14400/14400. Equal is not tighter: the caller's clock starts when it sends
    the request and this server's starts after connect, handshake, JSON decode, structure embedding
    and admission, so the caller always expired first. Every deliberately worded refusal on this
    server — `budget.Deadline.check`'s "run a smaller system, relax it first, or raise
    CHEMCLAW_XTB_INLINE_TIMEOUT_SECONDS", and the process-group kill behind the two subprocess
    budgets — was therefore unreachable in production, and the chemist got a transport timeout
    instead. The pod, meanwhile, kept computing for a request nobody was waiting for.

    Checked as a strict inequality with a stated minimum margin rather than as three numbers,
    because the numbers are a deployment's to change and the *ordering* is not.
    """
    margin = 120  # `config._CALLER_MARGIN_SECONDS`, and its derivation is in that comment.
    for name, budget in (
        ("calc_server_timeout_seconds", settings.xtb_inline_timeout_seconds),
        ("calc_atomic_timeout_seconds", float(settings.xtb_cli_timeout_seconds)),
        ("calc_sampling_timeout_seconds", float(settings.crest_timeout_seconds)),
    ):
        caller = CHEMCLAW3_CALLER_BUDGETS[name]
        assert budget <= caller - margin, (
            f"this server allows {budget:g} s where Chemclaw3's {name} waits {caller:g} s. The "
            f"caller's clock starts first, so anything above {caller - margin:g} s means its "
            "timeout wins and this server's refusal never reaches the chemist"
        )


def test_the_margin_covers_one_uninterruptible_single_point() -> None:
    """The binding term in that margin is granularity, not transport.

    `budget.Deadline` is checked *between* units of work and never inside one, because a single
    point is not interruptible — so a spent budget is noticed at most one single point late. One
    single point measured **81 s** here at 493 atoms (53 atoms 0.20 s, 153 atoms 2.43 s, 303 atoms
    19.8 s, 453 atoms 62.7 s, 493 atoms 81.1 s). A margin under that is a margin the overrun eats,
    and the caller expires again.

    Asserted against `xtb_max_atoms` rather than against a bare number so that raising the atom
    ceiling — which raises the worst-case single point superlinearly — fails here instead of
    quietly reintroducing the defect. **As an inequality rather than an equality**, because the
    cost of a single point rises monotonically with the atom count: a figure measured at 493 bounds
    every ceiling at or below it, so lowering the ceiling owes no new measurement and raising it
    past that size owes one. The equality this replaces failed on the *safe* direction, which is
    how a ratchet teaches people to edit it
    (`D-2026-09-18-a-ceiling-is-derived-from-the-pod-it-protects`).

    The optimizer gained a second uninterruptible unit with geomeTRIC — the coordinate-system build,
    which runs between the initial single point and the first `Deadline` check — and it is the
    smaller one at every size measured (0.52 s against 0.68 s at 119 atoms, 3.64 against 4.16 at
    239, 28.9 against 41.9 at 509), so the granularity is still one single point.
    """
    worst_single_point_seconds = 81.0
    measured_at_atoms = 493
    assert settings.xtb_max_atoms <= measured_at_atoms, (
        f"the {worst_single_point_seconds:g} s figure is one single point at {measured_at_atoms} "
        "atoms; a ceiling above that needs a re-measurement, not a re-reading of this comment"
    )
    margin = CHEMCLAW3_CALLER_BUDGETS["calc_server_timeout_seconds"] - (
        settings.xtb_inline_timeout_seconds
    )
    assert margin >= worst_single_point_seconds


def test_the_budget_does_not_reach_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refusal is not a result, so the clock is a setting rather than a spec field.

    The distinction is the whole of `test_key_covers_every_knob.py` read in the other direction:
    `xtb_opt_trust_radius` changes the number that comes back and therefore belongs in the key,
    while a budget only decides whether one comes back at all. Keying on it would fork the cache
    every time a deployment gave itself more time.
    """
    water = structure_from_smiles("O")
    monkeypatch.setattr(settings, "xtb_inline_timeout_seconds", 900.0)
    before = PropertiesSpec().cache_key(water)
    monkeypatch.setattr(settings, "xtb_inline_timeout_seconds", 60.0)
    assert PropertiesSpec().cache_key(water).as_str() == before.as_str()


def test_a_single_point_under_the_ceiling_still_runs() -> None:
    """The bounds refuse the unaffordable and nothing else — asserted, so a typo cannot pass.

    A ceiling written the wrong way round, or a budget in the wrong unit, would turn every
    calculation on this server into a refusal, and every test that only asserts a refusal would
    still be green.
    """
    result = compute_properties(PropertiesSpec(), structure_from_smiles("O"))
    assert result.total_energy_hartree < 0


def test_an_exceeded_inline_budget_says_which_loop_spent_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare counter cannot tell an undersized Hessian budget from an undersized optimisation one.

    `chemclaw_mcp_calc_inline_budget_exceeded_total` had no labels at all — while
    `engine/metrics.py`'s own header said "every label here is a binary name", which was false in
    the direction that hid the gap. The two loops behind it scale with different things: a Hessian
    is 6N + 1 single points and grows with the molecule, an optimisation grows with the surface, so
    an operator seeing the counter rise has two different decisions to make and the metric said
    nothing about which. `Deadline.check`'s `what` is a literal written at the call site, so it is
    bounded by this repository rather than by a caller — which is the condition an unauthenticated
    `/metrics` puts on any label.

    Both loops are driven, because one series proves the label exists and two prove it separates.
    """
    from chemclaw_mcp_calc.engine.metrics import INLINE_BUDGET_EXCEEDED

    before = {
        what: INLINE_BUDGET_EXCEEDED.labels(what)._value.get()
        for what in ("geometry optimization", "Hessian")
    }
    monkeypatch.setattr(settings, "xtb_inline_timeout_seconds", 1e-6)
    water = structure_from_smiles("O")
    with pytest.raises(ValueError):
        optimize_structure(OptSpec(engine="tblite"), water)
    with pytest.raises(ValueError):
        compute_hessian(HessianSpec(engine="tblite"), water)

    for what, was in before.items():
        assert INLINE_BUDGET_EXCEEDED.labels(what)._value.get() == was + 1, (
            f"the inline-budget counter did not separate {what!r}; an operator cannot tell which "
            "budget is undersized"
        )


def test_the_body_cap_admits_far_more_atoms_than_the_ceiling_at_every_formatting() -> None:
    """Why `Structure`'s atom ceiling exists at all, held as a range rather than as a figure.

    The argument the ceiling rests on is that the transport bound above it does not bound this: a
    `tools/call` body under `DEFAULT_MAX_REQUEST_BYTES` still carries orders of magnitude more atoms
    than the optimizer can afford. Three documents stated that as a single number — "37,983 atoms,
    measured at 26.3 bytes an atom" — which does not reconcile with its own coefficient (1,000,000 /
    26.3 is 38,023) and is not a property of this server: driven here, the same structure costs
    19.3 bytes an atom written to one decimal and 30.4 to six, a 1.6x spread set by nothing but the
    caller's formatting.

    So this asserts the claim rather than the digits — that every point in that range is far above
    `xtb_max_atoms`, which is the only thing the ceiling's existence depends on. A figure would go
    stale the first time anybody changed a field name in the payload; the inequality does not.
    """
    import json

    from mcp_server_kit.app import DEFAULT_MAX_REQUEST_BYTES

    # Deterministic rather than sampled: what is being measured is the *length* of a serialized
    # coordinate, and a fixed cycle of magnitudes spans the same lengths a real payload does
    # without making the assertion depend on a seed.
    magnitudes = (-28.0, -3.5, -0.25, 0.0, 1.75, 9.5, 27.0)
    atoms = 10_000
    for decimals, elements in ((1, ("C",)), (3, ("C",)), (6, ("C", "N", "O", "Cl", "Fe"))):
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "relax_structure",
                "arguments": {
                    "structure": {
                        "elements": [elements[i % len(elements)] for i in range(atoms)],
                        "positions": [
                            [
                                round(magnitudes[(3 * i + axis) % len(magnitudes)] / 3, decimals)
                                for axis in range(3)
                            ]
                            for i in range(atoms)
                        ],
                        "charge": 0,
                        "multiplicity": 1,
                    }
                },
            },
        }
        per_atom = len(json.dumps(payload, separators=(",", ":")).encode()) / atoms
        fits = int(DEFAULT_MAX_REQUEST_BYTES // per_atom)
        assert fits > settings.xtb_max_atoms * 10, (
            f"a {decimals}-decimal payload costs {per_atom:.1f} bytes an atom, so the body cap "
            f"carries {fits:,} atoms against a ceiling of {settings.xtb_max_atoms} — if that stops "
            "being a wide margin, the transport bound has become the real ceiling and "
            "`Structure`'s own is no longer what protects the optimizer"
        )
