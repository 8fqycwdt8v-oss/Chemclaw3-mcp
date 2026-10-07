"""GFN2-xTB geometry optimization.

Produces a stationary point of the surface every other number is computed on — the precondition
for a Hessian (`xtb_thermo`). In-process, the optimizer is **geomeTRIC** over delocalised internal
coordinates (TRIC) driven by tblite's analytic gradient; frozen atoms are Cartesian constraints in
the coordinate system, not optimizer bounds.

geomeTRIC is used through `geometric.optimize.Optimize`, never `run_optimizer`: the driver writes
files into the working directory and replaces the root logger's handlers, while `Optimize` in a
temporary directory touches neither. Nothing is stored here; the optimized `Structure` carries
`origin` for lineage.
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

# geomeTRIC logs per-cycle progress at INFO on a logger this process does not own (`connector_app`
# owns log configuration). `propagate = False` stops it there and the `NullHandler` keeps "no
# handlers" off stderr; its failures arrive as exceptions instead.
logging.getLogger("geometric").addHandler(logging.NullHandler())
logging.getLogger("geometric").propagate = False

# geomeTRIC's opening trust radius, in Angstrom (its default); `trust_radius` is the ceiling, so the
# radius stays adaptive.
_OPENING_TRUST_RADIUS = 0.1

# Wide enough not to bind. geomeTRIC requires all five criteria; this module's contract is the
# largest gradient component alone, re-verified on the returned geometry, so the others are opened
# up (dropping them would change what `maxiter` means).
_UNBOUNDED_CRITERION = 1.0

# geomeTRIC's constraint algorithm: `1` (2019), not the default `0`, which satisfies constraints
# slowly and left the free subspace unminimized on constrained scans (a gradient plateau above
# tolerance). `1` costs more cycles and reaches the true constrained minimum.
_CONSTRAINT_METHOD = 1

# Distance, in Angstrom, within which geomeTRIC's final frame is the engine's last evaluated point
# (its own Bohr conversion leaves a ~1e-9 round-trip difference); far below any real step.
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

    This module is the only importer of `geometric`, so it alone asks its version.
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
    # Ceiling on one step's atom movement, in Angstrom (geomeTRIC's `tmax`). A spec field because it
    # changes which geometry is reached, and so belongs in the key.
    trust_radius: float = Field(default_factory=lambda: settings.xtb_opt_trust_radius, gt=0)
    # The `xtb --opt` convergence level. Keyed only when the binary runs (`unkeyed_fields`);
    # in-process stops on `gradient_tolerance`.
    opt_level: str = Field(default_factory=lambda: settings.xtb_cli_opt_level, min_length=1)
    # Indices of atoms held at their input positions. Empty for a free optimization.
    frozen_atoms: tuple[int, ...] = ()

    def for_structure(self, structure: Structure) -> Self:
        """Also resolve the backend a *constrained* optimization really runs on.

        Frozen atoms cannot be passed to the binary without a control file, so a constrained run
        goes in-process; resolving that here, before the key is derived, keeps `calc_version` naming
        the program that actually ran.
        """
        if self.frozen_atoms and self.engine == "xtb":
            # Idempotent: the copy's engine is no longer the binary, so this arm runs once.
            return self.model_copy(update={"engine": "tblite"}).for_structure(structure)
        return super().for_structure(structure)

    def calc_version(self) -> str:
        """Name the optimizer too, because on this task the optimizer decides the answer.

        `calc_version` names every program whose output survives into the payload, and only those.
        The stored geometry is chosen by geomeTRIC, so a geomeTRIC upgrade must be a cache miss. It
        is added here rather than to `engine_version()`, which also keys tasks that run no optimizer
        — the same shape as `CrestSpec.calc_version`. Conditional on the resolved backend: the
        binary path runs ANCopt, which `backend_version` already names. `predict_pka` folds this in
        via its `opt-` segment, so its calibration follows.
        """
        if self.engine == "xtb":
            return super().calc_version()
        return f"{super().calc_version()}+{optimizer_version()}"

    def unkeyed_fields(self) -> set[str]:
        """`opt_level` is keyed on the backend that reads it, and only that one.

        It is inert in-process, where geomeTRIC stops on `gradient_tolerance`. `engine` is already
        resolved when `cache_key` asks.
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
    # Optimizer *cycles* (geomeTRIC accepted steps, or ANCopt cycles), not single points. `0` means
    # the input was already a minimum and no optimizer ran, so the geometry is byte-identical.
    steps: int
    # Largest absolute gradient component (Hartree/Angstrom) at the final geometry, over the free
    # atoms — the quantity `OptSpec.gradient_tolerance` bounds. `None` only for GFN-FF.
    max_gradient: float | None
    # RMS coordinate displacement, in Angstrom; not Kabsch-aligned, since an optimization introduces
    # no net translation.
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

    `multiplicity=None` reads explicit radical electrons, so a radical can be optimized and the key
    names the in-process backend it falls back to.
    """
    return OptSpec(solvent=solvent), structure_from_smiles(smiles, multiplicity=None, optimize=True)


def optimize_structure(spec: OptSpec, structure: Structure) -> OptimizationResult:
    """Relax `structure` to a minimum, or raise if it does not converge.

    Dispatches on the spec's resolved engine: an open shell goes in-process whatever was configured,
    since the binary cannot apply the spin-polarization term. The binary uses ANCopt; the shipped
    image pins the in-process path. The backends are keyed separately because their geometries
    differ.

    Raises `ValueError` if the gradient is still above `spec.gradient_tolerance` after
    `spec.max_steps`, with the numbers, so the caller can tell "nearly there" from "falling apart".
    """
    resolved = spec.for_structure(structure)
    if resolved.engine == "xtb":
        return _optimize_with_binary(resolved, structure)
    if resolved.method == "GFN-FF":
        # Worded here instead of tblite's "not available" message. Reachable without the binary, or
        # for a radical, which always runs in-process.
        raise ValueError(
            "GFN-FF is a force field and exists only in the xtb binary, which is "
            f"{'not installed' if not xtb_cli.is_available() else 'unavailable for this input'}"
            "; use a GFN method or install xtb"
        )
    return _optimize_with_library(resolved, structure)


class _TbliteEngine(Engine):  # type: ignore[misc]
    """geomeTRIC's engine interface over tblite's analytic gradient.

    The unit boundary: geomeTRIC works in Bohr and Hartree/Bohr, everything above `xtb_engine` in
    Angstrom; one constant converts both ways. `evaluations` counts single points (what costs time),
    not cycles.

    Remembers the last point evaluated, seeded with the input's, so geomeTRIC's first request and
    the caller's final verification reuse an SCF instead of repeating it. `progress` reports
    evaluations, the largest free gradient and the tolerance to `Deadline.check`, so a stopped
    relaxation says how close it was.
    """

    def __init__(
        self,
        molecule: Any,
        calculator: Calculator,
        deadline: Deadline,
        start: tuple[np.ndarray, float, np.ndarray],
        *,
        free_mask: np.ndarray,
        tolerance: float,
    ) -> None:
        super().__init__(molecule)
        self._calculator = calculator
        self._deadline = deadline
        self._free_mask = free_mask
        self._tolerance = tolerance
        self.evaluations = 0
        #: `(positions, energy, gradient)` in Angstrom and Hartree/Angstrom, as `evaluate_point`
        #: returns them — the most recent point this engine knows the answer at.
        self.last = start
        self._last_bohr = np.asarray(start[0], dtype=float).ravel() * ANGSTROM_TO_BOHR

    def calc_new(self, coords: Any, dirname: Any) -> dict[str, Any]:
        """One single point at `coords` (Bohr), as energy and gradient in atomic units."""
        flat = np.asarray(coords, dtype=float).ravel()
        if not np.array_equal(flat, self._last_bohr):
            # Checked per gradient, since one cycle on a large substrate is unbounded in seconds.
            self._deadline.check("geometry optimization", self.progress)
            self.evaluations += 1
            positions = flat.reshape(-1, 3) / ANGSTROM_TO_BOHR
            energy, gradient, _ = evaluate_point(self._calculator, positions)
            self.last = (positions, energy, gradient)
            self._last_bohr = flat.copy()
        _, energy, gradient = self.last
        return {"energy": energy, "gradient": (gradient / ANGSTROM_TO_BOHR).ravel()}

    def progress(self) -> str:
        """How far this relaxation got, phrased to follow "stopped after" in a refusal."""
        _, _, gradient = self.last
        largest = float(np.max(np.abs(np.where(self._free_mask, gradient.ravel(), 0.0))))
        noun = "evaluation" if self.evaluations == 1 else "evaluations"
        return (
            f"{self.evaluations} gradient {noun} past the input geometry, with max |gradient| "
            f"{largest:.2e} Hartree/Angstrom at the last one against the {self._tolerance:.2e} "
            "it had to reach"
        )


def _geometric_molecule(numbers: np.ndarray, positions: np.ndarray) -> Any:
    """A geomeTRIC `Molecule` for `numbers` at `positions` (Angstrom).

    Element symbols come from geomeTRIC's own table; `build_topology` underlies its coordinates.
    """
    molecule = Molecule()
    molecule.elem = [Elements[int(number)] for number in numbers]
    molecule.xyzs = [np.array(positions, dtype=float)]
    molecule.build_topology()
    return molecule


def _coordinate_system(molecule: Any, frozen_atoms: tuple[int, ...]) -> Any:
    """Delocalised internal coordinates (TRIC), with any frozen atoms as Cartesian constraints.

    `connect=False, addcart=True` (translation-rotation-internal) also describes the separation of
    fragments in a non-covalent complex. Frozen atoms use geomeTRIC's 1-based constraint language,
    which removes them from the search space; `_CONSTRAINT_METHOD` applies only on that branch.
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

    The only backend that can hold atoms fixed or describe an open shell, and the shipped path.
    Convergence is re-checked on the returned geometry against `spec.gradient_tolerance`, which an
    `OptimizationResult` guarantees; free when the final frame is the last evaluated point.
    """
    # A budget, not a spec field: it decides whether an answer comes back, not what it is, so keying
    # on it would fork the cache. The transport cannot hold this clock (see `budget.Deadline`).
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
    # Frozen coordinates' gradients are zeroed: convergence measures only the forces the optimizer
    # may relieve.
    free_mask = np.repeat(~frozen, 3)

    initial_energy, initial_gradient, _ = evaluate_point(calc, positions)
    # Seeded from the input geometry, so an existing minimum runs no optimizer and comes back
    # byte-identical; otherwise every pass would mint a new structure id and fork the cache.
    max_gradient = float(np.max(np.abs(np.where(free_mask, initial_gradient.ravel(), 0.0))))
    final = positions
    energy = initial_energy
    steps = 0
    if max_gradient > spec.gradient_tolerance:
        molecule = _geometric_molecule(numbers, positions)
        engine = _TbliteEngine(
            molecule,
            calc,
            deadline,
            (positions, initial_energy, initial_gradient),
            free_mask=free_mask,
            tolerance=spec.gradient_tolerance,
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
            deadline.check("geometry optimization", engine.progress)
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

    Re-evaluated on the returned geometry (one gradient), since xtb's own threshold is looser.
    Frozen atoms never arrive here (`OptSpec.for_structure` routes them in-process). GFN-FF is
    verified on its own surface: a force-field minimum is not a stationary point of GFN2.
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

    Requires `outcome.cycles` from xtb's "CONVERGED AFTER" line, not the exit code. The relaxation
    is reported as 0.0 (initial energy = final) rather than paying a second subprocess for it.
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

    Verifies a binary-produced geometry; never reached for GFN-FF.
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
