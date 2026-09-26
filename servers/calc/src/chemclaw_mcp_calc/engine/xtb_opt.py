"""GFN2-xTB geometry optimization.

The first task whose *output* is a geometry. Everything before it — the single point, the electronic
properties, the Fukui indices, the pKa acid branch — describes whatever conformer RDKit happened to
embed and MMFF happened to relax; this module produces a stationary point of the surface those
numbers are actually computed on, which is the precondition for a Hessian (`xtb_thermo`).

The in-process optimizer is **geomeTRIC**, driven by tblite's **analytic** gradient over
delocalised internal coordinates (TRIC). It replaced ~330 lines of
`scipy.optimize.minimize(method="L-BFGS-B")` run with *both* of its own stopping tests disabled
(`gtol=0.0`, `ftol=0.0`), convergence enforced by a `StopIteration`-raising callback, over a
hand-built per-coordinate trust region and a Lindh pairwise model Hessian (an `anc.py` module,
deleted with it). That module's own docstring stated the gap in the library's favour — "a full Lindh
model with angle and torsion terms would do better, at the cost of primitive-internal machinery and
a Wilson B matrix" — and measured its preconditioner at about **2x** against ANCopt's 8-11x. Atoms
are frozen as Cartesian constraints in the coordinate system rather than as optimizer bounds.

**What the swap is worth is a measured capability rather than a speedup.** Driven against a worktree
at the commit before it, so only the optimizer differs: water, ethanol, acetic acid and benzene all
relax to the same energy within 0.002 kcal/mol, in the same or fewer cycles — and **aspirin, which
the old optimizer refused**, having spent the whole 780 s inline budget without converging, relaxes
in 320.6 s and 29 cycles. Wall clocks are not quoted as a ratio: every run shared a four-core box,
and the noise is larger than the difference.

**geomeTRIC is used at `geometric.optimize.Optimize`, never at `run_optimizer`.** The driver is a
command-line program wearing a function's clothes, and that was measured rather than inferred: one
call to `run_optimizer` writes `<prefix>.log`, `<prefix>.tmp/` and `<prefix>_optim.xyz` into the
process's working directory, and replaces the **root logger's handlers** with its own stream and
*file* handlers — permanently, from inside a tool call, in a server whose log configuration
`connector_app` owns. `Optimize` is the optimizer underneath it: driven with a temporary directory
it writes nothing outside that directory and leaves the root logger untouched (measured in both
directions).

**Ported without `run_cached_optimization`,** and without the `geometry.record_optimization`
cross-method pointer it wrote to on every miss: both are store operations, and this server has no
store. The optimized `Structure` still carries `origin` — the key of the calculation that produced
it — so lineage survives the removal of the thing that used to persist it.
"""

from __future__ import annotations

import logging
import tempfile
from importlib.metadata import version
from typing import Any, Literal, Self

import numpy as np
from geometric.engine import Engine
from geometric.errors import GeomOptNotConvergedError
from geometric.internal import DelocalizedInternalCoordinates
from geometric.molecule import Elements, Molecule
from geometric.optimize import Optimize
from geometric.params import OptParams
from geometric.prepare import parse_constraints
from pydantic import Field

from chemclaw_mcp_calc.engine import xtb_cli
from chemclaw_mcp_calc.engine.budget import Deadline
from chemclaw_mcp_calc.engine.config import settings
from chemclaw_mcp_calc.engine.key import Keyed
from chemclaw_mcp_calc.engine.structure import Structure, structure_from_smiles
from chemclaw_mcp_calc.engine.xtb_engine import (
    ANGSTROM_TO_BOHR,
    HARTREE_TO_KCAL,
    Calculator,
    evaluate_point,
    make_calculator,
)
from chemclaw_mcp_calc.engine.xtb_spec import XtbSpec

# geomeTRIC reports its progress through `logging` at INFO: one line per optimizer cycle plus a
# Hessian-eigenvalue line beside it. That is a command-line progress report, and it is emitted on a
# logger this process does not own — `connector_app` owns the log configuration for every server in
# this fleet, which is why `CLAUDE.md` says not to call `basicConfig` in one. The records are
# stopped at geomeTRIC's own logger rather than reformatted at the root: `propagate = False` ends
# the walk there, and the `NullHandler` is what keeps "no handlers could be found" off stderr.
# Its *failures* do not travel this way — they are exceptions, and they are handled below.
logging.getLogger("geometric").addHandler(logging.NullHandler())
logging.getLogger("geometric").propagate = False

