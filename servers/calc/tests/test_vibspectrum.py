"""What `vibspectrum` actually contains, pinned against files the pinned binary wrote.

Chemclaw3 drops the leading `3N - n_vib` intensities and pairs the rest with its modes by index,
so the file order matters, and two orderings have the same count but disagree band for band:

    zeros first        [0,0,0,0,0,0,-500,1595,3756]
    strictly ascending [-500,0,0,0,0,0,1595,3756]

Measured against `xtb 6.7.1` (see `data/vibspectrum/README.md`): zeros first, and five of them
for a linear molecule. The tests are literal about the fixture numbers, so an `xtb` bump that
reordered the file fails rather than publishing a wrong spectrum.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from chemclaw_mcp_calc.engine.structure import Structure
from chemclaw_mcp_calc.engine.xtb_cli import _collect, _read_vibspectrum

FIXTURES = Path(__file__).resolve().parent / "data" / "vibspectrum"

# The external (translational/rotational) modes xtb projects out. It prints them with an exactly
# zero wavenumber and no symmetry label, which is the branch its own writer takes for
# `abs(freq) < 1.0e-2` — so this is the same threshold, not a tolerance invented here.
EXTERNAL_THRESHOLD_CM = 0.01


def _entries(name: str) -> list[tuple[float, float]]:
    return _read_vibspectrum(FIXTURES / f"{name}.vibspectrum")


def _external_count(entries: list[tuple[float, float]]) -> int:
    """How many entries at the head of the file are the projected-out external modes."""
    count = 0
    for wavenumber, _ in entries:
        if abs(wavenumber) >= EXTERNAL_THRESHOLD_CM:
            break
        count += 1
    return count


def test_every_cartesian_mode_is_written_exactly_once() -> None:
    """3N entries, N from the geometry beside the fixture — the count the caller reconciles."""
    for name, atoms in (
        ("water-minimum", 3),
        ("ammonia-planar-transition-state", 4),
        ("carbon-dioxide-linear", 3),
    ):
        assert len(_entries(name)) == 3 * atoms, name


def test_the_external_modes_come_first_and_an_imaginary_mode_is_not_one_of_them() -> None:
    """External modes come first, and an imaginary mode is not one of them.

    Planar ammonia has one imaginary mode, written immediately after the six zeros, carrying the
    largest intensity in the file.
    """
    entries = _entries("ammonia-planar-transition-state")

    assert _external_count(entries) == 6
    assert all(intensity == 0.0 for _, intensity in entries[:6])
    assert entries[6] == (-871.17, 548.22019)
    assert [wavenumber for wavenumber, _ in entries[6:]] == sorted(
        wavenumber for wavenumber, _ in entries[6:]
    )


def test_a_linear_molecule_gets_five_external_modes_and_a_bent_one_six() -> None:
    """A linear molecule gets five external modes and a bent one six.

    Chemclaw3 reports `3N - 5` modes for a linear molecule. xtb and Chemclaw3 decide linearity by
    different criteria (unmassed absolute vs mass-weighted relative), so their agreement on these
    molecules is asserted, not assumed.
    """
    linear = _entries("carbon-dioxide-linear")
    assert _external_count(linear) == 5
    assert len(linear) - _external_count(linear) == 3 * 3 - 5

    bent = _entries("water-minimum")
    assert _external_count(bent) == 6
    assert len(bent) - _external_count(bent) == 3 * 3 - 6


def test_how_many_external_modes_there_are_is_a_judgement_about_the_geometry() -> None:
    """How many external modes there are is a judgement about the geometry.

    CO2 bent by a degree is non-linear to xtb (six zeros) but still linear to Chemclaw3, which would
    then mispair every band silently. This is why `ir_wavenumbers_cm` travels with the intensities;
    this test holds what xtb writes at a geometry `compute_hessian` accepts.
    """
    almost_linear = _entries("carbon-dioxide-linear")
    bent_by_one_degree = _entries("carbon-dioxide-bent-one-degree")

    assert _external_count(almost_linear) == 5
    assert _external_count(bent_by_one_degree) == 6
    assert len(almost_linear) == len(bent_by_one_degree) == 9

    # What a caller that drops `3N - 5` gets on the bent geometry: the first external mode kept, and
    # every real band one place further along than it believes.
    shifted = bent_by_one_degree[9 - 4 :]
    assert [intensity for _, intensity in shifted] == [0.0, 68.71118, 0.00429, 1046.64228]
    assert [intensity for _, intensity in bent_by_one_degree[6:]] == [68.71118, 0.00429, 1046.64228]


@pytest.mark.parametrize(
    ("name", "vibrations"),
    [
        ("water-minimum", [(1539.1, 133.26444), (3642.25, 6.7758), (3650.69, 16.67722)]),
        (
            "ammonia-planar-transition-state",
            [
                (-871.17, 548.22019),
                (1506.4, 29.35631),
                (1506.44, 29.33193),
                (3381.48, 0.0),
                (3489.95, 0.08486),
                (3490.23, 0.08622),
            ],
        ),
        (
            "carbon-dioxide-linear",
            [(600.18, 68.70035), (600.18, 68.70035), (1424.95, 0.0), (2593.38, 1046.74705)],
        ),
    ],
)
def test_dropping_the_leading_entries_pairs_every_band_with_its_own_intensity(
    name: str, vibrations: list[tuple[float, float]]
) -> None:
    """Dropping the leading entries pairs every band with its own intensity.

    Reproduces Chemclaw3's `_align_intensities` (`intensities[len(intensities) - n_vib:]`) with
    `n_vib` from the molecule, and asserts the pairs, which the count check cannot.
    """
    entries = _entries(name)
    kept = entries[len(entries) - len(vibrations) :]
    assert kept == vibrations


def test_the_wavenumber_and_intensity_are_the_first_two_numbers_in_a_row_not_the_last_two() -> None:
    """The wavenumber and intensity are the first two numbers in a row, not the last two.

    `xtb --raman` writes four numbers per row, and the last two are Raman columns. Not reachable
    through `xtb_cli.run`'s fixed argv, but `_read_vibspectrum` parses the file format, not one
    invocation.
    """
    entries = _entries("water-with-raman-columns")

    assert len(entries) == 9
    assert entries[6:] == [(1539.1, 78.72168), (3642.25, 47.06267), (3650.69, 145.77031)]


def test_the_binary_backend_carries_a_wavenumber_beside_every_intensity(tmp_path: Path) -> None:
    """`_collect` hands the caller the pair, not half of it.

    The whole hazard above is that an intensity alone cannot say which band it belongs to: the
    caller has to reconstruct that by counting external modes on its own criterion and trusting that
    xtb used the same one. The wavenumbers cost 3N floats beside a Hessian that is already megabytes
    at drug size, and they turn the reconstruction into a lookup. Driven on the real `hessian` and
    `vibspectrum` that one `xtb --ohess` run on water left behind — only the log is synthetic, and
    only because `_collect` reads two lines out of 31 kB of it.
    """
    (tmp_path / "hessian").write_text((FIXTURES / "water-minimum.hessian").read_text())
    (tmp_path / "vibspectrum").write_text((FIXTURES / "water-minimum.vibspectrum").read_text())
    water = Structure(
        elements=[8, 1, 1],
        positions=[[0.0, 0.0, 0.1173], [0.0, 0.7572, -0.4692], [0.0, -0.7572, -0.4692]],
    )

    result = _collect(tmp_path, water, "hess", "TOTAL ENERGY -5.070322 Eh")

    assert result.ir_intensities is not None and result.ir_wavenumbers_cm is not None
    assert len(result.ir_wavenumbers_cm) == len(result.ir_intensities) == 9
    assert result.ir_wavenumbers_cm[6:] == [1539.1, 3642.25, 3650.69]
    assert result.ir_intensities[6:] == [133.26444, 6.7758, 16.67722]
