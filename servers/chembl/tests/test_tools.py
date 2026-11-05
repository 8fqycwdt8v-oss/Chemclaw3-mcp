"""What the tools answer, and what they refuse, against the committed ChEMBL 37 fixture.

The expected values are ChEMBL's own, read from the release by an independent query: the
imatinib/ABL1 row below was reproduced with a plain `SELECT` over `activities` (61 activities, 50
exact, 11 censored, 61 assays, confidence 8-9, the 25th and 26th exact values 658 and 775 nM) and
agrees with the aggregation `scripts/chembl_extract_subset.py` wrote.
"""

from __future__ import annotations

import pytest
from chemclaw_mcp_chembl.engine.bioactivity import MAX_ROWS, bioactivity_summary
from chemclaw_mcp_chembl.engine.lookup import MAX_COMPOUNDS, lookup
from mcp_server_kit.limits import MAX_SMILES_CHARS

ASPIRIN = "CC(=O)Oc1ccccc1C(=O)O"
ASPIRIN_KEY = "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"


# --- lookup ------------------------------------------------------------------------------------


def test_an_id_finds_the_compound_whatever_its_case() -> None:
    for written in ("CHEMBL25", "chembl25", " ChEMBL25 "):
        result = lookup(written)
        assert result.matched_by == "chembl_id" and result.found
        compound = result.compounds[0]
        assert (compound.chembl_id, compound.pref_name) == ("CHEMBL25", "ASPIRIN")
        assert compound.standard_inchi_key == ASPIRIN_KEY
        assert compound.molecular_formula == "C9H8O4"
        assert compound.molecular_weight == pytest.approx(180.16)
        assert compound.max_phase == 4.0


def test_a_name_matches_whole_after_case_and_space_folding_and_never_by_prefix() -> None:
    assert [c.chembl_id for c in lookup("Aspirin", "name").compounds] == ["CHEMBL25"]
    assert [c.chembl_id for c in lookup("  acetylsalicylic   ACID ", "name").compounds] == [
        "CHEMBL25"
    ]
    assert [c.chembl_id for c in lookup("paracetamol").compounds] == ["CHEMBL112"]
    # A prefix, a misspelling and a plural are misses: refuse rather than approximate.
    for near in ("aspir", "asprin", "aspirins", "acetylsalicylic"):
        missed = lookup(near, "name")
        assert not missed.found and missed.compounds == [] and missed.matched_by is None
        assert missed.note and "no compound" in missed.note


def test_a_brand_name_finds_the_drug_and_its_salt_form_with_the_parent_named() -> None:
    found = {c.chembl_id: c for c in lookup("Gleevec", "name").compounds}
    assert set(found) == {"CHEMBL941", "CHEMBL1642"}
    assert found["CHEMBL1642"].parent_chembl_id == "CHEMBL941"
    assert found["CHEMBL941"].parent_chembl_id is None


def test_a_structure_is_matched_on_its_inchikey_however_it_is_written() -> None:
    for written in (ASPIRIN, "OC(=O)c1ccccc1OC(C)=O", "CC(=O)OC1=CC=CC=C1C(O)=O"):
        result = lookup(written, "smiles")
        assert (result.matched_by, result.match_kind) == ("smiles", "exact")
        assert [c.chembl_id for c in result.compounds] == ["CHEMBL25"]
        assert result.note is None


def test_auto_tries_id_then_name_then_structure_and_says_which_answered() -> None:
    assert lookup("CHEMBL25").matched_by == "chembl_id"
    assert lookup("aspirin").matched_by == "name"
    assert lookup(ASPIRIN).matched_by == "smiles"
    assert lookup("Cc1ccccc1").compounds[0].pref_name == "TOLUENE"


def test_an_unknown_thing_is_a_miss_that_says_why_not_an_error() -> None:
    absent_id = lookup("CHEMBL999999999")
    assert absent_id.found is False and absent_id.matched_by is None
    assert absent_id.note == "no compound with that id is in this subset"
    neither = lookup("not a name or a structure")
    assert neither.found is False
    assert neither.note and "SMILES" in neither.note
    unlisted = lookup("C[C@H](N)C(=O)O", "smiles")  # alanine: valid, and not in the subset
    assert (
        unlisted.found is False and unlisted.note == "no compound in this subset has this structure"
    )


def test_a_flat_or_charged_form_finds_the_compound_by_skeleton_and_says_it_is_loose() -> None:
    result = lookup("CC(=O)Oc1ccccc1C(=O)[O-]", "smiles")  # aspirin's carboxylate: another InChIKey
    assert result.match_kind == "same_connectivity"
    assert [c.chembl_id for c in result.compounds] == ["CHEMBL25"]
    assert result.note and "different compounds" in result.note


def test_a_salt_written_with_its_counter_ion_finds_the_parent_form() -> None:
    result = lookup("CC(=O)Oc1ccccc1C(=O)[O-].[Na+]", "smiles")
    assert result.match_kind == "largest_fragment_same_connectivity"
    assert [c.chembl_id for c in result.compounds] == ["CHEMBL25"]
    assert result.note and "several fragments" in result.note


def test_a_compound_with_no_structure_is_found_by_name_with_null_structure_fields() -> None:
    trastuzumab = lookup("trastuzumab").compounds[0]
    assert trastuzumab.chembl_id == "CHEMBL1201585"
    assert trastuzumab.canonical_smiles is None and trastuzumab.standard_inchi_key is None
    assert trastuzumab.molecule_type == "Antibody"


def test_synonyms_are_bounded_and_shortest_first() -> None:
    aspirin = lookup("CHEMBL25").compounds[0]
    assert len(aspirin.synonyms) == 25 and aspirin.synonyms_truncated is True
    assert aspirin.synonyms == sorted(aspirin.synonyms, key=lambda name: (len(name), name))
    assert aspirin.pref_name not in aspirin.synonyms
    assert aspirin.activity_summaries > 100