# Where geomeTRIC's adaptive trust radius starts, in Angstrom — its own default. `trust_radius` is
# the *ceiling* rather than the opening value, because starting at the ceiling gives up the
# adaptivity that is half of why an internal-coordinate optimizer is fast.
_OPENING_TRUST_RADIUS = 0.1

# A convergence criterion set wide enough not to bind. geomeTRIC requires **all five** of its
# criteria; this module's contract is one of them, the largest gradient component, and
# `_optimize_with_binary` re-verifies exactly that on whatever geometry a backend returns. So the
# energy and displacement criteria are opened up and the gradient is what decides. They cannot be
# dropped instead: there is no "converge on gmax alone" switch that does not also change what
# `maxiter` means.
_UNBOUNDED_CRITERION = 1.0

# geomeTRIC's constraint algorithm. **Not its default, and the difference is the whole constrained
# path.** Upstream ships `0`, the 2016 algorithm, whose own source comment says constraints are
# "satisfied slowly unless `enforce` is enabled"; `1` is the 2019 one, where they are "satisfied
# instantly" and `enforce` is unnecessary. Measured on `scan_point`'s ethanol dihedral, four atoms
# frozen, at this module's own 5.0e-04 Hartree/Angstrom target:
#
#   conmethod=0  E = -11.39379720  12 cycles  free-atom max |gradient| 1.014e-03  REFUSED
#   conmethod=1  E = -11.39383627  27 cycles  free-atom max |gradient| 3.773e-04  accepted
#
# Both held the frozen atoms exactly (1.4e-08 and 0.0 Angstrom of movement). The failure under `0`
# is not slack in the constraint, it is the *free* subspace being left unminimized — and it is not a
# convergence-tightness problem either: driving geomeTRIC's own target to a quarter and a tenth of
# ours moved the residual to 8.47e-04 and 8.83e-04, a plateau rather than a descent. `1` costs more
# than twice the cycles and reaches a **lower** energy, which is what "the free subspace was not
# minimized" looks like from the outside.
_CONSTRAINT_METHOD = 1

# How far geomeTRIC's reported final frame may sit from the engine's last evaluated geometry and
# still be the *same* point, in Angstrom. The optimizer stores its frames through its own
# Bohr-to-Angstrom constant, so one point reaches this module twice, ~1e-9 Angstrom apart (measured
# on ethanol: 1.1e-09). Three orders of magnitude above that round trip and far below any
# displacement a step makes (the smallest accepted step is ~1e-4), so a match is a round trip and a
# miss is a genuinely different geometry that has to be evaluated.
_SAME_POINT_ANGSTROM = 1e-6

__all__ = [
    "OptSpec",
    "OptimizationResult",
    "OptimizationSummary",
    "optimization_inputs",
    "optimize_structure",
    "optimizer_version",
]


def optimizer_version() -> str:
    """The installed geomeTRIC build, for the `calc_version` of anything it relaxes.

    **This module is the only importer of `geometric` in the tree**, deliberately (see the mypy
    override in the root `pyproject.toml`), so the one place allowed to ask the distribution its
    version is the one place allowed to run it.
    """
    return f"geometric-{version('geometric')}"


