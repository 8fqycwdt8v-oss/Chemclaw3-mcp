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
from chemclaw_mcp_chem.engine.depiction import (
    MAX_DEPICTION_ATOMS,
    MAX_DEPICTION_CHARS,
    render_svg,
)


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


# --- The output bound -------------------------------------------------------------------------
#
# The atom ceiling above bounds what a depiction costs *this pod*. It bounds nothing about what
# comes back: measured on the installed RDKit at the shipped 320 px, `"C" * 250` renders to 126,348
# characters and 244,522 with every atom highlighted, against a caller that cuts one tool result at
# 60,000 characters divided by the width of the batch it was called in. A cut SVG is not a smaller
# picture, it is a truncated XML fragment — no picture at all, and still paid for in tokens.

ERYTHROMYCIN = (
    "CC[C@H]1OC(=O)[C@H](C)[C@@H](O[C@H]2C[C@@](C)(OC)[C@@H](O)[C@H](C)O2)[C@H](C)"
    "[C@@H](O[C@@H]2O[C@H](C)C[C@@H]([C@H]2O)N(C)C)[C@](C)(O)C[C@@H](C)C(=O)[C@H](C)"
    "[C@@H](O)[C@]1(C)O"
)


def test_a_depiction_over_the_character_bound_is_refused_not_truncated() -> None:
    """An oversized SVG is refused whole, in a message the caller can act on.

    `MAX_DEPICTION_ATOMS` admits this molecule, so this pins the *output* bound specifically.
    """
    oversize = "C" * MAX_DEPICTION_ATOMS
    with pytest.raises(InvalidSmilesError) as refusal:
        render_svg(oversize)
    message = str(refusal.value)
    assert "characters" in message
    assert str(MAX_DEPICTION_CHARS) in message
    assert "CHEMCLAW_CHEM_MAX_DEPICTION_CHARS" in message


def test_a_highlighted_depiction_is_measured_after_its_highlights() -> None:
    """Highlights roughly double the SVG, so the bound has to be read off the finished drawing."""
    smiles = "C" * 100
    assert len(render_svg(smiles)) < MAX_DEPICTION_CHARS
    with pytest.raises(InvalidSmilesError, match="highlight"):
        render_svg(smiles, highlight_atoms=list(range(100)))


def test_a_drug_sized_molecule_is_comfortably_inside_the_bound() -> None:
    """Erythromycin, 51 heavy atoms, is the size the default was set to admit with headroom."""
    svg = render_svg(ERYTHROMYCIN)
    assert "<svg" in svg
    assert len(svg) < MAX_DEPICTION_CHARS
