"""pH-dependent distribution coefficient (logD), composed from two calculators that already exist.

Analytical method development (HPLC mobile-phase pH, liquid-liquid extraction, formulation)
routinely needs logD at a working pH, not just the pH-independent logP. This composes Crippen LogP
(the same descriptor `solubility` uses) and the GFN2-xTB pKa predictor via the standard
Henderson-Hasselbalch relation. No new dependency and no new science.

**Domain, inherited from `pka` and then narrowed once.** Neutral O-H/S-H **acids** (carboxylic
acids, phenols, alcohols, thiols) and the conjugate acid of **aromatic or aryl nitrogen**
(pyridines, azoles, anilines). Aliphatic amines and molecules with neither site raise — `pka`'s own
`CalculationDomainError` propagates unchanged.

The narrowing is this module's own, because the composition is where it bites: `predict_pka` reports
**one** pKa and one number is all a single Henderson-Hasselbalch term can consume, so a molecule
ionising at two sites is outside what this arithmetic can express even though both halves it
composes are inside theirs. `_require_a_single_equilibrium` refuses those — polyprotic acids, and
amphoterics, which had been slipping past the aliphatic-amine refusal because a carboxyl sends the
molecule down the acid branch before the amine is ever looked at.

That domain widened underneath this module once, and the widening had teeth: `predict_pka` began
returning bases where it previously raised, and the Henderson-Hasselbalch correction runs in the
*opposite direction* for one. `PkaResult.site` is what makes the two distinguishable, and
`predict_logd` branches on it. Depending on a collaborator's domain is fine; depending on it without
reading which half of the domain you were handed is what produced a two-log-unit error that raised
nothing.

**The one calculator here with no `calc_key`.** Chemclaw3 never gave logD a cache entry — the
expensive half was already memoized as a pKa and Crippen LogP is sub-millisecond, so a second entry
would have been a second cache for no benefit. There is therefore no ported key derivation to carry
across, and `calc_key` is `None`. `calc_version` is *not* optional: `LogdResult` composes an RDKit
descriptor with a pKa, so the string names both, and the pKa half of it is the ledger key only this
server can produce.
"""

from __future__ import annotations

import math
from importlib.metadata import version

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

    **Constructed for this port; it has no counterpart in Chemclaw3**, which never versioned logD
    because it never cached it. Named `logd/` at the front so it can never be mistaken for a `pka`
    version string and reconciled against `predictions` rows keyed on one — those are two different
    calculators with two different residual distributions, and the exact-match predicate that reads
    that table would silently accept a mislabelled string.
    """
    return f"logd/rdkit-{version('rdkit')}/pka-{pka_calc_version()}"


def _require_a_single_equilibrium(result: PkaResult, ph: float, ionised_ratio: float) -> None:
    """Raise unless one Henderson-Hasselbalch term can describe this whole molecule.

    `predict_pka` reports one pKa and this module applies one ionisation term, so a molecule with a
    second ionisable site is only served correctly when that site is spectator. Two situations where
    it is not, and neither is recoverable from the surface `pka` offers — a second pKa is simply not
    computed — so both refuse rather than return a number:

    - **Amphoteric** (an acid site *and* a base site). Refused at every pH. `predict_pka` takes the
      acid branch whenever any O-H/S-H is present, so the base site is never even evaluated and
      nothing bounds its ionisation. Glycine at pH 7.4 is the measured case: it returned -2.81 with
      no error, silently evading the very refusal `predict_pka` raises for piperidine.
    - **Polyprotic** (two or more sites of the same kind) *while the reported site is substantially
      ionised*. The reported site is the most ionisable one, so when it is essentially neutral every
      other site is even more so and the single term is exact to within
      `settings.logd_negligible_ionised_fraction`'s bound — which is what keeps a diol or a sugar
      (O-H sites with pKa ~15) working. Above that threshold the unseen equilibrium is unbounded:
      succinic acid at pH 7.4 returned -1.48 +/- 1.6 against a true value near -5.

    **Refusal rather than an out-of-domain `Estimate`**, though both conventions exist here
    (`solubility` flags). Two reasons. This module has only ever had the first: its domain limits
    are exceptions inherited from `pka`, and the aliphatic-amine case this closes is *already* a
    refusal. And the two are not the same kind of claim — ESOL on a salt returns a number of unknown
    validity, whereas this returns one known to be wrong by 2-5 log units.
    """
    sites = ionisable_sites(result.smiles)
    if sites.acidic and sites.basic:
        raise CalculationDomainError(
            f"{result.smiles!r} is amphoteric ({sites.acidic} acidic O-H/S-H site(s) and "
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
            f"{result.smiles!r} has {sites.total} {kind} and is {ionised_fraction:.0%} ionised at "
            f"pH {ph:g} on the one site the pKa predictor reports (pKa {result.pka:.2f}). A second "
            "ionisation of comparable size is unaccounted for and its pKa is not computable from "
            "this predictor, so the single-equilibrium logD would be wrong by an unbounded "
            "amount (measured: succinic acid at pH 7.4 gives -1.5 against a true value near -5)"
        )


def predict_logd(job: LogdInput) -> LogdResult:
    """Predict logD at `job.ph` (or the configured default) for a singly-ionisable molecule.

    Raises `ValueError` everywhere `pka.predict_pka` does — an unparseable SMILES, a net-charged or
    open-shell input, a molecule with no ionisable site, an aliphatic amine — and additionally where
    a *single* Henderson-Hasselbalch term cannot describe the molecule at this pH (see
    `_require_a_single_equilibrium`). Never a guessed logD.

    **Ported without the store argument.** In Chemclaw3 the pKa half arrived from the calculation
    cache, so re-asking at a different pH for the same molecule cost only the trivial LogP
    recompute. Here every call runs the full pKa — the honest consequence of a stateless server, and
    the reason Chemclaw3 should keep caching this server's answers by the `pka_calc_key` this result
    carries.
    """
    ph = settings.logd_default_ph if job.ph is None else job.ph
    pka_result = predict_pka(PkaInput(smiles=job.smiles))
    # `pka_result.smiles` is already the canonical form `predict_pka` computed on, so this reparse
    # cannot fail — the acid was already proven parseable to get here.
    mol = Chem.MolFromSmiles(pka_result.smiles)
    assert mol is not None  # pragma: no cover - guaranteed by predict_pka's own validation
    clogp = Crippen.MolLogP(mol)  # type: ignore[attr-defined]  # rdkit-stubs gap
    # Henderson-Hasselbalch, and the sign of this exponent is the entire content of it.
    #   acid  HA  <-> A- + H+ : the ionized fraction *rises* with pH  -> 10**(pH - pKa)
    #   base  BH+ <-> B  + H+ : the ionized fraction *falls* with pH  -> 10**(pKa - pH)
    # Written as a branch on `site` rather than one formula because getting it wrong is silent:
    # before this, a base took the acid form and pyridine at pH 7.4 came out two log units too
    # lipophobic while looking entirely ordinary.
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
