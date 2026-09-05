"""Aqueous solubility predictor — an open, reproducible ESOL baseline.

Delaney (2004): a closed-form model over four RDKit descriptors, so predictions are real and
license-free today. Every prediction carries the model's reported uncertainty — a fast property
estimate is never presented as exact. There is one model, so it is called directly; when a second
(e.g. a trained GNN) exists, reintroduce a selection seam.

**Ported without `run_cached_solubility`.** The key it built is now built inside
`predict_solubility` and returned in the result — this is the second calculator whose `calc_version`
is a calibration- ledger key on the Chemclaw3 side (`report_measurement` scores `solubility`
predictions against measured log S), so the same rule applies as for pKa: derive it here, ship it,
never re-derive.
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
    The reported RMSE (`settings.solubility_rmse_log`, ~0.75 log units) is used as a constant
    uncertainty. A transparent, license-free default until a trained model replaces it.
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

    Its own function rather than a literal beside the key builder: Chemclaw3's calibration ledger
    keys predictions by `(calc_type, calc_version, input_hash)`, and a ledger whose version string
    drifted from the cache's would score two different things under one name.

    Versioned by model name+version, the RDKit build, *and* the reported RMSE, so bumping the model,
    upgrading RDKit (all four ESOL descriptors are RDKit-computed), or re-tuning
    `solubility_rmse_log` recomputes rather than serving a prediction — or a stale uncertainty —
    from the old one.

    None of those cover the *shape* of what is stored, which is what went wrong when `estimate` was
    added: the ESOL arithmetic was untouched, so `calc_version` correctly did not move, and every
    row already written came back with `estimate=None` — an out-of-domain salt reading as "not
    assessed" rather than "OUT OF DOMAIN". That is `key.CALCULATION_EPOCH`'s job, folded into the
    key by `CalculationKey.build` itself; bumping `calc_version` for it would have been wrong twice
    over, because that string is also the calibration ledger's key and the calibration data was
    still valid.
    """
    return (
        f"{_MODEL.name}@{_MODEL.version}/rdkit-{version('rdkit')}/u-{settings.solubility_rmse_log}"
    )


def cache_key(job: SolubilityInput) -> CalculationKey:
    """The versioned identity of predicting `job`'s solubility.

    Its own function, and the only place this key is assembled, so `predict_solubility` and
    `identity.calculation_identity` cannot disagree about what a caller should look the answer up
    under. Cheap by construction: a canonicalisation and two hashes, no descriptors.
    """
    return CalculationKey.build(
        calc_type=CALC_TYPE,
        calc_version=calc_version(),
        inputs={"smiles": require_canonical_smiles(job.smiles)},
    )


def predict_solubility(job: SolubilityInput) -> SolubilityResult:
    """Predict aqueous solubility for one molecule.

    Raises `ValueError` on an unparseable SMILES rather than returning a bogus number.

    **The prediction runs on the SMILES as given and the key is built on its canonical form**, which
    is exactly what the cached path in Chemclaw3 did: ESOL's four descriptors are invariant to
    spelling, so there is nothing to canonicalize *for*, while the key must collapse spellings or
    two of them would address two rows.

    "As given" is still parsed by `require_molecule` rather than by a bare `MolFromSmiles`, and the
    difference is the order of two failures rather than the set of them: every string this refuses
    was already refused a few lines later by `cache_key`'s canonicalisation. Doing it first is what
    puts the structural size bound *before* the descriptors run, so a megastring is turned away
    instead of being handed to four graph walks on its way to the refusal.
    """
    mol = require_molecule(job.smiles)
    log_s, uncertainty = _MODEL.predict(mol)
    key = cache_key(job)
    # The domain check runs on the molecule ESOL was actually handed. It cannot be inferred from the
    # result: a salt and its free base give different predictions and the same result shape, and the
    # whole point is that the second one is not merely less accurate but undefined.
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
            # "reported": this is the constant from Delaney's paper, not a spread measured here.
            # Narrowing it to a deployment's own residuals would take a read of Chemclaw3's
            # calibration ledger, which is a database call this server does not make.
            method="reported",
            in_domain=in_domain,
            domain_reasons=reasons,
        ),
    )
