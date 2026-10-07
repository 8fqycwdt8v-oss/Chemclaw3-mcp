"""One request model for every xTB task, and the one `calc_version` / `CalculationKey` derivation.

Per-task identity is how a cache goes wrong: someone adds a knob and forgets to key on it, and the
next run silently serves a result computed under the old setting. `XtbSpec` holds every field that
can move a number and `cache_key` is derived from `model_dump()`, so a new field is keyed by
construction. The structure is passed separately — it is the subject, not a setting — and keyed by
`structure_id`, so identical geometries share an entry.

`calc_version()` reads the installed tblite, RDKit and scipy versions, a Hamiltonian revision,
and `xtb --version` when the binary runs; `OptSpec` adds geomeTRIC and `CrestSpec` crest. None of
these exist on a Chemclaw3 pod, so every tool returns the string rather than leaving it to be
re-derived.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, Field, field_validator

from chemclaw_mcp_calc.engine import crest_cli, xtb_cli
from chemclaw_mcp_calc.engine.config import settings
from chemclaw_mcp_calc.engine.key import CalculationKey
from chemclaw_mcp_calc.engine.solvents import canonical_solvent
from chemclaw_mcp_calc.engine.structure import Structure
from chemclaw_mcp_calc.engine.xtb_engine import engine_version

__all__ = ["Backend", "CrestSpec", "XtbSpec", "XtbTask", "backend_version", "resolve_backend"]

# Which implementation runs a task. `tblite` is the in-process library; `xtb` is the binary, which
# carries ANCopt and GFN-FF.
Backend = Literal["tblite", "xtb"]


def resolve_backend(preferred: str | None = None) -> Backend:
    """Pick a concrete backend now, so `auto` never reaches a `calc_version`.

    "auto" would let two deployments share cache rows computed by different programs.
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


# The xTB tasks this server can run: `sp` single point; `properties` the same SCF's charges, bond
# orders, dipole and orbitals; `fukui` three single points; `opt` relaxation; `hess` the Hessian.
# `atomic` (binary-only per-atom panel) and `surface` (a second xtb run) key on the binary's build.
# `conformers` and `complex` are crest's (`CrestSpec`). There is no `scan`: a scan point is an
# ordinary constrained `opt` and keys as one.
XtbTask = Literal[
    "sp", "properties", "fukui", "atomic", "surface", "opt", "hess", "conformers", "complex"
]


# Which backend a task is allowed to name, decided once per task. `sp`, `properties` and `fukui`
# have no binary code path, so their `calc_version` must name tblite even where `auto` would pick
# the binary. `opt` and `hess` really dispatch on `engine`, and crest's tasks key on crest.
_FIXED_BACKEND: dict[str, Backend] = {
    "sp": "tblite",
    "properties": "tblite",
    "fukui": "tblite",
    "atomic": "xtb",
    "surface": "xtb",
}


