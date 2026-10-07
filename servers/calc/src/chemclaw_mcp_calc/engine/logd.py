"""pH-dependent distribution coefficient (logD), composed from two calculators that already exist.

Crippen LogP plus the GFN2-xTB pKa through Henderson-Hasselbalch. Domain: `pka`'s (neutral O-H/S-H
acids, conjugate acids of aromatic/aryl nitrogen; `CalculationDomainError` propagates), narrowed
to molecules one ionisation term can describe — see `_require_a_single_equilibrium`. The sign of
the correction depends on `PkaResult.site` (acid or base).

No `calc_key`: logD is not cached (its expensive half is a cached pKa). `calc_version` is still
returned, naming both the RDKit build and the pKa version.
"""

from __future__ import annotations

import math
from importlib.metadata import version

from mcp_server_kit.limits import echo
from pydantic import BaseModel, Field
from rdkit import Chem
from rdkit.Chem import Crippen

from chemclaw_mcp_calc.engine.config import settings
from chemclaw_mcp_calc.engine.key import Keyed
from chemclaw_mcp_calc.engine.pka import PkaInput, PkaResult, ionisable_sites, predict_pka
from chemclaw_mcp_calc.engine.pka import calc_version as pka_calc_version
from chemclaw_mcp_calc.engine.uncertainty import CalculationDomainError

__all__ = ["LogdInput", "LogdResult", "calc_version", "predict_logd"]


class LogdInput(BaseModel):
    """A logD request: the molecule and the pH (defaults to `settings.logd_default_ph`)."""

    smiles: str = Field(min_length=1)
    ph: float | None = None


class LogdResult(Keyed):
    """Predicted logD at a given pH, alongside the logP/pKa it was derived from.

    `uncertainty` propagates only the pKa calibration's residual (the dominant error term); Crippen
    LogP itself carries no reported uncertainty in RDKit.

    `calc_key` is `None` here and only here — see the module docstring. `pka_calc_key` carries the
    key of the pKa calculation this was built on, which *is* addressable in Chemclaw3's store, so
    the lineage is not lost by the absence of a key of its own.
    """

    smiles: str
    ph: float
    clogp: float
    pka: float
    log_d: float
    uncertainty: float
    pka_calc_key: str | None = None


def calc_version() -> str:
    """The version of the composition: the RDKit build under LogP, plus the pKa's whole version.

    Prefixed `logd/` so it can never be reconciled against `pka` ledger rows.
    """
    return f"logd/rdkit-{version('rdkit')}/pka-{pka_calc_version()}"


def _require_a_single_equilibrium(result: PkaResult, ph: float, ionised_ratio: float) -> None:
    """Raise unless one Henderson-Hasselbalch term can describe this whole molecule.

    `predict_pka` reports one pKa, so a second ionisable site must be a spectator. Refused:

    - **Amphoteric** (acid and base sites), at every pH: the base site is never evaluated.
    - **Polyprotic** (two or more same-kind sites) while the reported, most ionisable site is
      ionised beyond `settings.logd_negligible_ionised_fraction`; below it every other site is more
      neutral still and the single term is exact within that bound (diols, sugars).

    Refused rather than flagged out-of-domain, because the number would be known wrong by 2-5 log
    units, not merely of unknown validity.
    """
    sites = ionisable_sites(result.smiles)
    if sites.acidic and sites.basic:
        raise CalculationDomainError(
            f"{echo(result.smiles)!r} is amphoteric ({sites.acidic} acidic O-H/S-H site(s) and "
            f"{sites.basic} basic nitrogen(s)): its acid and base equilibria run in opposite "
            "directions and this calculator applies one ionisation term to the single pKa "
            "the pKa predictor reports — which for an amphoteric molecule is always the acid site, "
            "so the base site is neither computed nor bounded. No logD rather than a plausible one"
        )
    if sites.total < 2:
        return
    ionised_fraction = ionised_ratio / (1.0 + ionised_ratio)
    if ionised_fraction > settings.logd_negligible_ionised_fraction:
        kind = "acidic O-H/S-H site(s)" if result.site == "acid" else "basic nitrogen(s)"
        raise CalculationDomainError(
            f"{echo(result.smiles)!r} has {sites.total} {kind} and is {ionised_fraction:.0%} "
            "ionised at "
            f"pH {ph:g} on the one site the pKa predictor reports (pKa {result.pka:.2f}). A second "
            "ionisation of comparable size is unaccounted for and its pKa is not computable from "
            "this predictor, so the single-equilibrium logD would be wrong by an unbounded "
            "amount (measured: succinic acid at pH 7.4 gives -1.5 against a true value near -5)"
        )


def predict_logd(job: LogdInput) -> LogdResult:
    """Predict logD at `job.ph` (or the configured default) for a singly-ionisable molecule.

    Raises `ValueError` wherever `pka.predict_pka` does, and where one Henderson-Hasselbalch term
    cannot describe the molecule (`_require_a_single_equilibrium`). Every call runs the full pKa;
    callers cache via the returned `pka_calc_key`.
    """
    ph = settings.logd_default_ph if job.ph is None else job.ph
    pka_result = predict_pka(PkaInput(smiles=job.smiles))
    # Already canonical and parsed once, so this cannot fail; raised rather than asserted because
    # `python -O` strips asserts and `None` would reach `MolLogP` as an opaque Boost error.
    mol = Chem.MolFromSmiles(pka_result.smiles)
    if mol is None:  # pragma: no cover - `predict_pka` canonicalised this exact string
        raise ValueError(f"invalid SMILES: {echo(job.smiles)!r}")
    clogp = Crippen.MolLogP(mol)  # type: ignore[attr-defined]  # rdkit-stubs gap
    # Henderson-Hasselbalch; the exponent's sign is the whole content:
    #   acid  HA  <-> A- + H+ : ionised fraction rises with pH  -> 10**(pH - pKa)
    #   base  BH+ <-> B  + H+ : ionised fraction falls with pH  -> 10**(pKa - pH)
    exponent = ph - pka_result.pka if pka_result.site == "acid" else pka_result.pka - ph
    # [ionized]/[neutral] — the same quantity the correction and the domain check both need,
    # computed once so the number that is refused on is the number that would have been used.
    ionised_ratio = 10.0**exponent
    _require_a_single_equilibrium(pka_result, ph, ionised_ratio)
    log_d = clogp - math.log10(1.0 + ionised_ratio)
    return LogdResult(
        calc_version=calc_version(),
        calc_key=None,
        pka_calc_key=pka_result.calc_key,
        smiles=pka_result.smiles,
        ph=ph,
        clogp=clogp,
        pka=pka_result.pka,
        log_d=log_d,
        uncertainty=pka_result.uncertainty,
    )
