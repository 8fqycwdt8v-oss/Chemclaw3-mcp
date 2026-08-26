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
    `CHEMCLAW_XTB_INLINE_TIMEOUT_SECONDS`, which defaults to the manifest's `request_timeout`.

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
    of 76 atoms in 218 s, about 25 minutes. The caller gives up at 900 s and the thread keeps going.
    """
    monkeypatch.setattr(settings, "xtb_inline_timeout_seconds", 1e-6)
    water = structure_from_smiles("O")
    with pytest.raises(ValueError, match=r"exceeded this server's inline budget"):
        compute_hessian(HessianSpec(engine="tblite"), water)


def test_the_default_budget_is_the_one_the_manifest_promises() -> None:
    """`request_timeout: 900` is a statement about the work, or it is a statement about nothing.

    The two subprocess timeouts are deliberately longer — a CREST search is hours and Chemclaw3
    calls it knowing that — but the in-process paths are the ones the shipped image takes for every
    `opt` and `hess`, and those are what the manifest's number is about.
    """
    assert settings.xtb_inline_timeout_seconds == 900.0


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