class XtbSpec(BaseModel):
    """The settings of one xTB calculation — everything except its subject structure.

    Defaults come from config via `default_factory` (not a class-definition-time snapshot), so an
    ENV override applies to specs built afterwards.

    **Task-specific settings live in subclasses** (`PropertiesSpec`, `OptSpec`, `HessianSpec`,
    `EnsembleSpec`, `ComplexSpec`), not in this model. A subclass inherits `cache_key` unchanged and
    its fields are keyed automatically, because the key is derived from `model_dump()` — so the
    invariant survives while a single point's key stays free of a temperature it does not have.
    `ThermoSpec` stood in that list and is **Chemclaw3's**, not this repository's: the RRHO
    arithmetic stayed there with `compute_thermochemistry`, so naming it here pointed a reader at a
    class this package does not define.
    """

    task: XtbTask
    # GFN parametrization, e.g. "GFN2-xTB". Part of `calc_version`, not `params`: it identifies the
    # method, which is what a calculator version means.
    method: str = Field(default_factory=lambda: settings.xtb_method)
    # Which implementation runs it; in `calc_version`, since backends give different numbers.
    engine: Backend = Field(default_factory=resolve_backend)
    # ALPB implicit solvent name, or None for gas phase. Stored canonicalised, because it is hashed
    # into `params_hash`: see the validator below.
    solvent: str | None = None
    # xtb's `--acc` numerical accuracy. A spec field because it moves the answer; keyed only where
    # the binary runs (`unkeyed_fields`), since tblite has no equivalent and crest gets none.
    accuracy: float = Field(default_factory=lambda: settings.xtb_cli_accuracy, gt=0)

    @field_validator("solvent")
    @classmethod
    def _solvent_must_be_parameterised(cls, value: str | None) -> str | None:
        """Refuse a solvent ALPB has no parameters for, here rather than inside the SCF, and
        canonicalise the spelling that survives.

        Refusing at construction gives a chemist-readable error before any work (e.g. "2-MeTHF" has
        no GFN2 parameters). The canonical name is kept because the value is hashed into
        `params_hash`: spelling variants of one solvent must be one cache row. Alias groups are
        checked against tblite in `tests/test_solvents.py`.
        """
        return None if value is None else canonical_solvent(value)

    def for_structure(self, structure: Structure) -> Self:
        """The spec that will actually run for `structure`, backend included.

        A task with one implementation gets it (`_FIXED_BACKEND`). Open-shell systems then fall back
        to the in-process backend, which applies spin polarization; the binary's `--spinpol` is
        unusable in the pinned build. An open-shell `atomic` spec therefore resolves to a backend
        that cannot serve it, and `xtb_atomic` refuses it in words. Resolving here keeps
        `calc_version` naming what ran. Idempotent.
        """
        fixed = _FIXED_BACKEND.get(self.task)
        spec = (
            self
            if fixed is None or fixed == self.engine
            else self.model_copy(update={"engine": fixed})
        )
        if spec.engine == "xtb" and structure.uhf:
            return spec.model_copy(update={"engine": "tblite"})
        return spec

    def calc_version(self) -> str:
        """What actually computes this spec, versioned — half the staleness guard.

        Every override obeys one rule: name every program whose output survives into the stored
        payload, and no program that does not run. The other half is `key.CALCULATION_EPOCH`, folded
        in by `CalculationKey.build`, which moves when *our* code changes.
        """
        return f"{self.method}+{self.engine}+{backend_version(self.engine)}"

    def unkeyed_fields(self) -> set[str]:
        """Fields that must *not* enter `params` — because they are keyed elsewhere, or inert here.

        `task`, `method` and `engine` are already in the key name or `calc_version`. `accuracy` is
        inert for tasks the binary does not run, which depends on the resolved backend (hence an
        instance method). A field is excluded only where it provably cannot reach the calculation: a
        false hit serves a wrong number, a false miss only costs CPU. Overriding this rather than
        `cache_key` keeps keying by construction the default.
        """
        unkeyed = {"task", "method", "engine"}
        if self.engine != "xtb":
            unkeyed.add("accuracy")
        return unkeyed

    def cache_key(self, structure: Structure) -> CalculationKey:
        """The versioned identity of running this spec on `structure`.

        `calc_version` carries the method and the build that runs it; every other field lands in
        `params` via `model_dump()`.
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

    Keys on crest's build (the program that produced the ensemble), drops `engine` from the key, and
    makes `for_structure` a no-op, since crest runs whatever `engine` says. So an open-shell CREST
    search has no spin-polarization fallback. A subclass that does run `engine` puts it back
    (`ComplexSpec` in `crest_search`).

    A CREST key promises the settings, not the ensemble: metadynamics is stochastic and no seed is
    set, so two runs of one spec may differ. Reproducibility comes from the caller's cache (first
    writer wins); do not read a small energy difference between deployments as physical.
    """

    def for_structure(self, structure: Structure) -> Self:
        """No-op: there is no second backend to fall back to (see the class docstring)."""
        return self

    def unkeyed_fields(self) -> set[str]:
        """`accuracy` goes too: `crest_cli` hands the search no `--acc`, whatever `engine` says."""
        return {*super().unkeyed_fields(), "accuracy"}

    def calc_version(self) -> str:
        """Keyed on crest's build, because crest is what runs.

        Answers `"absent"` where the binary is missing; that never reaches a stored row because
        every caller, and `calculation_key`, refuses first (`crest_search.require_crest`).
        """
        return f"{self.method}+crest-{crest_cli.binary_version()}"
