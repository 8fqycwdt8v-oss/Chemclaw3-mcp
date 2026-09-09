"""What `vibspectrum` actually contains, pinned against files the pinned binary wrote.

**The IR intensities are paired with their modes by position, and nothing pinned the position.**
This server parses `vibspectrum` in file order and hands Chemclaw3 a bare list of intensities, one
per Cartesian mode; Chemclaw3 drops the leading `3N - n_vib` of them and pairs the rest with its own
projected modes by index. Two orderings pass the only check either side makes — the count — and they
disagree band for band, the second shifting every band by one and zeroing the last:

    zeros first        [0,0,0,0,0,0,-500,1595,3756]
    strictly ascending [-500,0,0,0,0,0,1595,3756]

Which one `xtb` writes was, until these fixtures, asserted by a docstring on the consuming side and
by nothing at all on the producing side, where the evidence lives. It is now measured: see
`data/vibspectrum/README.md` for the commands, run against `xtb 6.7.1`, the version
`Containerfile` pins. The answer is *zeros first* — and the second half of the answer is that there
are **five** of them for a linear molecule, not six.

These tests are deliberately literal about the numbers in the files. A fixture is evidence about one
run of one version; what makes it worth keeping is that the assumption it holds is otherwise written
down nowhere, so an `xtb` bump that reordered the file would land as a wrong published spectrum
rather than as a failure.
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
    """The measurement that settles the pairing, on the case where the two orderings differ.

    Planar ammonia is the inversion saddle: one imaginary mode at -871.17 cm^-1. Under "strictly
    ascending" it would be entry 1 and every band after it would shift; it is entry 7, immediately
    after the six zeros. The intensity beside it is the largest in the file, which is what an
    inversion mode looks like and what a shifted pairing would move onto a C-H stretch.
    """
    entries = _entries("ammonia-planar-transition-state")

    assert _external_count(entries) == 6
    assert all(intensity == 0.0 for _, intensity in entries[:6])
    assert entries[6] == (-871.17, 548.22019)
    assert [wavenumber for wavenumber, _ in entries[6:]] == sorted(
        wavenumber for wavenumber, _ in entries[6:]
    )


def test_a_linear_molecule_gets_five_external_modes_and_a_bent_one_six() -> None:
    """The other half of the pairing, and the half a `3N - 6` assumption gets wrong.

    Chemclaw3 does not assume 6: `thermo._vibrational_basis` builds the rotations about the
    principal axes and keeps two of them for a linear molecule, so it reports `3N - 5` modes and
    drops `3N - (3N - 5) = 5` entries. That agrees with what xtb writes here. The two sides reach it
    by different criteria — xtb tests the *unmassed* inertia moments against an absolute 1e-4 bohr^2
    (`is_linear`, `src/axis_trafo.f90`), Chemclaw3 tests the *mass-weighted* moments against a
    relative 1e-4 — so their agreement is a fact about these molecules rather than a shared rule,
    and it is asserted here rather than assumed.
    """
    linear = _entries("carbon-dioxide-linear")
    assert _external_count(linear) == 5
    assert len(linear) - _external_count(linear) == 3 * 3 - 5

    bent = _entries("water-minimum")
    assert _external_count(bent) == 6
    assert len(bent) - _external_count(bent) == 3 * 3 - 6


def test_how_many_external_modes_there_are_is_a_judgement_about_the_geometry() -> None:
    """The count is not a property of the formula, and this is the case that proves it.

    Bend CO2 by one degree and xtb stops calling it linear: five external modes at 180.0, 179.99 and
    179.9 degrees, **six** from 179.0 on. Chemclaw3 makes the same judgement independently and by a
    different rule — mass-weighted moments against a relative threshold, against xtb's unmassed
    moments against an absolute one — and at 179.0 it still says linear, so it asks for `3N - 5 = 4`
    vibrations and drops `9 - 4 = 5` entries of the six. Every band shifts by one: the strong
    2593 cm^-1 stretch's intensity lands on its third mode and its first gets a zero, with nothing
    raised, because the only check is that the difference is not negative.

    That is the failure Finding-1 predicted, reached by the count rather than by the order — and it
    is why `ir_wavenumbers_cm` travels with the intensities. This test holds the producing side of
    it: what xtb writes, at a geometry `compute_hessian` accepts on purpose.
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
    """The consumer's arithmetic, run here against the producer's file.

    `_align_intensities` over in Chemclaw3 is exactly `intensities[len(intensities) - n_vib:]`, so
    this reproduces it — with `n_vib` taken from the molecule (3N-6, or 3N-5 for CO2) rather than
    from the file — and asserts the pairs that come out. This is the assertion the count check
    cannot make: both candidate orderings have the same length, and only one of them puts these
    intensities on these bands.
    """
    entries = _entries(name)
    kept = entries[len(entries) - len(vibrations) :]
    assert kept == vibrations


def test_the_wavenumber_and_intensity_are_the_first_two_numbers_in_a_row_not_the_last_two() -> None:
    """A wider row carries the wavenumber and the IR intensity in the same two columns.

    `xtb --raman` writes four numbers per vibration — wavenumber, IR intensity, Raman activity,
    Raman scattering cross-section — before the selection rules. Reading the last two numeric fields
    returns the two Raman columns; reading from the left returns what the caller asked for. This
    format is not reachable through `xtb_cli.run`, whose argv is fixed and carries no `--raman`,
    `--alpha` or `--ptb`; it is pinned because `_read_vibspectrum` is a parser for the file rather
    than for one invocation of it, and because the code took the last two while the comment beside
    it said it indexed from the left.
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
