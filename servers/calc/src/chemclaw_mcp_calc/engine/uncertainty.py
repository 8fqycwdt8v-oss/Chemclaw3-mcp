"""One shape for "how much should this number be trusted", plus the refusal that is its other end.

Ported from Chemclaw3's `science/calc/uncertainty.py` without the calibration-ledger parts, which
stay there; the result shapes must match what Chemclaw3's clients parse.

- `uncertainty` and `method`: how wrong the number is likely to be, and whether that is a
  published constant (`reported`) or carried through arithmetic (`propagated`).
- `in_domain`: whether the model can speak about this molecule at all; out of domain, an error
  bar is meaningless rather than wide. `None` means not assessed, which is not `True`.

Only the **structural** domain is asserted (what the model's terms are defined over), since no
training set ships to derive a statistical one from.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from rdkit import Chem

__all__ = ["CalculationDomainError", "Estimate", "Method", "structural_domain"]

# How an uncertainty was arrived at: a paper's test-set spread or one measured on this chemistry.
Method = Literal["reported", "propagated", "none"]

# Elements ESOL-style organic models have terms for; outside this set the equation does not apply.
_ORGANIC_ELEMENTS = frozenset({"H", "B", "C", "N", "O", "F", "Si", "P", "S", "Cl", "Br", "I"})

# Reviewer-facing prose per `Method`, kept beside it so a method never renders as empty.
_METHOD_PROSE: dict[Method, str] = {
    "reported": "the model's own reported error",
    "propagated": "propagated from the inputs",
    "none": "no uncertainty established",
}


class CalculationDomainError(ValueError):
    """A calculator refuses a molecule it cannot speak about, and says why.

    `in_domain=False` means "do not trust this number"; this means "there is no number" (no
    protonatable nitrogen, no basic pKa). A plain `ValueError` so `connector_app` passes the
    explanation to the model verbatim instead of an opaque error.
    """


class Estimate(BaseModel):
    """A number, its uncertainty, where that uncertainty came from, and whether to trust it at all.

    Deliberately not a replacement for the calculators' own result models: those carry the domain
    fields a chemist reads (a pKa's site, a solubility's model id), and flattening them into one
    generic envelope would lose that. This is the *uniform* part, produced beside them, so a skill,
    a note writer or a retrieval excerpt has one shape to consult regardless of which calculator
    answered.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: float
    unit: str = Field(min_length=1)
    # None when nothing about this prediction's error is known. Distinct from 0.0, which would
    # claim the prediction is exact.
    uncertainty: float | None = None
    method: Method = "none"
    # None = no declared domain: unanswered, not "yes".
    in_domain: bool | None = None
    # Why not, in a chemist's terms, one reason per failed check. Empty when in domain or unknown.
    domain_reasons: tuple[str, ...] = ()

    @property
    def trustworthy(self) -> bool:
        """Whether a consumer may use this number without a human looking at it first.

        Requires an affirmative domain answer: `None` (nobody checked) does not count.
        """
        return self.in_domain is True

    def render(self, *, fmt: str = ".6g") -> str:
        """This number and how far to trust it, as one inline fragment of a note body.

        Inline, never a footer, because retrieval excerpts are body prefixes. An in-domain estimate
        adds no domain remark; out-of-domain and not-assessed are both spelled out.

        Args:
            fmt: Format spec for the value and its uncertainty; the caller owns precision.
        """
        number = f"{self.value:{fmt}}"
        if self.uncertainty is not None:
            number += f" ± {self.uncertainty:{fmt}}"
        text = f"{number} {self.unit} ({_METHOD_PROSE[self.method]})"
        if self.in_domain is None:
            return f"{text}; applicability not assessed"
        if not self.in_domain:
            return f"{text}; OUT OF DOMAIN — {'; '.join(self.domain_reasons)}"
        return text


def structural_domain(mol: Chem.Mol) -> tuple[bool, tuple[str, ...]]:
    """Whether a molecule is the kind of structure an organic property model is defined over.

    One component (no term for a counter-ion), neutral (Crippen terms are for neutral species), and
    organic elements only (otherwise RDKit sums whatever fragments it recognises).

    Returns `(in_domain, reasons)`; `reasons` is empty when in domain.
    """
    reasons: list[str] = []
    if len(Chem.GetMolFrags(mol)) > 1:
        reasons.append(
            "multi-component structure (salt, co-crystal or mixture); the model describes one "
            "molecule and has no term for the counter-ion"
        )
    charge = Chem.GetFormalCharge(mol)
    if charge != 0:
        reasons.append(
            f"net formal charge {charge:+d}; the descriptors are parameterised for neutral species "
            "and an ionised form is a different physical quantity"
        )
    foreign = sorted({a.GetSymbol() for a in mol.GetAtoms()} - _ORGANIC_ELEMENTS)
    if foreign:
        reasons.append(
            f"non-organic element(s) {', '.join(foreign)}; the descriptor contributions are not "
            "defined for them and RDKit sums whatever it recognises rather than refusing"
        )
    return not reasons, tuple(reasons)