class OptSpec(XtbSpec):
    """Settings of one geometry optimization.

    Every field moves the result and therefore belongs in the key, which it reaches automatically —
    `XtbSpec.cache_key` derives from `model_dump()`, so a subclass field is keyed by construction
    exactly as a base field is. That is the whole reason the per-task settings live in subclasses
    instead of widening the base model: a single point's key has no business carrying a gradient
    tolerance.
    """

    task: Literal["opt"] = "opt"
    # Convergence criterion: the largest absolute gradient component, in Hartree/Angstrom, over the
    # atoms that are free to move.
    gradient_tolerance: float = Field(
        default_factory=lambda: settings.xtb_opt_gradient_tolerance, gt=0
    )
    # Optimizer cycles allowed before the relaxation is refused, on either backend.
    max_steps: int = Field(default_factory=lambda: settings.xtb_opt_max_steps, gt=0)
    # Ceiling on how far one optimizer step may move an atom, in Angstrom — geomeTRIC's `tmax`,
    # above an adaptive radius that starts at `_OPENING_TRUST_RADIUS`. A spec field rather than a
    # settings read inside the loop, because it moves the answer and a setting that moves the answer
    # belongs in the key: measured on ethanol, 0.35 and 0.05 relax to different geometries and
    # different energies — and a structure id is what every downstream key is built from.
    trust_radius: float = Field(default_factory=lambda: settings.xtb_opt_trust_radius, gt=0)
    # The convergence level passed to `xtb --opt`, which is where the binary's relaxation stops.
    # Keyed only when the binary is what runs (`unkeyed_fields`), because the in-process path stops
    # on `gradient_tolerance` instead and never sees this.
    opt_level: str = Field(default_factory=lambda: settings.xtb_cli_opt_level, min_length=1)
    # Indices of atoms held at their input positions. Empty for a free optimization.
    frozen_atoms: tuple[int, ...] = ()

    def for_structure(self, structure: Structure) -> Self:
        """Also resolve the backend a *constrained* optimization really runs on.

        Frozen atoms are expressible as optimizer bounds and not as an xtb flag without a control
        file, so `_optimize_with_binary` has always handed them straight to the in-process path.
        That fallback happened *after* the key was derived, so a scan point on a deployment with the
        binary was stored under a `calc_version` naming a program that had not run — the defect
        `_FIXED_BACKEND` describes for the fixed-backend tasks, in a third place, and the reason
        `opt_level` can be keyed by backend at all: the resolved engine has to be the truth.
        """
        if self.frozen_atoms and self.engine == "xtb":
            # Idempotent: the copy's engine is no longer the binary, so this arm runs once.
            return self.model_copy(update={"engine": "tblite"}).for_structure(structure)
        return super().for_structure(structure)

    def calc_version(self) -> str:
        """Name the optimizer too, because on this task the optimizer decides the answer.

        `XtbSpec.calc_version`'s rule is "name every program whose output survives into the stored
        payload, and no program that does not run", and for the in-process path the program that
        *is* the payload is geomeTRIC: what `optimize_geometry` and `relax_structure` store is a
        geometry, chosen by which stationary point the optimizer walked to. `engine_version()`
        named tblite, RDKit and scipy and did not name it — so a geomeTRIC upgrade would have moved
        every optimized geometry under an unchanged key, and Chemclaw3 would have served the old
        one forever while stamping new residuals into one calibration bucket with the old ones.
        Measured at `6c6a0eb`: `'geometric' in engine_version()` was `False`
        (`D-2026-09-16-the-optimizer-that-decides-the-geometry-is-not-in-the-version-string`).

        **Here and not in `engine_version()`, which is the repair this looks like.** That string is
        the *engine's*, shared by every tblite task, and geomeTRIC runs in none of the others: a
        single point, an electronic-properties panel, a Fukui triple and a Hessian are all evaluated
        at a geometry somebody hands them. Naming it there would have keyed those four on a program
        none of them runs, which is the second half of the rule above and the exact defect
        `_FIXED_BACKEND` exists for. This is `CrestSpec.calc_version`'s shape one task over: key on
        the build of the thing that actually did the work.

        **Conditional on the resolved backend, because `optimize_structure` is.** That function
        dispatches on `for_structure(...).engine` — `xtb` goes to ANCopt inside the binary, which
        `backend_version` already names and which does not touch geomeTRIC — and `cache_key`
        resolves before it asks for a version, so `self.engine` here is the one that will run. The
        shipped image takes the in-process path either way (it installs the binary and pins
        `CHEMCLAW_XTB_ENGINE=tblite`), so in every shipped configuration this string grows.

        **What it costs, stated rather than discovered:** every `xtb.opt` row on disk is now a
        miss, which is correct — those rows do not record the optimizer that produced them — and
        every downstream key built from an optimized `structure_id` was already going to move the
        moment the geometry did. `predict_pka` folds this string in through
        `pka.calc_version()`'s `opt-` segment, so its calibration ledger resets with it, on the
        same argument that function already makes for its own widening.
        """
        if self.engine == "xtb":
            return super().calc_version()
        return f"{super().calc_version()}+{optimizer_version()}"

    def unkeyed_fields(self) -> set[str]:
        """`opt_level` is keyed on the backend that reads it, and only that one.

        It is ANCopt's convergence level and is inert in-process, where geomeTRIC stops on
        `gradient_tolerance` instead. `engine` is already resolved by the time `cache_key` asks, so
        it cannot be excluded on a spec whose declared engine is not the one that will run.

        **There used to be a second knob here and it is gone.** `curvature_floor` was the floor the
        ANC preconditioner assumed for the directions its pairwise model could not see — bends and
        torsions — and it has no analogue in an optimizer that builds real internal coordinates.
        Removing it changes the *shape* of this spec and therefore of every optimization key, which
        is why it shipped in the same commit as the `_HAMILTONIAN_REVISION` bump rather than on its
        own: one invalidation, not two.
        """
        unkeyed = super().unkeyed_fields()
        if self.engine != "xtb":
            unkeyed.add("opt_level")
        return unkeyed


