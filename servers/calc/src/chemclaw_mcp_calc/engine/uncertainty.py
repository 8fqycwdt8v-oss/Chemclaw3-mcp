"""One shape for "how much should this number be trusted", plus the refusal that is its other end.

Ported from Chemclaw3's `chemclaw/science/calc/uncertainty.py`. **The port instruction said not to
bring this module across, and two thirds of it did not**: everything that reads or narrows against
the calibration ledger stayed behind, because the ledger stays in Chemclaw3. What is here is the
part `SolubilityResult.estimate` and every domain refusal are *made of* — drop it and the result
shapes this server returns stop matching the ones Chemclaw3's clients parse, which is the one
thing a replacement port may not do.

**Three questions, and they are genuinely different.**

*How wrong is this likely to be?* — `uncertainty`, and `method` says how it was obtained.
`reported` is the model's published error, a constant that knows nothing about *this* molecule.
`propagated` is an input's uncertainty carried through arithmetic (logD does this from the pKa
calibration). The distinction matters to a reviewer: a constant RMSE is a claim about a paper's
test set rather than about this system's chemistry.

*Was this molecule the kind of thing the model can speak about at all?* — `in_domain`. Separate
from the uncertainty because an out-of-domain prediction's error bar is not merely larger, it is
**meaningless**: extrapolating a linear fit does not widen its residual distribution, it leaves the
distribution the residuals were drawn from.

*Do we know?* — `None`, and it is not the same as `False`. A calculator with no declared domain
reports "unknown", which is the honest answer and the one that keeps a reviewer's attention.

**On what a domain check may honestly assert here.** The statistical applicability domain of a
fitted model needs its training set, and neither repository ships one. Inventing bounds and
labelling them "the training ranges" would be a fabricated threshold, which is worse than no check.
What *can* be asserted is the model's **structural** domain: what its terms are defined over at
all. ESOL is a linear equation in Crippen logP over neutral single-component organic molecules; it
is not that a salt is far from the training data, it is that the equation's inputs do not describe
a salt.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from rdkit import Chem

__all__ = ["CalculationDomainError", "Estimate", "Method", "structural_domain"]

# How an uncertainty was arrived at. Not decoration: it is the difference between "the paper that
# published this model measured this spread on its own test set" and "this deployment measured this
# spread on its own chemistry", and a reviewer weighs the two differently.
Method = Literal["reported", "propagated", "none"]

# Elements ESOL-style organic property models are parameterised over. Not a chemistry opinion — the
# Crippen contributions and the aromatic-proportion term are defined for organic structures, and a
# molecule outside this set is one the equation has no terms for.
_ORGANIC_ELEMENTS = frozenset({"H", "B", "C", "N", "O", "F", "Si", "P", "S", "Cl", "Br", "I"})

# How an uncertainty was obtained, in the words someone reviewing a merged note reads. Kept beside
# `Method` so the two cannot drift: a method with no prose here would render as an empty
# parenthetical, which is the silence this module exists to break.
_METHOD_PROSE: dict[Method, str] = {
    "reported": "the model's own reported error",
    "propagated": "propagated from the inputs",
    "none": "no uncertainty established",
}


class CalculationDomainError(ValueError):
    """A calculator refuses a molecule it cannot speak about, and says why.

    The refusal end of the same question `Estimate.in_domain` answers. `in_domain=False` means
    "here is a number, do not trust it"; this means "there is no number to give you" — a molecule
    with no protonatable nitrogen has no basic pKa, and returning one would be an invention rather
    than a poor estimate.

    **A `ValueError` and nothing more, which is a simplification the split made correct.** In
    Chemclaw3 this subclasses `ChemclawError`, a hierarchy that exists so an authorization layer can
    tell a domain refusal from an internal fault. Here the same job is done by
    `mcp_server_kit.connector_app`, which passes a `ValueError` to the model verbatim and replaces
    every other exception with a generic notice — so the narrower base is what actually carries the
    message across the wire. It matters: the measured failure this class was created for was
    `predict_pka`'s aliphatic-amine explanation, which names the Spearman -0.17 correlation and
    tells the chemist to measure it instead, reaching the model as an opaque "Error: Function
    failed." The answer then guessed the reason and presented the guess as a fact.
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
    # None = this calculator declares no applicability domain, so the question is unanswered rather
    # than answered "yes". A consumer that treats None as True is the bug this field exists to make
    # visible.
    in_domain: bool | None = None
    # Why not, in a chemist's terms, one reason per failed check. Empty when in domain or unknown.
    domain_reasons: tuple[str, ...] = ()

    @property
    def trustworthy(self) -> bool:
        """Whether a consumer may use this number without a human looking at it first.

        Requires an affirmative domain answer, not merely the absence of a negative one: `None`
        means nobody checked, and "nobody checked" must not read as "fine".
        """
        return self.in_domain is True

    def render(self, *, fmt: str = ".6g") -> str:
        """This number and how far to trust it, as one inline fragment of a note body.

        **Inline, and never a footer.** A retrieval excerpt is a blind character prefix of a note
        body, so a trust stanza appended below the value is cut from precisely the notes carrying
        the most prose, and survives only in the short ones that needed it least.

        **Silence is meaningful, in one direction only.** An in-domain estimate adds no domain
        remark, so a line without one says the check ran and passed. Both abnormal states are
        spelled out instead — including "not assessed", because a reader who cannot tell an unasked
        question from an answered one will read it as answered.

        Args:
            fmt: Format spec for the value and its uncertainty. The caller owns precision because
                significance is the calculator's own fact.
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

    Three checks, each following from what such a model *is* rather than from where its training set
    happened to fall — so they are defensible without the training data neither repository ships:

    - **One component.** A salt or a co-crystal is two species; a single-molecule descriptor
      equation has no term for the counter-ion, and the answer it returns describes neither.
    - **Neutral.** Crippen contributions and the aromatic-proportion term are parameterised for
      neutral species. A charged form's solubility is a different physical quantity, usually by
      orders of magnitude, which is exactly the size of error a 0.75-log RMSE hides.
    - **Organic elements only.** An organometallic or a boron cluster has no Crippen contribution to
      sum; RDKit returns a number regardless, assembled from whatever fragments it recognises.

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
