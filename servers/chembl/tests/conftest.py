"""A miniature ChEMBL release in ChEMBL's own table layout, for testing the extraction script.

Only the tables and columns `scripts/chembl_extract_subset.py` reads. Three compounds (ethanol,
acetic acid and its sodium salt, whose parent is the acid), two targets and a handful of activities
chosen so every rule of the aggregation has a row that would break it: a duplicate, a value ChEMBL
flags as suspect, a censored value, a second unit, a second assay with a lower confidence score.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from chemclaw_mcp_chembl.engine.chem import inchi_key
from rdkit import Chem

DDL = """
CREATE TABLE molecule_dictionary (molregno INTEGER PRIMARY KEY, chembl_id TEXT, pref_name TEXT,
    max_phase REAL, molecule_type TEXT);
CREATE TABLE compound_properties (molregno INTEGER PRIMARY KEY, full_molformula TEXT,
    full_mwt REAL);
CREATE TABLE compound_structures (molregno INTEGER PRIMARY KEY, canonical_smiles TEXT,
    standard_inchi_key TEXT);
CREATE TABLE molecule_hierarchy (molregno INTEGER PRIMARY KEY, parent_molregno INTEGER);
CREATE TABLE molecule_synonyms (molsyn_id INTEGER PRIMARY KEY, molregno INTEGER, synonyms TEXT,
    syn_type TEXT);
CREATE TABLE target_dictionary (tid INTEGER PRIMARY KEY, chembl_id TEXT, pref_name TEXT,
    target_type TEXT, organism TEXT);
CREATE TABLE assays (assay_id INTEGER PRIMARY KEY, tid INTEGER, confidence_score INTEGER);
CREATE TABLE activities (activity_id INTEGER PRIMARY KEY, assay_id INTEGER, molregno INTEGER,
    standard_type TEXT, standard_units TEXT, standard_relation TEXT, standard_value REAL,
    pchembl_value REAL, potential_duplicate INTEGER, data_validity_comment TEXT);
"""

#: (molregno, chembl_id, pref_name, max_phase, type, formula, weight, smiles, parent)
COMPOUNDS = [
    (1, "CHEMBL1", "ETHANOL", 4.0, "Small molecule", "C2H6O", 46.07, "CCO", None),
    (2, "CHEMBL2", "ACETIC ACID", 4.0, "Small molecule", "C2H4O2", 60.05, "CC(=O)O", None),
    (3, "CHEMBL3", None, 4.0, "Small molecule", "C2H3NaO2", 82.03, "CC(=O)[O-].[Na+]", 2),
    (4, "CHEMBL4", "LONELY", None, "Small molecule", "CH4", 16.04, "C", None),
]
SYNONYMS = [
    (1, "alcohol", "OTHER"),
    (1, "Ethyl Alcohol", "TRADE_NAME"),
    (1, "ETHANOL", "INN"),  # equal to the preferred name: listed once
    (3, "Sodium  Acetate", "USAN"),
]
TARGETS = [
    (10, "CHEMBL203", "Epidermal growth factor receptor erbB1", "SINGLE PROTEIN", "Homo sapiens"),
    (11, "CHEMBL204", "Thrombin", "SINGLE PROTEIN", "Homo sapiens"),
    (12, "CHEMBL999", "Never measured", "SINGLE PROTEIN", "Homo sapiens"),
]
ASSAYS = [(100, 10, 9), (101, 10, 8), (102, 11, 9)]
#: (assay, molregno, type, units, relation, value, pchembl, duplicate, validity)
ACTIVITIES = [
    (100, 1, "IC50", "nM", "=", 10.0, 8.0, 0, None),
    (100, 1, "IC50", "nM", "=", 20.0, 7.7, 0, "Manually validated"),
    (101, 1, "IC50", "nM", "=", 30.0, None, 0, None),
    (101, 1, "IC50", "nM", ">", 1000.0, None, 0, None),
    (100, 1, "IC50", "nM", "=", 999999.0, None, 1, None),  # flagged duplicate: out
    (100, 1, "IC50", "nM", "=", -5.0, None, 0, "Outside typical range"),  # suspect: out
    (100, 1, "IC50", "ug.mL-1", "=", 2.0, None, 0, None),  # another unit: its own row
    (100, 1, "Ki", "nM", "=", 5.0, 8.3, 0, None),
    (102, 1, "IC50", "nM", "=", 400.0, 6.4, 0, None),
    (100, 3, "IC50", "nM", "<", 50.0, None, 0, None),  # only censored: counted, no statistics
    (100, 1, "Kd", "nM", None, 77.0, None, 0, None),  # no relation at all: no summary row
]


def build(path: Path) -> Path:
    """Write the miniature release to `path` and return it."""
    connection = sqlite3.connect(path)
    connection.executescript(DDL)
    for molregno, chembl_id, name, phase, kind, formula, weight, smiles, parent in COMPOUNDS:
        key = inchi_key(Chem.MolFromSmiles(smiles))
        connection.execute(
            "INSERT INTO molecule_dictionary VALUES (?,?,?,?,?)",
            (molregno, chembl_id, name, phase, kind),
        )
        connection.execute(
            "INSERT INTO compound_properties VALUES (?,?,?)", (molregno, formula, weight)
        )
        connection.execute(
            "INSERT INTO compound_structures VALUES (?,?,?)", (molregno, smiles, key)
        )
        connection.execute(
            "INSERT INTO molecule_hierarchy VALUES (?,?)", (molregno, parent or molregno)
        )
    connection.executemany(
        "INSERT INTO molecule_synonyms (molregno, synonyms, syn_type) VALUES (?,?,?)", SYNONYMS
    )
    connection.executemany("INSERT INTO target_dictionary VALUES (?,?,?,?,?)", TARGETS)
    connection.executemany("INSERT INTO assays VALUES (?,?,?)", ASSAYS)
    connection.executemany(
        "INSERT INTO activities (assay_id, molregno, standard_type, standard_units, "
        "standard_relation, standard_value, pchembl_value, potential_duplicate, "
        "data_validity_comment) VALUES (?,?,?,?,?,?,?,?,?)",
        ACTIVITIES,
    )
    connection.commit()
    connection.close()
    return path


@pytest.fixture
def release_db(tmp_path: Path) -> Path:
    """The miniature release as a file the extraction script can read."""
    return build(tmp_path / "chembl_mini.db")
