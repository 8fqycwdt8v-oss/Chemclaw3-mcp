"""The reagent table validates itself, because a hand-compiled table is a table with typos in it.

`props` sets the bar here with three checks that compare pairs of *independently written* numbers —
CAS check digits, formula against molecular weight, Antoine constants against the boiling point —
so a transposed digit fails a test instead of answering a question. **This corpus has no
counterpart to those, and saying so is more useful than inventing a weak one.** It is a port of a
table Chemclaw3 already reviewed, and it carries no second number about any substance: a name, a
structure, and for some rows a density. There is nothing here to cross-check a SMILES against, and
generating a formula column from that same SMILES would validate RDKit rather than the table.

So the checks below are the ones that are real:

- **Structural.** Every SMILES parses under the strict gate. No spelling resolves two ways, and no
  two substances resolve to one structure — either would make an answer depend on file order, which
  is the defect that shows up once, in production, in a number nobody re-derives.
- **Range.** A density outside 0.5-2.0 g/mL is not a solvent anyone charges by volume; a
  transposed digit lands outside that band far more often than inside it.
- **The claims the manifest makes about itself** — row and spelling counts — because
  `dataset.json`'s description is what a reviewer reads instead of the file.

The checks that actually catch drift for this corpus live elsewhere, and deliberately:
`test_canonicalization_contract.py` pins the structure definition against Chemclaw3, and
`tests/test_fleet.py` pins the densities against `props`'s independently compiled solvent table.
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
    """The two ambiguities that would make an answer depend on file order, caught from outside.

    A spelling claimed by two rows resolves to the wrong name here; two rows sharing one structure
    make the reverse map hand back the other row's name. Both are refused when the index is built,
    and this is the check that reads the refusal off the public surface rather than trusting it.
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

    The failure it guards is silent in exactly the wrong direction: `stoichiometry_table` refuses a
    solvent with no density on file, so a dead density row reads to a chemist as "this server does
    not know THF" rather than as a broken table.
    """
    for row in RECORDS:
        if not row["density_g_per_ml"].strip():
            continue
        spelling = row["synonyms"].split(";")[0]
        assert reagents.density_of(spelling) == float(row["density_g_per_ml"])