class OptimizationResult(Keyed):
    """A converged GFN2-xTB minimum, with what it took to get there.

    `structure` is the optimized geometry and is the value downstream tasks consume; it carries
    `origin`, the key of the calculation that produced it, so a thermochemistry result computed from
    it has its lineage recorded rather than implied.

    A *non*-converged optimization is never returned: it raises. A geometry that is not a stationary
    point produces frequencies, thermochemistry and reaction energies that all look ordinary and
    mean nothing, so the honest contract is that holding an `OptimizationResult` guarantees
    convergence.

    `max_gradient` is `None` for **GFN-FF only**, and that is the one case where the guarantee is
    worded differently rather than weakened: a force field has no tblite equivalent, so this module
    cannot re-evaluate its gradient, and convergence is xtb's own ANCopt convergence — required, not
    assumed.
    """

    smiles: str | None
    input_structure_id: str
    structure: Structure
    method: str
    # Which backend produced this geometry. Recorded because the two do not agree to the last
    # decimal, so a reader comparing two results needs to know they are comparable.
    engine: str
    solvent: str | None
    initial_energy_hartree: float
    energy_hartree: float
    # How much the relaxation was worth, in the unit a chemist reads. A large value on a supposedly
    # relaxed input means the starting geometry was misleading.
    relaxation_kcal: float
    # Optimizer *cycles*, which is what both backends count: geomeTRIC's accepted steps in-process,
    # ANCopt's cycles on the binary. It is not the number of single points — a rejected step costs a
    # gradient and advances no cycle — and the two used to be conflated, because the L-BFGS-B path
    # reported `outcome.nit` summed over its legs. `0` means the input was already a minimum and no
    # optimizer ran, which is the case that keeps a converged geometry byte-identical.
    steps: int
    # Largest absolute gradient component (Hartree/Angstrom) at the final geometry, over the free
    # atoms — the quantity `OptSpec.gradient_tolerance` bounds. `None` only for GFN-FF.
    max_gradient: float | None
    # Root-mean-square coordinate displacement, in Angstrom. Not Kabsch-aligned: the forces of a
    # molecule sum to zero, so an optimization introduces no net translation and this is a movement
    # measure, not a superposition.
    displacement_rms_angstrom: float
    frozen_atoms: list[int]


class OptimizationSummary(Keyed):
    """An optimization without its coordinates — what an agent can actually use.

    A model cannot read 3N Cartesians, and pasting them into a conversation is an unbounded-context
    failure. `structure_id` is what makes the geometry referable from a transcript, and `calc_key`
    is what makes it addressable in Chemclaw3's store.
    """

    smiles: str | None
    structure_id: str
    method: str
    engine: str
    solvent: str | None
    energy_hartree: float
    relaxation_kcal: float
    steps: int
    max_gradient: float | None
    displacement_rms_angstrom: float

    @classmethod
    def of(cls, result: OptimizationResult) -> OptimizationSummary:
        """Drop the geometry from a full result, keeping its address and its provenance."""
        return cls(
            calc_version=result.calc_version,
            calc_key=result.calc_key,
            smiles=result.smiles,
            structure_id=result.structure.structure_id,
            method=result.method,
            engine=result.engine,
            solvent=result.solvent,
            energy_hartree=result.energy_hartree,
            relaxation_kcal=result.relaxation_kcal,
            steps=result.steps,
            max_gradient=result.max_gradient,
            displacement_rms_angstrom=result.displacement_rms_angstrom,
        )


def optimization_inputs(smiles: str, solvent: str | None = None) -> tuple[OptSpec, Structure]:
    """The settings and the *starting* geometry `optimize_geometry` relaxes — see `xtb.sp_inputs`.

    `multiplicity=None` reads the SMILES' own explicit radical electrons instead of assuming a
    closed shell, which is what lets a radical be optimized at all — and what makes `OptSpec`'s
    open-shell fallback to the in-process backend fire, so the key names tblite rather than the
    configured binary.
    """
    return OptSpec(solvent=solvent), structure_from_smiles(smiles, multiplicity=None, optimize=True)


