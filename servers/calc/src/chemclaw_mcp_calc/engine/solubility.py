"""Aqueous solubility predictor — an open, reproducible ESOL baseline.

Delaney (2004): a closed-form model over four RDKit descriptors, licence-free, with its reported
uncertainty on every prediction. `calc_version` is a calibration-ledger key on the Chemclaw3 side,
so it is derived here and returned, never re-derived there.
"""

from __future__ import annotations

from importlib.metadata import version

from pydantic import BaseModel, Field
from rdkit import Chem
from rdkit.Chem import Crippen, Descriptors, rdMolDescriptors

from chemclaw_mcp_calc.engine.chem import require_canonical_smiles, require_molecule
from chemclaw_mcp_calc.engine.config import settings
from chemclaw_mcp_calc.engine.key import CalculationKey, Keyed
from chemclaw_mcp_calc.engine.uncertainty import Estimate, structural_domain

__all__ = [
    "CALC_TYPE",
    "SolubilityInput",
    "SolubilityResult",
    "cache_key",
    "calc_version",
    "predict_solubility",
]

CALC_TYPE = "solubility"


class SolubilityInput(BaseModel):
    """A solubility request: just the molecule."""

    smiles: str = Field(min_length=1)


class SolubilityResult(Keyed):
    """Predicted aqueous solubility as log S (mol/L), with an uncertainty.

    `uncertainty_log` is one standard deviation in log-S units — report it so a consumer never
    treats the point estimate as exact.

    `estimate` carries the same number in a uniform shape, adding the two things this model could
    not otherwise say: **where the uncertainty came from** and **whether this molecule is something
    ESOL can speak about at all**. Kept beside the domain fields rather than replacing them, so a
    chemist still reads `model` and a skill reads one shape across every calculator.
    """

    smiles: str
    model: str
    log_s_mol_per_l: float
    uncertainty_log: float
    estimate: Estimate | None = None


class EsolBaseline:
    """Delaney (2004) ESOL model — a closed form over four RDKit descriptors.

    log S = 0.16 - 0.63·clogP - 0.0062·MW + 0.066·(rotatable bonds) - 0.74·(aromatic proportion).
    The reported RMSE (`settings.solubility_rmse_log`) is the constant uncertainty.
    """

    name = "esol-delaney"
    version = "2004"

    def predict(self, mol: Chem.Mol) -> tuple[float, float]:
        """Return (log S mol/L, uncertainty) from the ESOL descriptor equation."""
        # `Descriptors.MolWt` and `Crippen.MolLogP` are assigned as lambdas inside rdkit and the
        # stub package omits them; the ignores are the stubs' gap, not a doubt about the calls.
        clogp = Crippen.MolLogP(mol)  # type: ignore[attr-defined]
        mw = Descriptors.MolWt(mol)  # type: ignore[attr-defined]
        rotatable = rdMolDescriptors.CalcNumRotatableBonds(mol)
        heavy = mol.GetNumHeavyAtoms()
        aromatic_proportion = (
            sum(1 for atom in mol.GetAtoms() if atom.GetIsAromatic()) / heavy if heavy else 0.0
        )
        log_s = 0.16 - 0.63 * clogp - 0.0062 * mw + 0.066 * rotatable - 0.74 * aromatic_proportion
        return log_s, settings.solubility_rmse_log


# The single solubility model. Called directly (no selection seam until a second exists).
_MODEL = EsolBaseline()


def calc_version() -> str:
    """The version this calculator's results are keyed and calibrated under.

    Model name and version, the RDKit build (all descriptors are RDKit's) and the reported RMSE, so
    any of those changing recomputes. A change in the stored payload's *shape* is
    `key.CALCULATION_EPOCH`'s job instead, because this string is also the calibration ledger's key.
    """
    return (
        f"{_MODEL.name}@{_MODEL.version}/rdkit-{version('rdkit')}/u-{settings.solubility_rmse_log}"
    )


def cache_key(job: SolubilityInput) -> CalculationKey:
    """The versioned identity of predicting `job`'s solubility.

    The only place this key is assembled, shared with `identity.calculation_identity`. Cheap: a
    canonicalisation and two hashes.
    """
    return CalculationKey.build(
        calc_type=CALC_TYPE,
        calc_version=calc_version(),
        inputs={"smiles": require_canonical_smiles(job.smiles)},
    )


def predict_solubility(job: SolubilityInput) -> SolubilityResult:
    """Predict aqueous solubility for one molecule.

    Runs on the SMILES as given (ESOL's descriptors are spelling-invariant) and keys on its
    canonical form. Parsed by `require_molecule` first so the size bounds apply before any
    descriptor; raises `ValueError` on an unparseable SMILES.
    """
    mol = require_molecule(job.smiles)
    log_s, uncertainty = _MODEL.predict(mol)
    key = cache_key(job)
    # Checked on the molecule ESOL was given: a salt gives a plausible-looking but undefined answer.
    in_domain, reasons = structural_domain(mol)
    return SolubilityResult(
        calc_version=key.calc_version,
        calc_key=key.as_str(),
        smiles=job.smiles,
        model=f"{_MODEL.name}@{_MODEL.version}",
        log_s_mol_per_l=log_s,
        uncertainty_log=uncertainty,
        estimate=Estimate(
            value=log_s,
            unit="log10(mol/L)",
            uncertainty=uncertainty,
            # "reported": Delaney's published constant, not a spread measured here.
            method="reported",
            in_domain=in_domain,
            domain_reasons=reasons,
        ),
    )
