"""One request model for every xTB task, and the one `calc_version` / `CalculationKey` derivation.

Why one model. Each xTB task (single point, electronic properties, Fukui indices, optimization,
Hessian) needs the same question answered: *what identifies this calculation?* Answering it per task
is how a cache goes wrong — someone adds a knob and forgets to key on it, and the next run silently
serves a result computed under the old setting. `XtbSpec` holds every field that can move a number,
and `cache_key` is written once over `model_dump()`, so a new field is keyed by construction rather
than by review.

The structure is *not* part of the spec: it is passed to `cache_key` separately because it is the
calculation's subject, not its settings — and because keying on `Structure.structure_id` rather than
on "this SMILES with that embedding seed" is what lets an identical geometry from any source share
an entry.

**This module is the reason the port happened.** `calc_version()` reads the tblite and RDKit
distribution versions, a Hamiltonian-revision constant, and — when the backend resolves to the
binary — `xtb --version`. None of those exist on a Chemclaw3 pod after the split. Every tool here
therefore returns the string rather than leaving it to be re-derived.

**`CrestSpec` is here because crest now runs here.** Its whole content is "key on crest's build
instead of the engine's", which is the same rule stated for a different program: name what ran.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, Field, field_validator

from chemclaw_mcp_calc.engine import crest_cli, xtb_cli
from chemclaw_mcp_calc.engine.config import settings
from chemclaw_mcp_calc.engine.key import CalculationKey
from chemclaw_mcp_calc.engine.solvents import require_supported_solvent
from chemclaw_mcp_calc.engine.structure import Structure
from chemclaw_mcp_calc.engine.xtb_engine import engine_version

__all__ = ["Backend", "CrestSpec", "XtbSpec", "XtbTask", "backend_version", "resolve_backend"]

# Which implementation runs a task. `tblite` is the in-process library; `xtb` is the binary, which
# carries ANCopt and GFN-FF.
Backend = Literal["tblite", "xtb"]


def resolve_backend(preferred: str | None = None) -> Backend:
    """Pick a concrete backend now, so `auto` never reaches a `calc_version`.

    A version containing "auto" would mean different things on two deployments, and they would
    silently share cache entries and ledger rows computed by different programs. Resolving at spec
    construction keeps the string honest about what actually ran.
    """
    choice = preferred or settings.xtb_engine
    if choice in ("xtb", "tblite"):
        return "xtb" if choice == "xtb" else "tblite"
    return "xtb" if xtb_cli.is_available() else "tblite"


def backend_version(backend: Backend) -> str:
    """The build of `backend`, for `calc_version`. An upgrade must recompute."""
    if backend == "xtb":
        return f"xtb-{xtb_cli.binary_version()}/{engine_version()}"
    return engine_version()


# The xTB tasks this server can run. `sp` is a plain single point; `properties` reads the same SCF's
# charges, bond orders, dipole and orbitals; `fukui` runs three single points (N, N-1, N+1
# electrons) for the condensed Fukui indices; `opt` relaxes to a minimum; `hess` is the Hessian and
# the thermochemistry over it.
#
# `conformers` and `complex` are crest's (`CrestSpec` below); the rest are xTB's.
#
# Chemclaw3's own literal additionally carries `scan`, and that one is deliberately absent: a scan
# is a *sweep*, and this server exposes only its point — which is an ordinary constrained `opt` and
# keys as one, so a scan point computed here and a hand-written constrained relaxation of the same
# geometry share a cache row rather than sitting in two. A `xtb.scan@...` key would name a
# calculation this server never runs.
XtbTask = Literal["sp", "properties", "fukui", "opt", "hess", "conformers", "complex"]


class XtbSpec(BaseModel):
    """The settings of one xTB calculation — everything except its subject structure.

    Defaults come from config via `default_factory` (not a class-definition-time snapshot), so an
    ENV override applies to specs built afterwards.

    **Task-specific settings live in subclasses** (`OptSpec`, `HessianSpec`, `ThermoSpec`), not in
    this model. A subclass inherits `cache_key` unchanged and its fields are keyed automatically,
    because the key is derived from `model_dump()` — so the invariant survives while a single
    point's key stays free of a temperature it does not have.
    """

    task: XtbTask
    # GFN parametrization, e.g. "GFN2-xTB". Part of `calc_version`, not `params`: it identifies the
    # method, which is what a calculator version means.
    method: str = Field(default_factory=lambda: settings.xtb_method)
    # Which implementation runs it. Also part of `calc_version` and for the same reason: two
    # backends produce different numbers for the same request, so they are different calculator
    # versions, not different parameters of one.
    engine: Backend = Field(default_factory=resolve_backend)
    # ALPB implicit solvent name, or None for gas phase.
    solvent: str | None = None

    @field_validator("solvent")
    @classmethod
    def _solvent_must_be_parameterised(cls, value: str | None) -> str | None:
        """Refuse a solvent ALPB has no parameters for, here rather than inside the SCF.

        **Not in Chemclaw3's copy of this model**, and added deliberately rather than by accident of
        porting. Over there the check is a durable-job *precondition*, evaluated in the chat service
        before a workflow starts; there are no durable jobs here, so without this the refusal would
        happen either as tblite's "String value for epsilon was not found among database of
        solvents" (an implementation detail, not a mistake a chemist can act on) or, on the binary
        backend, minutes later inside a subprocess. The measured case is "2-MeTHF", among the most
        common process solvents there is and not one GFN2-xTB has parameters for.
        """
        require_supported_solvent(value)
        return value

    def for_structure(self, structure: Structure) -> Self:
        """The spec that will actually run for `structure`, backend included.

        **Open-shell systems fall back to the in-process backend**, whatever was configured, and the
        reason is measured. GFN2 without a spin-polarization term does not stabilize an open shell
        at all — it put triplet O2 *above* singlet — so `xtb_engine` enables that contribution
        whenever there are unpaired electrons. The `xtb` 6.6.1 binary cannot: its `--spinpol` is
        killed by the OOM killer in that build. Running a radical through it would silently
        reintroduce exactly the physics error that fix removed.

        Resolving here rather than at the call site is what keeps `calc_version` honest: it is
        derived from the *returned* spec, so a calculation computed by tblite is recorded as
        tblite's even when the deployment prefers the binary. Idempotent, so callers may apply it
        more than once.
        """
        if self.engine == "xtb" and structure.uhf:
            return self.model_copy(update={"engine": "tblite"})
        return self

    def calc_version(self) -> str:
        """What actually computes this spec, versioned — half the staleness guard, and the half
        only this process can see.

        **The rule, which every override obeys: name every program whose output survives into the
        stored payload, and no program that does not run.** A version that named the wrong program
        is one that survives an upgrade to the right one, and one that omitted the right program
        serves one program's number as another's — the same defect from either side.

        **Only half**, because every name in this string belongs to somebody else's program. Nothing
        here moves when *our* code changes — which is how a fix to the linear-rotor term in
        `xtb_thermo` left every `xtb.hess` row on disk serving the entropy and free energy it
        computed wrongly. `key.CALCULATION_EPOCH` is the other half and covers exactly that; it is
        folded into the key by `CalculationKey.build`, so it is not something a spec has to remember
        to name.
        """
        return f"{self.method}+{self.engine}+{backend_version(self.engine)}"

    @classmethod
    def unkeyed_fields(cls) -> set[str]:
        """Fields that must *not* enter `params` — because they are keyed elsewhere.

        `task` names the calculation type, and `method`/`engine` are already in `calc_version`, so
        all three would be recorded twice.

        Overriding this rather than overriding `cache_key` is deliberate: the key derivation stays
        in one place, so a new field is still keyed by construction and *excluding* one is the
        visible, deliberate act rather than the silent default.
        """
        return {"task", "method", "engine"}

    def cache_key(self, structure: Structure) -> CalculationKey:
        """The versioned identity of running this spec on `structure`.

        `calc_version` carries the method *and* the build of whatever runs it, so a tblite, xtb or
        RDKit upgrade recomputes on the Chemclaw3 side rather than serving a value the new stack
        would not reproduce. Everything else in the spec lands in `params` automatically — that is
        the whole point of deriving the key from `model_dump()`.
        """
        resolved = self.for_structure(structure)
        if resolved is not self:
            return resolved.cache_key(structure)
        return CalculationKey.build(
            calc_type=f"xtb.{self.task}",
            calc_version=self.calc_version(),
            inputs={
                "structure": structure.structure_id,
                "charge": structure.charge,
                "multiplicity": structure.multiplicity,
            },
            params=self.model_dump(exclude=self.unkeyed_fields()),
        )


class CrestSpec(XtbSpec):
    """Base of the specs whose work is done by `crest`, not by `engine`.

    Two things are wrong for a CREST search if it inherits `XtbSpec` unchanged, and both are key
    defects rather than cosmetic ones.

    **CREST's own build would be in no key.** `calc_version` names the tblite/xtb build, so
    upgrading crest — the program that actually produced the ensemble — would serve every stored
    ensemble unchanged.

    **`engine` would be inherited but never honoured.** A search calls `crest_cli.run` whatever it
    says, so a spec could be keyed as `tblite` while crest did the work — which `for_structure`
    made routine rather than hypothetical, because it rewrites `engine` to `tblite` for any
    open-shell input.

    So `engine` is dropped from this key and `for_structure` is a no-op. Note what the second one
    means and does not mean: an open-shell CREST search is **not** protected by the
    spin-polarization fallback, because there is nowhere to fall back to — crest has no in-process
    equivalent. That is a real limitation of radical conformer searches, and it is stated instead of
    hidden behind a key that claimed tblite had run.

    **What the drop is not: a claim that backends do not belong in keys.** It is the same rule
    `XtbSpec.calc_version` states, applied to a spec whose numbers all come from crest — name what
    ran. A subclass that *does* run `engine` therefore has to put it back, and `ComplexSpec` in
    `crest_search` is one.
    """

    def for_structure(self, structure: Structure) -> Self:
        """No-op: there is no second backend to fall back to (see the class docstring)."""
        return self

    def calc_version(self) -> str:
        """Keyed on crest's build, because crest is what runs.

        Answers `"absent"` where the binary is missing — which is this image today and Chemclaw3's
        too. That string never reaches a stored row, because every caller refuses before computing
        (`crest_search.require_crest`); it is visible only if someone asks for the *identity* of a
        search that cannot run, and `calculation_key` refuses that for the same reason.
        """
        return f"{self.method}+crest-{crest_cli.binary_version()}"