def optimize_structure(spec: OptSpec, structure: Structure) -> OptimizationResult:
    """Relax `structure` to a minimum, or raise if it does not converge.

    Dispatches on the spec's engine, after `for_structure` has had its say — an open-shell species
    goes to the in-process backend whatever was configured, because the binary cannot apply the
    spin-polarization term its energy needs.

    The `xtb` binary optimizes in approximate normal coordinates (ANCopt). It was measured 9-11x
    faster on drug-sized molecules than the *Cartesian L-BFGS-B* that used to be below it, and that
    figure has not been re-taken against geomeTRIC — `engine/xtb_cli.py` carries the table and says
    so. The in-process path is what the shipped image takes either way: it installs the binary and
    pins `CHEMCLAW_XTB_ENGINE=tblite`. The two backends are keyed separately because they do not
    produce identical geometries.

    Raises `ValueError` if the gradient is still above `spec.gradient_tolerance` after
    `spec.max_steps` — with the numbers, so the caller can tell "nearly there" from "this geometry
    is falling apart".
    """
    resolved = spec.for_structure(structure)
    if resolved.engine == "xtb":
        return _optimize_with_binary(resolved, structure)
    if resolved.method == "GFN-FF":
        # Named here rather than surfacing tblite's own "Method 'GFN-FF' is not available for this
        # calculator", which is true but says nothing about what to do. Reachable two ways: a
        # deployment without the binary, and a *radical*, which `for_structure` sends in-process
        # whatever was configured.
        raise ValueError(
            "GFN-FF is a force field and exists only in the xtb binary, which is "
            f"{'not installed' if not xtb_cli.is_available() else 'unavailable for this input'}"
            "; use a GFN method or install xtb"
        )
    return _optimize_with_library(resolved, structure)


class _TbliteEngine(Engine):  # type: ignore[misc]
    """geomeTRIC's engine interface over tblite's analytic gradient.

    The `coords -> {energy, gradient}` callable is the whole of what geomeTRIC asks for, which is
    what made this replacement a wiring job rather than a port: `evaluate_point` already had that
    shape and already returned an analytic gradient.

    **The unit boundary is here and nowhere else.** geomeTRIC works entirely in atomic units —
    coordinates in Bohr, gradient in Hartree/Bohr — and everything above `xtb_engine` works in
    Angstrom. Both conversions are the *same* constant applied in opposite directions, which is why
    neither of them gets a literal of its own.

    `evaluations` counts single points rather than optimizer cycles. The two differ by more than a
    constant — a rejected step costs a gradient and advances no cycle — and it is the single points
    that cost the seconds, so it is the number worth reporting when a relaxation was expensive.

    **It remembers the last point it evaluated, and is seeded with the input's.** The caller has
    already evaluated the input geometry to decide whether to optimize at all, and geomeTRIC's
    first request is exactly that geometry; after the optimizer returns, the caller re-verifies the
    final frame, which is exactly geomeTRIC's last request. Measured on ethanol, both were full
    SCFs — 9 single points for 6 steps — and the verification one ran with no `Deadline.check`, so
    the uninterruptible overrun past the budget could be two single points against a caller margin
    sized for one. `last` answers both without a second SCF.
    """

    def __init__(
        self,
        molecule: Any,
        calculator: Calculator,
        deadline: Deadline,
        start: tuple[np.ndarray, float, np.ndarray],
    ) -> None:
        super().__init__(molecule)
        self._calculator = calculator
        self._deadline = deadline
        self.evaluations = 0
        #: `(positions, energy, gradient)` in Angstrom and Hartree/Angstrom, as `evaluate_point`
        #: returns them — the most recent point this engine knows the answer at.
        self.last = start
        self._last_bohr = np.asarray(start[0], dtype=float).ravel() * ANGSTROM_TO_BOHR

    def calc_new(self, coords: Any, dirname: Any) -> dict[str, Any]:
        """One single point at `coords` (Bohr), as energy and gradient in atomic units."""
        flat = np.asarray(coords, dtype=float).ravel()
        if not np.array_equal(flat, self._last_bohr):
            # Per gradient rather than per cycle: `max_steps` bounds cycles, and one cycle on a
            # large substrate is unbounded in seconds — so a check outside the optimizer is exactly
            # the one that misses this. It is the same placement the L-BFGS-B objective used.
            self._deadline.check("geometry optimization")
            self.evaluations += 1
            positions = flat.reshape(-1, 3) / ANGSTROM_TO_BOHR
            energy, gradient, _ = evaluate_point(self._calculator, positions)
            self.last = (positions, energy, gradient)
            self._last_bohr = flat.copy()
        _, energy, gradient = self.last
        return {"energy": energy, "gradient": (gradient / ANGSTROM_TO_BOHR).ravel()}


def _geometric_molecule(numbers: np.ndarray, positions: np.ndarray) -> Any:
    """A geomeTRIC `Molecule` for `numbers` at `positions` (Angstrom).

    The element symbols come from geomeTRIC's own `Elements` table rather than from RDKit's, so the
    spelling is one the library certainly recognises; `build_topology` is what its coordinate
    system is built over.
    """
    molecule = Molecule()
    molecule.elem = [Elements[int(number)] for number in numbers]
    molecule.xyzs = [np.array(positions, dtype=float)]
    molecule.build_topology()
    return molecule


