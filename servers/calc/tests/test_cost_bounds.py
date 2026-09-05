"""What stops a call costing more than anybody agreed to pay: a size bound and a wall clock.

`docs/adding-a-server.md` states what a slow tool owes the fleet — "a bound on its input so the cost
cannot run away unpriced, a `request_timeout` stating the real budget". This server had neither on
the paths its shipped image actually runs, and the two gaps compound:

- **No atom bound.** `compute_hessian` refused above `xtb_hessian_max_atoms`; nothing else did. A
  `Structure` was validated for internal consistency and never for size, so a ~40,000-atom
  `relax_structure` (well inside the 1 MB body cap) reached `make_calculator` with no refusal.
  Measured here: the ANC preconditioner is a dense `(3N, 3N)` eigendecomposition rebuilt **per leg**
  — 3.6 s at 120 atoms, 11.6 s at 240, 32.9 s and 18.7 MB at 510 — and at 40,000 atoms it asks for
  a 127 GB array, which takes the whole uvicorn process down and with it every other connected turn.
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

import pytest
from chemclaw_mcp_calc.engine.config import settings
from chemclaw_mcp_calc.engine.structure import Structure, structure_from_smiles
from chemclaw_mcp_calc.engine.xtb_hessian import HessianSpec, compute_hessian
from chemclaw_mcp_calc.engine.xtb_opt import OptSpec, optimize_structure
from chemclaw_mcp_calc.engine.xtb_props import compute_properties
from chemclaw_mcp_calc.engine.xtb_spec import XtbSpec


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

    Per SCF rather than per leg, because a single L-BFGS leg is itself unbounded in seconds — the
    step count bounds iterations, not time. Water with a microsecond of budget is the cheapest
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
    point is not interruptible — so a spent budget is noticed at most one single point late. At the
    500-atom ceiling `xtb_max_atoms` accepts, one single point measured **81 s** here (53 atoms
    0.20 s, 153 atoms 2.43 s, 303 atoms 19.8 s, 453 atoms 62.7 s, 493 atoms 81.1 s). A margin under
    that is a margin the overrun eats, and the caller expires again.

    Asserted against `xtb_max_atoms` rather than against a bare number so that raising the atom
    ceiling — which raises the worst-case single point superlinearly — fails here instead of
    quietly reintroducing the defect.
    """
    worst_single_point_seconds = 81.0
    assert settings.xtb_max_atoms == 500, (
        "the 81 s figure is one single point at 500 atoms; a different ceiling needs a "
        "re-measurement, not a re-reading of this comment"
    )
    margin = CHEMCLAW3_CALLER_BUDGETS["calc_server_timeout_seconds"] - (
        settings.xtb_inline_timeout_seconds
    )
    assert margin >= worst_single_point_seconds


def test_the_budget_does_not_reach_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refusal is not a result, so the clock is a setting rather than a spec field.

    The distinction is the whole of `test_key_covers_every_knob.py` read in the other direction:
    `xtb_anc_curvature_floor` changes the number that comes back and therefore belongs in the key,
    while a budget only decides whether one comes back at all. Keying on it would fork the cache
    every time a deployment gave itself more time.
    """
    water = structure_from_smiles("O")
    monkeypatch.setattr(settings, "xtb_inline_timeout_seconds", 900.0)
    before = XtbSpec(task="properties").cache_key(water)
    monkeypatch.setattr(settings, "xtb_inline_timeout_seconds", 60.0)
    assert XtbSpec(task="properties").cache_key(water).as_str() == before.as_str()


def test_a_single_point_under_the_ceiling_still_runs() -> None:
    """The bounds refuse the unaffordable and nothing else — asserted, so a typo cannot pass.

    A ceiling written the wrong way round, or a budget in the wrong unit, would turn every
    calculation on this server into a refusal, and every test that only asserts a refusal would
    still be green.
    """
    result = compute_properties(XtbSpec(task="properties"), structure_from_smiles("O"))
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
