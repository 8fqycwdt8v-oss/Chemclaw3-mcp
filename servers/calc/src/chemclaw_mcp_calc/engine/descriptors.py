"""Developability descriptor panel — closed-form RDKit, zero extra dependencies.

Chemists routinely need the panel itself — molecular weight, lipophilicity, polar surface area,
H-bond counts, rotatable bonds, sp3 fraction, QED — to screen a candidate's developability (Lipinski
Rule-of-Five, Veber's oral-bioavailability rule) before committing bench time. Every descriptor here
is a closed-form RDKit computation, so this ships with no new dependency and no offline risk, unlike
a trained model.

**Ported without `run_cached_descriptor_profile`.** Its two behaviours beyond the store lookup are
kept inside `compute_descriptor_profile`: canonicalize first, then compute on the canonical form, so
two spellings of one molecule produce one key *and* one identical panel.
"""

from __future__ import annotations

from importlib.metadata import version

from pydantic import BaseModel
from rdkit import Chem
from rdkit.Chem import QED, Crippen, Descriptors, rdMolDescriptors

from chemclaw_mcp_calc.engine.chem import require_canonical_smiles
from chemclaw_mcp_calc.engine.key import CalculationKey, Keyed

__all__ = [
    "CALC_TYPE",
    "DescriptorInput",
    "DescriptorProfile",
    "cache_key",
    "calc_version",
    "compute_descriptor_profile",
]

CALC_TYPE = "developability"


class DescriptorInput(BaseModel):
    """A descriptor-panel request: just the molecule."""

    smiles: str


class DescriptorProfile(Keyed):
    """The developability descriptor panel for one molecule, plus rule-of-thumb flags.

    `lipinski_violations` counts the four Rule-of-Five criteria (MW>500, LogP>5, HBD>5, HBA>10) the
    molecule breaks; `veber_pass` is Veber's oral-bioavailability heuristic (rotatable bonds <=10
    and TPSA<=140 A^2). Both are widely used triage heuristics, not developability verdicts — report
    them as flags a chemist weighs, never as a pass/fail gate on their own.
    """

    smiles: str
    molecular_weight: float
    clogp: float
    tpsa: float
    h_bond_donors: int
    h_bond_acceptors: int
    rotatable_bonds: int
    aromatic_rings: int
    fraction_csp3: float
    qed: float
    lipinski_violations: int
    veber_pass: bool


def calc_version() -> str:
    """Version tying the panel to the RDKit build.

    Every descriptor here is a pure RDKit computation, so an RDKit upgrade is the only thing that
    can shift a value; versioning on it is enough. Public where Chemclaw3 kept it private, because
    after the split the string has to leave this process.
    """
    return f"rdkit-{version('rdkit')}"


def cache_key(job: DescriptorInput) -> CalculationKey:
    """The versioned identity of `job`'s descriptor panel — the only place this key is assembled.

    Read by both `compute_descriptor_profile` and `identity.calculation_identity`, so the string a
    caller looks the answer up under and the string the answer comes back carrying are one
    definition rather than two that agree today.
    """
    return CalculationKey.build(
        calc_type=CALC_TYPE,
        calc_version=calc_version(),
        inputs={"smiles": require_canonical_smiles(job.smiles)},
    )


def compute_descriptor_profile(job: DescriptorInput) -> DescriptorProfile:
    """Compute the developability descriptor panel for one molecule.

    Raises `ValueError` on an unparseable SMILES rather than returning a bogus panel.

    Canonicalizes first and computes on the canonical form, so two spellings of the same molecule
    share one key *and* one panel.
    """
    canonical = require_canonical_smiles(job.smiles)
    mol = Chem.MolFromSmiles(canonical)
    if mol is None:  # pragma: no cover - `require_canonical_smiles` already proved it parses
        raise ValueError(f"invalid SMILES: {job.smiles!r}")

    # Three `type: ignore`s here and one below, all the same rdkit-stubs gap: the descriptor
    # functions are assigned as lambdas at import and `QED.qed` carries no annotations.
    mw = Descriptors.MolWt(mol)  # type: ignore[attr-defined]
    clogp = Crippen.MolLogP(mol)  # type: ignore[attr-defined]
    tpsa = rdMolDescriptors.CalcTPSA(mol)
    hbd = rdMolDescriptors.CalcNumHBD(mol)
    hba = rdMolDescriptors.CalcNumHBA(mol)
    rotatable = rdMolDescriptors.CalcNumRotatableBonds(mol)
    aromatic_rings = rdMolDescriptors.CalcNumAromaticRings(mol)
    fraction_csp3 = rdMolDescriptors.CalcFractionCSP3(mol)
    qed = QED.qed(mol)  # type: ignore[no-untyped-call]

    violations = sum([mw > 500, clogp > 5, hbd > 5, hba > 10])
    veber_pass = rotatable <= 10 and tpsa <= 140

    key = cache_key(DescriptorInput(smiles=canonical))
    return DescriptorProfile(
        calc_version=key.calc_version,
        calc_key=key.as_str(),
        smiles=canonical,
        molecular_weight=mw,
        clogp=clogp,
        tpsa=tpsa,
        h_bond_donors=hbd,
        h_bond_acceptors=hba,
        rotatable_bonds=rotatable,
        aromatic_rings=aromatic_rings,
        fraction_csp3=fraction_csp3,
        qed=qed,
        lipinski_violations=violations,
        veber_pass=veber_pass,
    )