def _coordinate_system(molecule: Any, frozen_atoms: tuple[int, ...]) -> Any:
    """Delocalised internal coordinates (TRIC), with any frozen atoms as Cartesian constraints.

    `connect=False, addcart=True` is geomeTRIC's translation-rotation-internal setup, and it is the
    right one here rather than a default worth revisiting: a structure reaching this function may be
    a non-covalent complex (`combine_structures` builds them), and a coordinate system derived from
    bonded connectivity alone has nothing to describe the distance between two fragments with.

    Frozen atoms go through geomeTRIC's constraint *language*, which is 1-based text and is its only
    public entry point for one. That is a constraint on the coordinate system rather than a bound on
    the optimizer, which is a stronger statement than the equal-bounds trick it replaces: a frozen
    coordinate is removed from the space being searched instead of being allowed to press against a
    wall.

    `conmethod` is what makes that true rather than nearly true — see `_CONSTRAINT_METHOD`, which
    carries the measurement. It is passed only on this branch because it is a constraint algorithm
    and there are no constraints on the other one.
    """
    if not frozen_atoms:
        return DelocalizedInternalCoordinates(molecule, build=True, connect=False, addcart=True)
    listing = ",".join(str(index + 1) for index in sorted(frozen_atoms))
    constraints, values = parse_constraints(molecule, f"$freeze\nxyz {listing}\n")
    return DelocalizedInternalCoordinates(
        molecule,
        build=True,
        connect=False,
        addcart=True,
        constraints=constraints,
        cvals=values[0],
        conmethod=_CONSTRAINT_METHOD,
    )


def _optimizer_params(spec: OptSpec) -> Any:
    """geomeTRIC's parameters for one relaxation, with this module's contract expressed in them."""
    # geomeTRIC converges on atomic units; `gradient_tolerance` is Hartree/Angstrom. Same constant,
    # opposite direction, as in `_TbliteEngine.calc_new`.
    gmax = spec.gradient_tolerance / ANGSTROM_TO_BOHR
    return OptParams(
        maxiter=spec.max_steps,
        trust=min(_OPENING_TRUST_RADIUS, spec.trust_radius),
        tmax=spec.trust_radius,
        convergence_gmax=gmax,
        # `grms <= gmax` always holds, so setting them equal leaves the largest component as the
        # binding criterion — which is the one this module promises.
        convergence_grms=gmax,
        convergence_energy=_UNBOUNDED_CRITERION,
        convergence_drms=_UNBOUNDED_CRITERION,
        convergence_dmax=_UNBOUNDED_CRITERION,
    )


