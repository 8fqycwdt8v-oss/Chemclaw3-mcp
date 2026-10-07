"""Data held by more than one server: one answer per question across the corpora, and no corpus
vendored from a source whose terms forbid it.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


SERVERS = ROOT / "servers"


def test_the_two_tables_that_both_hold_densities_agree() -> None:
    """`props` and `chem` both record ambient densities. They must not disagree about a solvent."""
    from chemclaw_mcp_chem.engine.chem import require_canonical_smiles
    from chemclaw_mcp_chem.engine.reagents import dataset as chem_dataset
    from chemclaw_mcp_props.engine import records
    from mcp_server_kit import read_records

    props_densities = {
        require_canonical_smiles(solvent.smiles): (solvent.name, solvent.density_20c)
        for solvent in records.all_solvents()
    }
    compared = 0
    for row in read_records(chem_dataset()):
        raw = row["density_g_per_ml"].strip()
        if not raw:
            continue
        found = props_densities.get(require_canonical_smiles(row["smiles"]))
        if found is None:
            continue
        name, density = found
        compared += 1
        assert abs(float(raw) - density) / density < 0.01, (
            f"chem says {raw} g/mL for {row['name']} and props says {density} for {name}; "
            "one solvent, two answers"
        )
    assert compared >= 20, f"only {compared} solvents overlap — did a table lose its densities?"


def test_the_three_answers_to_molecular_mass_agree() -> None:
    """Three servers derive a molecular mass by three independent routes. They must not disagree."""
    from chemclaw_mcp_chem.engine.chem import molecular_weight
    from chemclaw_mcp_props.engine import records
    from chemclaw_mcp_thermalsafety.engine.oxygen_balance import molar_mass, parse_formula

    tolerance = 0.05
    compared = 0
    for solvent in records.all_solvents():
        compared += 1
        derived = {
            "rdkit MolWt from the SMILES column": molecular_weight(solvent.smiles),
            "thermalsafety molar_mass from the formula column": molar_mass(
                parse_formula(solvent.formula)
            ),
        }
        for how, value in derived.items():
            assert abs(value - solvent.mw) < tolerance, (
                f"{solvent.name}: props tabulates mw={solvent.mw} g/mol, {how} gives {value} "
                f"({solvent.formula}, {solvent.smiles}) — one molecule, two masses"
            )
    assert compared >= 40, f"only {compared} solvents carried a mass — did the table lose a column?"


def test_the_reagent_table_two_servers_carry_is_one_file() -> None:
    """`chem` and `safety` both ship the bench-reagent corpus. It must be the *same* corpus."""
    copies = [
        SERVERS / "chem" / "src" / "chemclaw_mcp_chem" / "data",
        SERVERS / "safety" / "src" / "chemclaw_mcp_safety" / "data" / "reagents",
    ]
    records = {path: (path / "records.csv").read_bytes() for path in copies}
    manifests = {path: (path / "dataset.json").read_bytes() for path in copies}
    assert len(set(records.values())) == 1, f"the reagent tables differ: {list(records)}"
    assert len(set(manifests.values())) == 1, f"the reagent manifests differ: {list(manifests)}"


def test_no_corpus_is_vendored_from_gestis_and_a_built_ghs_says_why() -> None:
    """GESTIS forbids transfer into other information systems, so no corpus here comes from it."""
    corpora = sorted(SERVERS.glob("*/src/*/data/**/dataset.json"))
    assert corpora, "no vendored dataset.json found; the glob has drifted from the layout"
    sourced = []
    for path in corpora:
        provenance = json.loads(path.read_text(encoding="utf-8"))
        for field in ("retrieved_from", "licence"):
            if "gestis" in str(provenance.get(field, "")).lower():
                sourced.append(f"{path.relative_to(ROOT)}:{field}")
    assert not sourced, (
        f"corpus provenance naming GESTIS: {sourced}. GESTIS prohibits transfer into other "
        "information systems; `ghs` is built on PubChem LCSS and ECHA C&L instead"
    )
    ghs = SERVERS / "ghs"
    if ghs.is_dir():
        readme = (ghs / "README.md").read_text(encoding="utf-8")
        assert "GESTIS" in readme, (
            "servers/ghs/README.md does not name GESTIS. The reason its corpus is PubChem LCSS and "
            "ECHA C&L has to travel with the server, or the next contributor reaches for GESTIS"
        )
