"""GFN2-xTB geometry optimization.

The first task whose *output* is a geometry. Everything before it — the single point, the electronic
properties, the Fukui indices, the pKa acid branch — describes whatever conformer RDKit happened to
embed and MMFF happened to relax; this module produces a stationary point of the surface those
numbers are actually computed on, which is the precondition for a Hessian (`xtb_thermo`).

The in-process optimizer is `scipy.optimize.minimize(method="L-BFGS-B")` driven by tblite's
**analytic** gradient, in the eigenbasis of an approximate Hessian (`anc`). It works in Cartesian
coordinates, which is the simple choice rather than the fast one; atoms can be frozen by pinning
their coordinates with equal bounds — an exact constrained minimization over the free subspace.

**Ported without `run_cached_optimization`,** and without the `geometry.record_optimization`
cross-method pointer it wrote to on every miss: both are store operations, and this server has no
store. The optimized `Structure` still carries `origin` — the key of the calculation that produced
it — so lineage survives the removal of the thing that used to persist it.
"""

from __future__ import annotations

from typing import Literal, Self

import numpy as np
from pydantic import Field
from scipy.optimize import OptimizeResult, minimize

from chemclaw_mcp_calc.engine import anc, xtb_cli
from chemclaw_mcp_calc.engine.budget import Deadline
from chemclaw_mcp_calc.engine.config import settings
from chemclaw_mcp_calc.engine.key import Keyed
from chemclaw_mcp_calc.engine.structure import Structure, structure_from_smiles
from chemclaw_mcp_calc.engine.xtb_engine import (
    HARTREE_TO_KCAL,
    Calculator,
    evaluate_point,
    make_calculator,
)
from chemclaw_mcp_calc.engine.xtb_spec import XtbSpec

__all__ = [
    "OptSpec",
    "OptimizationResult",
    "OptimizationSummary",
    "optimization_inputs",
    "optimize_structure",
]


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
    max_steps: int = Field(default_factory=lambda: settings.xtb_opt_max_steps, gt=0)
    # Cap on how far any atom may move in one leg, in Angstrom. A spec field rather than a settings
    # read inside the loop, because it moves the answer and a setting that moves the answer belongs
    # in the key: measured on ethanol, 0.35 and 0.05 relax to different geometries and different
    # energies — and a structure id is what every downstream key is built from.
    trust_radius: float = Field(default_factory=lambda: settings.xtb_opt_trust_radius, gt=0)
    # Curvature (Hartree/Angstrom^2) the ANC preconditioner assumes for the directions its pairwise
    # model cannot see. Here for the same reason as `trust_radius`, and it was the same defect:
    # `anc.basis` read it from `settings` inside the loop, so it was in no key at all. Measured on
    # ethanol, 1.0 and 0.005 relax to different geometries and different energies, and on a floppy
    # substrate the two floors can settle in different basins — at which point a shared cache row
    # serves one configuration another's conformer.
    curvature_floor: float = Field(default_factory=lambda: settings.xtb_anc_curvature_floor, gt=0)
    # The convergence level passed to `xtb --opt`, which is where the binary's relaxation stops.
    # Keyed only when the binary is what runs (`unkeyed_fields`), because the in-process path stops
    # on `gradient_tolerance` instead and never sees this.
    opt_level: str = Field(default_factory=lambda: settings.xtb_cli_opt_level, min_length=1)
    # Indices of atoms held at their input positions. Empty for a free optimization.
    frozen_atoms: tuple[int, ...] = ()

    def for_structure(self, structure: Structure) -> Self:
        """Also resolve the backend a *constrained* optimization really runs on.

        Frozen atoms are expressible as optimizer bounds and not as an xtb flag without a control
        file, so `_optimize_with_binary` has always handed them straight to the Cartesian path. That
        fallback happened *after* the key was derived, so a scan point on a deployment with the
        binary was stored under a `calc_version` naming a program that had not run — the defect
        `_FIXED_BACKEND` describes for the fixed-backend tasks, in a third place, and the reason
        `curvature_floor` can be keyed by backend at all: the resolved engine has to be the truth.
        """
        if self.frozen_atoms and self.engine == "xtb":
            # Idempotent: the copy's engine is no longer the binary, so this arm runs once.
            return self.model_copy(update={"engine": "tblite"}).for_structure(structure)
        return super().for_structure(structure)

    def unkeyed_fields(self) -> set[str]:
        """The two optimizer knobs are keyed on the backend that reads them, and only that one.

        `curvature_floor` is the in-process preconditioner's; `opt_level` is ANCopt's. Each is inert
        on the other backend, and `engine` is already resolved by the time `cache_key` asks — so
        neither can be excluded on a spec whose declared engine is not the one that will run.
        """
        unkeyed = super().unkeyed_fields()
        unkeyed.add("opt_level" if self.engine != "xtb" else "curvature_floor")
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

    The `xtb` binary optimizes in approximate normal coordinates (ANCopt) and is 9-11x faster on
    drug-sized molecules than the Cartesian L-BFGS below it. The in-process path is what the shipped
    image takes all the same — it installs the binary and pins `CHEMCLAW_XTB_ENGINE=tblite`, so
    adding it did not re-key anything — and the two are keyed separately because they do not produce
    identical geometries.

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