def test_a_structure_that_cannot_be_read_in_full_is_refused_not_searched() -> None:
    for refused in ("CCO junk", "CCO\tCN", "", "   ", "°C"):
        with pytest.raises(ValueError, match=r"invalid SMILES|empty"):
            lookup(refused, "smiles")
    with pytest.raises(ValueError, match=r"characters, above the .*-character limit"):
        lookup("C" * (MAX_SMILES_CHARS + 1), "smiles")
    with pytest.raises(ValueError, match="characters, above"):
        lookup("x" * (MAX_SMILES_CHARS + 1), "name")


def test_an_unknown_kind_is_refused() -> None:
    with pytest.raises(ValueError, match="kind must be one of"):
        lookup("CHEMBL25", "cas")  # type: ignore[arg-type]


def test_the_query_is_echoed_bounded() -> None:
    result = lookup("q" * 3000, "name")
    assert len(result.query) < 200


def test_lookup_returns_at_most_the_bound() -> None:
    assert len(lookup("CCO", "name").compounds) <= MAX_COMPOUNDS


# --- bioactivity -------------------------------------------------------------------------------


def test_a_row_is_the_aggregate_of_exact_values_reproduced_from_the_release() -> None:
    summary = bioactivity_summary("CHEMBL941", standard_type="IC50", target_chembl_id="CHEMBL1862")
    assert summary.chembl_id == "CHEMBL941" and summary.pref_name == "IMATINIB"
    assert summary.rows_matching == 1 and summary.truncated is False
    row = summary.rows[0]
    assert (row.target_name, row.target_type, row.organism) == (
        "Tyrosine-protein kinase ABL1",
        "SINGLE PROTEIN",
        "Homo sapiens",
    )
    assert (row.standard_type, row.units) == ("IC50", "nM")
    assert (row.n_exact_values, row.n_censored_values, row.n_assays) == (50, 11, 61)
    assert (row.median, row.minimum, row.maximum) == (716.5, 19.9, 74000.0)
    assert row.median_pchembl == pytest.approx(6.145)
    assert (row.assay_confidence_min, row.assay_confidence_max) == (8, 9)


def test_a_name_and_a_structure_reach_the_same_summary_as_the_id() -> None:
    by_id = bioactivity_summary("CHEMBL25", limit=5)
    assert bioactivity_summary("aspirin", limit=5).rows == by_id.rows
    assert bioactivity_summary(ASPIRIN, limit=5).rows == by_id.rows
    assert bioactivity_summary(ASPIRIN, limit=5).match_kind == "exact"


def test_rows_come_most_measured_first_and_the_limit_truncates_honestly() -> None:
    summary = bioactivity_summary("CHEMBL941", limit=5)
    counts = [r.n_exact_values + r.n_censored_values for r in summary.rows]
    assert counts == sorted(counts, reverse=True) and len(summary.rows) == 5
    assert summary.truncated is True and summary.rows_matching > 5


def test_filters_narrow_and_are_echoed_and_compose() -> None:
    kinases = bioactivity_summary(
        "CHEMBL941", standard_type="ic50", min_confidence_score=9, limit=200
    )
    assert kinases.filters == {
        "standard_type": "ic50",
        "target_chembl_id": None,
        "min_confidence_score": 9,
    }
    assert kinases.rows and all(r.standard_type == "IC50" for r in kinases.rows)
    assert all(r.assay_confidence_max == 9 for r in kinases.rows)
    assert (
        kinases.rows_matching < bioactivity_summary("CHEMBL941", standard_type="IC50").rows_matching
    )


def test_units_are_reported_per_row_and_a_unitless_type_has_null_units() -> None:
    rows = bioactivity_summary("CHEMBL9113", limit=200).rows
    assert rows and {r.units for r in rows if r.standard_type == "LogP"} == {None}
    mixed = bioactivity_summary("CHEMBL25", standard_type="Inhibition", limit=200).rows
    assert {r.units for r in mixed} == {"%"}


def test_no_summary_under_the_filters_is_a_result_that_does_not_claim_inactivity() -> None:
    none = bioactivity_summary("CHEMBL25", standard_type="NoSuchType")
    assert none.found is True and none.rows == [] and none.rows_matching == 0
    assert none.note and "not evidence it was never tested" in none.note


def test_an_unknown_compound_is_a_miss_with_no_rows() -> None:
    missed = bioactivity_summary("CHEMBL999999999")
    assert missed.found is False and missed.chembl_id is None and missed.rows == []
    assert missed.note == "no compound with that id is in this subset"


def test_an_identifier_matching_several_compounds_is_refused_naming_them() -> None:
    with pytest.raises(
        ValueError, match=r"matches 2 compounds: CHEMBL1642 \(IMATINIB MESYLATE\), CHEMBL941"
    ):
        bioactivity_summary("Gleevec")


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"limit": 0}, "limit must be between 1 and"),
        ({"limit": MAX_ROWS + 1}, "limit must be between 1 and"),
        ({"min_confidence_score": 10}, "0-9 scale"),
        ({"min_confidence_score": -1}, "0-9 scale"),
        ({"standard_type": "  "}, "standard_type must be"),
        ({"target_chembl_id": "T" * 65}, "target_chembl_id must be"),
    ],
)
def test_out_of_range_arguments_are_refused(arguments: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        bioactivity_summary("CHEMBL25", **arguments)  # type: ignore[arg-type]


def test_a_structure_that_cannot_be_read_is_refused_here_too() -> None:
    with pytest.raises(ValueError, match="invalid SMILES"):
        bioactivity_summary("CCO junk", "smiles")