def _optimize_with_library(spec: OptSpec, structure: Structure) -> OptimizationResult:
    """Relax with geomeTRIC over tblite's analytic gradient.

    The only backend that can hold atoms fixed or describe an open shell, and — in an image without
    the `xtb` binary — the only backend at all. It is also the shipped path: the image installs
    `xtb` and pins `CHEMCLAW_XTB_ENGINE=tblite`.

    **The convergence check is this module's own, re-evaluated on the returned geometry**, exactly
    as `_optimize_with_binary` does it for the binary and for the same reason: the contract of this
    module is that holding an `OptimizationResult` guarantees `spec.gradient_tolerance` was met, and
    a backend converging to its own criteria must not quietly weaken that. It costs no extra
    gradient when the final frame is the engine's last evaluated point, which is the usual case,
    and one checked against the budget when it is not.
    """
    # A budget rather than a spec field, deliberately: it decides whether an answer comes back, not
    # what the answer is, so keying on it would fork the cache every time a deployment gave itself
    # more time. See `budget.Deadline` for why the transport cannot hold this clock instead.
    deadline = Deadline(settings.xtb_inline_timeout_seconds)
    numbers, positions = structure.arrays()
    frozen = np.zeros(len(numbers), dtype=bool)
    if spec.frozen_atoms:
        if max(spec.frozen_atoms) >= len(numbers) or min(spec.frozen_atoms) < 0:
            raise ValueError(f"frozen atom index out of range for {len(numbers)} atoms")
        frozen[list(spec.frozen_atoms)] = True
    if frozen.all():
        raise ValueError("every atom is frozen: there is nothing to optimize")

    calc = make_calculator(
        spec.method,
        numbers,
        positions,
        charge=structure.charge,
        uhf=structure.uhf,
        solvent=spec.solvent,
    )
    # The gradient of a frozen coordinate is zeroed rather than merely constrained: the convergence
    # test must measure the forces the optimizer is allowed to relieve, not the ones the constraint
    # is holding.
    free_mask = np.repeat(~frozen, 3)

    initial_energy, initial_gradient, _ = evaluate_point(calc, positions)
    # Seeded from the *input* geometry, so a structure that is already a minimum runs no optimizer
    # at all and comes back byte-identical. The loop used to be bounded only by the step count, so
    # it always ran and always moved something: re-optimizing a converged water shifted it 3e-4
    # Angstrom, and since a structure id is a hash of the coordinates, every pass minted a new id.
    # That silently forks the cache for every task keyed on a geometry. The gradient here costs
    # nothing: `evaluate_point` above already computed it.
    max_gradient = float(np.max(np.abs(np.where(free_mask, initial_gradient.ravel(), 0.0))))
    final = positions
    energy = initial_energy
    steps = 0
    if max_gradient > spec.gradient_tolerance:
        molecule = _geometric_molecule(numbers, positions)
        engine = _TbliteEngine(
            molecule, calc, deadline, (positions, initial_energy, initial_gradient)
        )
        coordinates = _coordinate_system(molecule, spec.frozen_atoms)
        try:
            with tempfile.TemporaryDirectory(prefix="chemclaw-geometric-") as scratch:
                # `print_info=False` suppresses the banner geomeTRIC writes about the coordinate
                # system it built; the scratch directory is where it would put a restart file.
                progress = Optimize(
                    np.asarray(positions, dtype=float).ravel() * ANGSTROM_TO_BOHR,
                    molecule,
                    coordinates,
                    engine,
                    scratch,
                    _optimizer_params(spec),
                    print_info=False,
                )
        except GeomOptNotConvergedError as error:
            raise ValueError(
                f"geometry optimization did not converge in {spec.max_steps} cycles "
                f"({engine.evaluations} gradient evaluations): {error}"
            ) from error
        # geomeTRIC's progress carries one frame per accepted cycle, the input included.
        steps = max(len(progress.xyzs) - 1, 1)
        final = np.array(progress.xyzs[-1], dtype=float)
        last_positions, last_energy, last_gradient = engine.last
        if np.max(np.abs(final - last_positions)) <= _SAME_POINT_ANGSTROM:
            # The engine's own geometry rather than geomeTRIC's round-tripped copy of it, so the
            # coordinates returned are exactly the ones whose gradient is checked below.
            final, energy, gradient = last_positions, last_energy, last_gradient
        else:
            deadline.check("geometry optimization")
            energy, gradient, _ = evaluate_point(calc, final)
        max_gradient = float(np.max(np.abs(np.where(free_mask, gradient.ravel(), 0.0))))

    if max_gradient > spec.gradient_tolerance:
        raise ValueError(
            f"geometry optimization did not converge in {steps} steps: "
            f"max |gradient| {max_gradient:.2e} > {spec.gradient_tolerance:.2e} "
            "Hartree/Angstrom"
        )

    key = spec.cache_key(structure)
    optimized = Structure(
        elements=structure.elements,
        positions=[[float(value) for value in row] for row in final],
        charge=structure.charge,
        multiplicity=structure.multiplicity,
        smiles=structure.smiles,
        origin=key.as_str(),
    )
    return OptimizationResult(
        calc_version=spec.calc_version(),
        calc_key=key.as_str(),
        smiles=structure.smiles,
        input_structure_id=structure.structure_id,
        structure=optimized,
        method=spec.method,
        engine=spec.engine,
        solvent=spec.solvent,
        initial_energy_hartree=initial_energy,
        energy_hartree=energy,
        relaxation_kcal=(initial_energy - energy) * HARTREE_TO_KCAL,
        steps=steps,
        max_gradient=max_gradient,
        displacement_rms_angstrom=float(np.sqrt(np.mean((final - positions) ** 2))),
        frozen_atoms=list(spec.frozen_atoms),
    )