def _preconditioned_leg(
    calc: Calculator,
    spec: OptSpec,
    origin: np.ndarray,
    free_mask: np.ndarray,
    vectors: np.ndarray,
    scale: np.ndarray,
    max_iterations: int,
    deadline: Deadline,
) -> tuple[np.ndarray, int]:
    """Run L-BFGS-B once in the preconditioned basis; return the geometry and its cost.

    A leg rather than the whole optimization, because the model Hessian depends on the interatomic
    distances and a leg can move them enough that the basis is worth rebuilding. Its own function
    rather than a closure inside the loop so the basis it uses is an argument — the version that
    captured the loop variables was correct only by accident of evaluation order.
    """

    def to_cartesian(step: np.ndarray) -> np.ndarray:
        full = origin.copy()
        full[free_mask] += vectors @ (step * scale)
        return full

    # The convergence promise is about the *Cartesian* gradient, and the optimizer sees a
    # preconditioned one — so rather than converting a threshold between the two (the first attempt
    # converted it the wrong way and every leg stopped almost immediately), the objective records
    # what the promise is actually about and a callback stops the leg the moment it is met.
    reached = {"max_gradient": float("inf")}

    def objective(step: np.ndarray) -> tuple[float, np.ndarray]:
        # Per gradient rather than per leg: `max_steps` bounds iterations, and one iteration on a
        # large substrate is unbounded in seconds — so a leg is exactly what an outer check misses.
        deadline.check("geometry optimization")
        energy, gradient, _ = evaluate_point(calc, to_cartesian(step).reshape(-1, 3))
        free_gradient = gradient.ravel()[free_mask]
        reached["max_gradient"] = float(np.max(np.abs(free_gradient)))
        # Chain rule through the linear transform: dE/ds = scale * (V^T dE/dx).
        return energy, scale * (vectors.T @ free_gradient)

    def stop_when_converged(intermediate: OptimizeResult) -> None:
        """Halt the leg the moment the Cartesian promise is met.

        `StopIteration` is scipy's documented way for a callback to end a minimization; it returns
        the best point found rather than treating it as a failure.
        """
        if reached["max_gradient"] <= spec.gradient_tolerance:
            raise StopIteration

    # The trust region is a Cartesian distance, so it becomes a per-coordinate bound in the
    # preconditioned basis by dividing by that coordinate's own scale — a soft direction is allowed
    # a large `s` because a large `s` moves the atoms little.
    limit = spec.trust_radius / np.maximum(scale, 1e-12)
    # `type: ignore` scoped to one call: scipy's `minimize` overload for `jac=True` *with* a
    # callback requires the objective to accept `*args, **kwargs`, which this one has no use for.
    # The call is correct; the stub is narrow.
    outcome = minimize(  # type: ignore[call-overload]
        objective,
        np.zeros(int(free_mask.sum())),
        jac=True,
        method="L-BFGS-B",
        bounds=list(zip(-limit, limit, strict=True)),
        callback=stop_when_converged,
        options={
            "maxiter": max_iterations,
            # Both of L-BFGS-B's own stopping tests are disabled: `ftol` fires long before a tight
            # gradient target is met, and `gtol` is in the preconditioned units the callback exists
            # to avoid reasoning about.
            "gtol": 0.0,
            "ftol": 0.0,
        },
    )
    return to_cartesian(np.asarray(outcome.x, dtype=float)), max(int(outcome.nit), 1)


