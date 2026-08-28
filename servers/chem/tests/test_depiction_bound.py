"""`render_structure` must refuse a large molecule promptly, not lay it out for minutes.

`Compute2DCoords` is superlinear: a ~1500-atom molecule took 672 s of one worker thread, and the
offload thread's cancellation does not stop it, so the caller's timeout frees nothing. Two bounds
protect the pod — a per-call atom ceiling (`MAX_DEPICTION_ATOMS`) and a concurrency ceiling
(`Admission`). These pin both, and that the refusal is *fast* rather than a hang.
"""

from __future__ import annotations

import time

import pytest
from chemclaw_mcp_chem.engine.admission import Admission
from chemclaw_mcp_chem.engine.chem import InvalidSmilesError
from chemclaw_mcp_chem.engine.depiction import MAX_DEPICTION_ATOMS, render_svg


def test_a_molecule_over_the_depiction_bound_is_refused_fast() -> None:
    """A molecule above `MAX_DEPICTION_ATOMS` is refused before `Compute2DCoords`, under 1 s.

    The atom count (just over the bound) is below the parse-level bounds, so this exercises the
    depiction ceiling specifically rather than the SMILES-length or atom-count parse guards.
    """
    oversize = "C" * (MAX_DEPICTION_ATOMS + 20)
    start = time.monotonic()
    with pytest.raises(InvalidSmilesError, match=r"depiction limit|above the"):
        render_svg(oversize)
    assert time.monotonic() - start < 1.0


def test_the_runaway_string_is_refused_by_the_length_bound() -> None:
    """The verified PoC (`"C" * 6000`) is refused by the length bound before it ever parses."""
    start = time.monotonic()
    with pytest.raises(InvalidSmilesError):
        render_svg("C" * 6000)
    assert time.monotonic() - start < 1.0


def test_a_real_molecule_still_draws() -> None:
    """The bound must not touch an ordinary structure."""
    svg = render_svg("CCO")
    assert "<svg" in svg


def test_a_real_molecule_with_a_highlight_still_draws() -> None:
    """A highlighted depiction — the torsion-confirmation path — is unaffected."""
    svg = render_svg("CC(=O)Nc1ccccc1", highlight_atoms=[0, 1])
    assert "<svg" in svg


def test_admission_refuses_past_the_ceiling() -> None:
    """The concurrency ceiling refuses rather than queues, in terms the caller can act on."""
    gate = Admission(limit=1)
    gate.acquire("render_structure")
    assert gate.in_flight == 1
    with pytest.raises(ValueError, match=r"already rendering 1 structures"):
        gate.acquire("render_structure")
    gate.release()
    assert gate.in_flight == 0
    # A slot is reusable once released.
    gate.acquire("render_structure")
    gate.release()


def test_admission_rejects_a_ceiling_below_one() -> None:
    """A ceiling of zero would refuse every depiction — caught at construction."""
    with pytest.raises(ValueError):
        Admission(limit=0)