def _optimize_with_binary(spec: OptSpec, structure: Structure) -> OptimizationResult:
    """Relax with `xtb --opt` (ANCopt), then verify convergence on our own criterion.

    The convergence check is deliberately *ours*, re-evaluated on the returned geometry rather than
    trusted from xtb's exit status: the contract of this module is that an `OptimizationResult`
    satisfies `spec.gradient_tolerance`, and a backend that converged to its own looser threshold
    must not quietly weaken that. It costs one gradient evaluation.

    Frozen atoms never arrive here: pinning coordinates is expressible as optimizer bounds but not
    as an xtb flag without writing a control file, which is exactly the input surface `xtb_cli`
    refuses to have — so `OptSpec.for_structure` resolves a constrained spec to `tblite` before
    dispatch. The fallback used to live *here*, after the key had been derived, which is how a
    constrained optimization came to be stored under a `calc_version` naming the binary that had
    not run.

    **GFN-FF is verified on its own surface**, because there is no other honest option: tblite has
    no force field, so re-evaluating the geometry in-process would test a GFN-FF minimum against a
    *GFN2* gradient — a different potential energy surface, on which a converged force-field
    geometry is simply not a stationary point. Measured, an octane relaxed by GFN-FF carries a GFN2
    max-gradient of 1.3e-2 against this module's 5e-4 target.
    """
    outcome = xtb_cli.run(
        structure,
        task="opt",
        method=spec.method,
        solvent=spec.solvent,
        accuracy=spec.accuracy,
        opt_level=spec.opt_level,
        max_cycles=spec.max_steps,
    )
    if outcome.structure is None:
        raise ValueError("xtb --opt produced no optimized geometry")
    key = spec.cache_key(structure)
    optimized = outcome.structure.model_copy(update={"origin": key.as_str()})
    if spec.method == "GFN-FF":
        return _force_field_result(spec, structure, optimized, outcome, key.as_str())
    initial, _, _ = _energy_and_gradient(spec, structure, structure)
    energy, gradient, _ = _energy_and_gradient(spec, structure, optimized)
    max_gradient = float(np.max(np.abs(gradient)))
    if max_gradient > spec.gradient_tolerance:
        raise ValueError(
            f"geometry optimization did not converge in {outcome.cycles} ANC cycles: "
            f"max |gradient| {max_gradient:.2e} > {spec.gradient_tolerance:.2e} "
            "Hartree/Angstrom"
        )
    _, positions = structure.arrays()
    final = np.array(optimized.positions)
    return OptimizationResult(
        calc_version=spec.calc_version(),
        calc_key=key.as_str(),
        smiles=structure.smiles,
        input_structure_id=structure.structure_id,
        structure=optimized,
        method=spec.method,
        engine=spec.engine,
        solvent=spec.solvent,
        initial_energy_hartree=initial,
        energy_hartree=energy,
        relaxation_kcal=(initial - energy) * HARTREE_TO_KCAL,
        steps=outcome.cycles or 0,
        max_gradient=max_gradient,
        displacement_rms_angstrom=float(np.sqrt(np.mean((final - positions) ** 2))),
        frozen_atoms=[],
    )


def _force_field_result(
    spec: OptSpec,
    structure: Structure,
    optimized: Structure,
    outcome: xtb_cli.CliResult,
    calc_key: str,
) -> OptimizationResult:
    """Package a GFN-FF relaxation, whose only convergence evidence is xtb's own.

    `outcome.cycles` is parsed from xtb's "CONVERGED AFTER" line, so requiring it is requiring the
    binary to say it converged — not inferring it from an exit code, which `xtb_cli` documents as
    unreliable. Without it there is no evidence at all, and the contract is that an
    `OptimizationResult` is a converged one.

    `initial_energy_hartree` equals the final energy because a force-field single point at the input
    geometry would be a second subprocess for a number nothing reads; the relaxation is reported as
    0.0 rather than invented.
    """
    if outcome.cycles is None:
        raise ValueError(
            "xtb --opt with GFN-FF did not report convergence, and a force-field geometry "
            "cannot be verified in-process (tblite has no GFN-FF): refusing to return it"
        )
    _, positions = structure.arrays()
    final = np.array(optimized.positions)
    return OptimizationResult(
        calc_version=spec.calc_version(),
        calc_key=calc_key,
        smiles=structure.smiles,
        input_structure_id=structure.structure_id,
        structure=optimized,
        method=spec.method,
        engine=spec.engine,
        solvent=spec.solvent,
        initial_energy_hartree=outcome.energy_hartree,
        energy_hartree=outcome.energy_hartree,
        relaxation_kcal=0.0,
        steps=outcome.cycles,
        max_gradient=None,
        displacement_rms_angstrom=float(np.sqrt(np.mean((final - positions) ** 2))),
        frozen_atoms=[],
    )


def _energy_and_gradient(
    spec: OptSpec, template: Structure, at: Structure
) -> tuple[float, np.ndarray, np.ndarray]:
    """Evaluate energy and gradient at `at`, using the in-process engine.

    Used to verify a binary-produced geometry against our own convergence criterion. Never reached
    for GFN-FF — that path returns before this, because substituting GFN2 here is what made a
    force-field optimization fail against the wrong surface.
    """
    numbers, _ = template.arrays()
    calc = make_calculator(
        spec.method,
        numbers,
        np.array(at.positions),
        charge=at.charge,
        uhf=at.uhf,
        solvent=spec.solvent,
    )
    return evaluate_point(calc, np.array(at.positions))
