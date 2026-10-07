"""The reagent table validates itself, because a hand-compiled table is a table with typos in it.

Unlike `props`, this corpus carries no second independent number to cross-check (a name, a
structure, sometimes a density), so the checks are the real ones available:

- **Structural.** Every SMILES parses under the strict gate; no spelling resolves two ways and no
  two substances share a structure, either of which makes answers depend on file order.
- **Range.** A density outside 0.5-2.0 g/mL is not a solvent charged by volume.
- **The manifest's own claims** (row and spelling counts).

Drift against Chemclaw3 is in `test_canonicalization_contract.py`; densities are checked against
`props` in the fleet tests.
"""

from __future__ import annotations

import pytest
from chemclaw_mcp_chem.engine import reagents
from chemclaw_mcp_chem.engine.chem import require_canonical_smiles
from mcp_server_kit import read_records

RECORDS = read_records(reagents.dataset())

# The band a bulk process solvent's ambient density falls in — n-pentane at 0.626 is the lightest
# thing anyone measures out by volume, dibromomethane at 2.48 about the heaviest, and this table
# holds neither. Wide on purpose: it is a typo detector, not a physical claim.
DENSITY_RANGE = (0.5, 2.0)


def test_the_dataset_is_the_one_that_was_approved() -> None:
    """`load_dataset` verifies the checksum on load; this is the assertion that it is doing so."""
    loaded = reagents.dataset()
    assert loaded.records_path.is_file()
    assert loaded.licence and loaded.retrieved_from
    assert loaded.citation().startswith("bench-reagents v")


@pytest.mark.parametrize("row", RECORDS, ids=[row["name"] for row in RECORDS])
def test_every_structure_parses_strictly(row: dict[str, str]) -> None:
    """A row whose SMILES does not parse is a reagent that silently cannot be charged.

    The strict gate rather than a bare parse, so a cell with a stray space in it — which RDKit
    would read up to and no further — fails here instead of shipping half a molecule.
    """
    assert require_canonical_smiles(row["smiles"])


@pytest.mark.parametrize("row", RECORDS, ids=[row["name"] for row in RECORDS])
def test_every_row_has_a_name_and_at_least_one_spelling(row: dict[str, str]) -> None:
    """The display name is what a chemist reads back; the spellings are what they type."""
    assert row["name"].strip()
    assert [part for part in row["synonyms"].split(";") if part.strip()]


@pytest.mark.parametrize("row", RECORDS, ids=[row["name"] for row in RECORDS])
def test_a_density_is_in_the_range_a_liquid_can_be(row: dict[str, str]) -> None:
    """Blank means "not charged by volume" and is the common case; a number must be plausible."""
    raw = row["density_g_per_ml"].strip()
    if not raw:
        return
    low, high = DENSITY_RANGE
    assert low < float(raw) < high, f"{row['name']}: {raw} g/mL is not a liquid anybody measures"


def test_every_spelling_resolves_to_its_own_row() -> None:
    """Every spelling resolves to its own row, read off the public surface.

    Duplicate spellings or shared structures are refused at index build; this confirms from outside.
    """
    for row in RECORDS:
        canonical = require_canonical_smiles(row["smiles"])
        for spelling in row["synonyms"].split(";"):
            match = reagents.resolve_compound_name(spelling)
            assert match is not None, f"{spelling!r} resolves to nothing"
            assert (match.name, match.smiles) == (row["name"], canonical)


def test_the_corpus_is_the_size_the_manifest_describes() -> None:
    """`dataset.json`'s prose is what a reviewer reads instead of the file, so it must be true."""
    description = reagents.dataset().description
    spellings = sum(len(row["synonyms"].split(";")) for row in RECORDS)
    assert f"{len(RECORDS)} bench reagents" in description
    assert f"{spellings} spellings" in description
    assert f"{sum(1 for row in RECORDS if row['density_g_per_ml'].strip())} that can be" in (
        description
    )


def test_every_recorded_density_is_reachable_through_a_name() -> None:
    """A density keyed to a row nobody can name is a solvent charge that cannot be computed.

    `stoichiometry_table` would report it as "unknown solvent" rather than a broken table.
    """
    for row in RECORDS:
        if not row["density_g_per_ml"].strip():
            continue
        spelling = row["synonyms"].split(";")[0]
        assert reagents.density_of(spelling) == float(row["density_g_per_ml"])
