"""The logD domain-check contract with Chemclaw3, written as literal strings and numbers.

Chemclaw3 composes logD client-side from a cached `PkaResult` (Crippen LogP, one
Henderson-Hasselbalch term, and a single-equilibrium domain check), duplicating this server's
`engine/pka.py` site enumeration and `engine/logd.py` refusal. Two kinds of duplication, pinned
two ways:

1. **Composition arithmetic.** `COMPOSITION_CONTRACT` pins it on frozen, literal `PkaResult`
   inputs (GFN2-xTB is not bit-reproducible), with `predict_pka` monkeypatched. Expected values
   come from Chemclaw3's own function, e.g.:

       cd /path/to/Chemclaw3 && uv run --no-sync python -c "
       from chemclaw.science.calc.models import PkaResult
       from chemclaw.science.calc.logd import logd_from_pka
       r = logd_from_pka(PkaResult(smiles='c1ccncc1', method='GFN2-xTB/ALPB-water',
           pka=5.3997777211992215, deprotonation_energy_kcal=0.0, uncertainty=1.0, site='base'),
           ph=7.4)
       print(r.clogp, r.log_d)"

2. **Site enumeration.** `SITE_CONTRACT` pins `ionisable_sites` against literal `(acidic, basic)`
   counts from `chemclaw.science.calc.logd.ionisable_sites`.

Refusals inside `predict_pka` (unparseable, net-charged, aliphatic amine) happen before any
`PkaResult` exists, so Chemclaw3 only relays them; `PKA_REFUSAL_CASES` pins that this server's
`predict_logd` refuses on exactly those inputs.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from chemclaw_mcp_calc.engine.logd import LogdInput, predict_logd
from chemclaw_mcp_calc.engine.pka import PkaResult, ionisable_sites
from chemclaw_mcp_calc.engine.uncertainty import CalculationDomainError

# (smiles, (acidic sites, basic sites)) — Chemclaw3's `ionisable_sites` on the same input.
# Spans both ionisable classes plus the two ways a domain refusal is triggered downstream:
# amphoteric (both counts positive) and polyprotic (one count >= 2).
SITE_CONTRACT: list[tuple[str, tuple[int, int]]] = [
    ("c1ccncc1", (0, 1)),  # pyridine: one aryl nitrogen, no O-H/S-H
    ("O=C(O)c1ccccc1", (1, 0)),  # benzoic acid
    ("Oc1ccccc1", (1, 0)),  # phenol
    ("CC(=O)O", (1, 0)),  # acetic acid
    ("c1c[nH]cn1", (0, 1)),  # imidazole: pyrrole-type N excluded, pyridine-type N counted
    ("Nc1ccccc1", (0, 1)),  # aniline
    ("NCC(=O)O", (1, 1)),  # glycine: amphoteric
    ("O=C(O)CCC(=O)O", (2, 0)),  # succinic acid: diprotic
    ("OCCO", (2, 0)),  # ethylene glycol: diprotic, but pKa ~13.5 keeps it in-domain at pH 7.4
    ("CC(=O)[O-]", (0, 0)),  # acetate anion: charged, no neutral O-H/S-H or basic N left
]


@pytest.mark.parametrize(("smiles", "counts"), SITE_CONTRACT, ids=[row[0] for row in SITE_CONTRACT])
def test_ionisable_sites_matches_chemclaw3(smiles: str, counts: tuple[int, int]) -> None:
    """One row of the structural half of the contract — no xTB, so nothing here is noisy."""
    sites = ionisable_sites(smiles)
    assert (sites.acidic, sites.basic) == counts


# A frozen `PkaResult`, the pH to compose at, and the expected outcome: `(clogp, log_d)` or a
# substring of the raised `CalculationDomainError`, each produced by Chemclaw3's `logd_from_pka`.
COMPOSITION_CONTRACT: list[tuple[str, str, float, float, float, tuple[float, float] | str]] = [
    # -- in-domain bases (aromatic/aryl nitrogen) --
    ("c1ccncc1", "base", 5.3997777211992215, 1.0, 7.4, (1.0816, 1.0772808264400353)),
    ("c1c[nH]cn1", "base", 6.96715250082292, 1.0, 7.4, (0.4097, 0.27326254994610655)),
    ("Nc1ccccc1", "base", 4.23304540694247, 1.0, 7.4, (1.2688000000000001, 1.268504415322401)),
    # -- in-domain acids (neutral O-H/S-H) --
    ("O=C(O)c1ccccc1", "acid", 6.278404436680525, 1.6, 7.4, (1.3848, 0.23156189081651313)),
    ("Oc1ccccc1", "acid", 11.220059514787671, 1.6, 7.4, (1.3921999999999999, 1.3921342808501795)),
    ("CC(=O)O", "acid", 6.512636701266935, 1.6, 7.4, (0.09089999999999993, -0.8493916194964322)),
    # -- diprotic, but negligibly ionised on the unseen site: served, not refused --
    ("OCCO", "acid", 13.469003111775024, 1.6, 7.4, (-1.0290000000000001, -1.0290003704938595)),
    # -- refusals decided by the composition arithmetic itself --
    ("NCC(=O)O", "acid", 5.564141622686336, 1.6, 7.4, "is amphoteric"),
    ("O=C(O)CCC(=O)O", "acid", 5.998380693173921, 1.6, 7.4, "96% ionised"),
]


@pytest.mark.parametrize(
    ("smiles", "site", "pka", "uncertainty", "ph", "expected"),
    COMPOSITION_CONTRACT,
    ids=[row[0] for row in COMPOSITION_CONTRACT],
)
def test_predict_logd_composition_matches_chemclaw3(
    smiles: str,
    site: str,
    pka: float,
    uncertainty: float,
    ph: float,
    expected: tuple[float, float] | str,
) -> None:
    """One row of the arithmetic half of the contract, on a frozen pKa.

    `predict_pka` is monkeypatched, so only the duplicated domain-check and Henderson-Hasselbalch
    arithmetic is compared, free of SCF noise.
    """
    frozen = PkaResult(
        calc_version="contract-test",
        calc_key="contract-test",
        smiles=smiles,
        method="GFN2-xTB/ALPB-water",
        pka=pka,
        deprotonation_energy_kcal=0.0,
        uncertainty=uncertainty,
        site=site,  # type: ignore[arg-type]
    )
    with patch("chemclaw_mcp_calc.engine.logd.predict_pka", return_value=frozen):
        if isinstance(expected, str):
            with pytest.raises(CalculationDomainError, match=expected):
                predict_logd(LogdInput(smiles=smiles, ph=ph))
        else:
            result = predict_logd(LogdInput(smiles=smiles, ph=ph))
            expected_clogp, expected_log_d = expected
            assert result.clogp == pytest.approx(expected_clogp, abs=1e-9)
            assert result.log_d == pytest.approx(expected_log_d, abs=1e-9)
            assert result.pka == pka


# Refused inside `predict_pka`, before a `PkaResult` exists for either side to compose from — see
# the module docstring's third paragraph for why there is no Chemclaw3 copy of this to pin against.
# `(smiles, substring that must appear in the raised error)`.
PKA_REFUSAL_CASES: list[tuple[str, str]] = [
    ("CCN", "aliphatic nitrogen"),  # ethylamine: outside the base calibration's domain
    ("CC(=O)[O-]", "net formal charge"),  # acetate anion: charged, outside the acid calibration
    ("not-a-molecule", "invalid SMILES"),  # unparseable
]


@pytest.mark.parametrize(
    ("smiles", "substring"), PKA_REFUSAL_CASES, ids=[c[0] for c in PKA_REFUSAL_CASES]
)
def test_predict_logd_relays_the_upstream_pka_refusal(smiles: str, substring: str) -> None:
    """`predict_logd` refuses on exactly what `predict_pka` refuses on — no local override.

    Single-sided by construction: Chemclaw3 relays this refusal from `predict_pka`.
    """
    with pytest.raises((ValueError, CalculationDomainError), match=substring):
        predict_logd(LogdInput(smiles=smiles, ph=7.4))