def _optimize_with_library(spec: OptSpec, structure: Structure) -> OptimizationResult:
    """Relax with tblite's analytic gradient, L-BFGS-B, and an ANC preconditioner.

    The only backend that can hold atoms fixed or describe an open shell, and — in an image without
    the `xtb` binary — the only backend at all.

    The optimization runs in the eigenbasis of an approximate Hessian, scaled by the square root of
    its curvature, so the surface L-BFGS sees is nearly isotropic. The transform is linear, so a
    step in it is an exact Cartesian displacement and there is nothing to back-transform. The trust
    region and the convergence test both stay in Cartesian space, where they mean something
    physical.
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
    # The gradient of a frozen coordinate is zeroed rather than merely bounded: the convergence test
    # must measure the forces the optimizer is allowed to relieve, not the ones the constraint is
    # holding.
    free_mask = np.repeat(~frozen, 3)

    initial_energy, initial_gradient, _ = evaluate_point(calc, positions)
    # Trust region, enforced with bounds. L-BFGS-B's first trial step is scaled by 1/|gradient|,
    # which on a strained starting geometry is wildly too large: measured on a water with a 1.6
    # Angstrom O-H, its opening move collapses the bond to 0.20 Angstrom, and a step like that puts
    # the SCF somewhere it does not converge at all.
    current = positions.ravel()
    steps = 0
    # Seeded from the *input* geometry, so a structure that is already a minimum runs no leg at all
    # and comes back byte-identical. Previously the loop was bounded only by the step count, so it
    # always ran at least one leg and always moved something: re-optimizing a converged water
    # shifted it 3e-4 Angstrom, and since a structure id is a hash of the coordinates, every pass
    # minted a new id. That silently forks the cache for every task keyed on a geometry. The
    # gradient here costs nothing: `evaluate_point` above already computed it and threw it away.
    max_gradient = float(np.max(np.abs(np.where(free_mask, initial_gradient.ravel(), 0.0))))
    # The energy at whatever geometry `current` holds, carried forward rather than recomputed after
    # the loop. Every `evaluate_point` is a full SCF, and the one that used to sit below the loop
    # ran at a geometry already evaluated.
    energy = initial_energy
    while max_gradient > spec.gradient_tolerance and steps < spec.max_steps:
        # The basis is rebuilt each leg, from the geometry the last one reached: the model Hessian
        # depends on the distances, and a leg can move them enough to matter. It costs one O(N^2)
        # assembly and one eigendecomposition — negligible against the SCFs the leg is about to run.
        origin = current.copy()
        vectors, scale = anc.basis(numbers, origin.reshape(-1, 3), free_mask, spec.curvature_floor)
        current, iterations = _preconditioned_leg(
            calc, spec, origin, free_mask, vectors, scale, spec.max_steps - steps, deadline
        )
        steps += iterations
        energy, gradient, _ = evaluate_point(calc, current.reshape(-1, 3))
        max_gradient = float(np.max(np.abs(np.where(free_mask, gradient.ravel(), 0.0))))
        if max_gradient <= spec.gradient_tolerance:
            break

    final = current.reshape(-1, 3)
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
