"""The logD domain-check contract with Chemclaw3, written as literal strings and numbers.

`D-2026-08-16-the-physics-leaves-the-cache-stays` decomposed `predict_logd`: the pKa physics stayed
here (`engine/pka.py`), but Chemclaw3 never calls this server's `predict_logd` tool. Instead it
takes a *cached* `PkaResult` — however it was obtained, in production a call to this server's own
`predict_pka` — and composes logD client-side in `chemclaw.science.calc.logd`: a Crippen LogP sum,
one Henderson-Hasselbalch term, and a domain check (`ionisable_sites`, `_lone_pair_is_available`,
`_require_a_single_equilibrium`) that decides which molecules a single equilibrium term can honestly
describe. That domain-check arithmetic is therefore duplicated verbatim across the repository
boundary — this server's copy lives in `engine/pka.py` (site enumeration) and `engine/logd.py`
(the single-equilibrium refusal); Chemclaw3's copy is inlined into `science/calc/logd.py` because it
has no `pka.py` left to import from. Nothing before this file pinned the two copies together.

**Two different kinds of duplication, tested two different ways.**

1. **The composition arithmetic** (`ionisable_sites`, `_require_a_single_equilibrium`, Crippen,
   Henderson-Hasselbalch) runs on *both* sides once a `PkaResult` exists, so it is where the two
   repositories could silently disagree while both look correct in isolation.
   `COMPOSITION_CONTRACT` below pins it against **frozen, literal** `PkaResult` inputs —
   deliberately not a fresh `predict_pka` call, because GFN2-xTB's SCF is not bit-reproducible
   across runs (measured: pyridine pKa 5.399777721199..., varying in the 9th significant figure
   between repeated calls on identical input on this machine). Freezing the pKa as data removes
   that noise and isolates exactly the arithmetic this file exists to pin. Each row's
   `predict_logd` value is reproduced here by monkeypatching `predict_pka` to return the frozen
   result, and each expected value was produced by feeding the identical frozen `PkaResult` to
   Chemclaw3's own `logd_from_pka`:

       cd /path/to/Chemclaw3 && uv run --no-sync python -c "
       from chemclaw.science.calc.models import PkaResult
       from chemclaw.science.calc.logd import logd_from_pka
       r = logd_from_pka(PkaResult(smiles='c1ccncc1', method='GFN2-xTB/ALPB-water',
           pka=5.3997777211992215, deprotonation_energy_kcal=0.0, uncertainty=1.0, site='base'),
           ph=7.4)
       print(r.clogp, r.log_d)"

   Run once per row in `COMPOSITION_CONTRACT`, substituting that row's `smiles`/`site`/`pka`/
   `uncertainty`/`ph`. Pyridine's numbers are the ones this table (and the task that produced it)
   measured live: `clogp=1.0816`, `log_d=1.0772808264400353`.

2. **`ionisable_sites`' structural enumeration** needs no pKa at all — it is pure RDKit graph
   inspection — so `SITE_CONTRACT` pins it directly against literal `(acidic, basic)` counts,
   produced the same way:

       cd /path/to/Chemclaw3 && uv run --no-sync python -c "
       from chemclaw.science.calc.logd import ionisable_sites
       s = ionisable_sites('c1ccncc1'); print(s.acidic, s.basic)"

**A third class of refusal has no Chemclaw3 counterpart to pin at all**, and that is itself part of
the finding: an unparseable SMILES, a net-charged input, and an aliphatic amine are all refused
inside `predict_pka` (`engine/pka.py`), *before* a `PkaResult` ever exists — so Chemclaw3 never
receives one to compose from and runs no arithmetic of its own on these inputs; in production it
would see this server's `predict_pka` tool call fail and the refusal would propagate as-is. There is
therefore nothing on the Chemclaw3 side to run and no second copy to disagree. `PKA_REFUSAL_CASES`
below pins that this server's own `predict_logd` refuses on exactly the inputs `predict_pka` does,
which is the behaviour Chemclaw3 actually depends on (a refusal it relays, not one it decides).
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


# A frozen `PkaResult` (never recomputed by `predict_pka` — see the module docstring) paired with
# the pH to compose at, and the expected `predict_logd` outcome: either `(clogp, log_d)` or a
# substring that must appear in the raised `CalculationDomainError`. Every non-`None` numeric pair
# and every error substring was produced by feeding the identical frozen inputs to Chemclaw3's
# `logd_from_pka`.
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
    """One row of the arithmetic half of the contract, on a frozen (never recomputed) pKa.

    `predict_pka` is monkeypatched rather than called, so this pins the domain-check and
    Henderson-Hasselbalch arithmetic alone — the piece duplicated across the repository boundary —
    with none of GFN2-xTB's run-to-run float noise able to move a comparison this exact.
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

    This is single-sided by construction (see the module docstring): Chemclaw3 has no local pKa
    engine to run these SMILES through and would see this same refusal arrive from a real
    `predict_pka` call, so pinning it here is pinning the contract Chemclaw3 actually depends on.
    """
    with pytest.raises((ValueError, CalculationDomainError), match=substring):
        predict_logd(LogdInput(smiles=smiles, ph=7.4))
