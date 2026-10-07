"""The `xtb` binary as a second calculation backend — and the source of half a `calc_version`.

The image installs `xtb` and pins `CHEMCLAW_XTB_ENGINE=tblite`, so the binary is inactive until a
deployment selects it; adding a backend must never silently re-key a cache. `is_available()` and
`XtbSpec.resolve_backend()` decide, and the version string says which backend ran.
`binary_version()` returns `"absent"` rather than raising, so `calc_version` is derived here and
shipped in the result, never re-derived by a client.

The binary is worth having for its machinery: a native Hessian (much faster than finite
differences) and GFN-FF. Its own thermochemistry is ignored; the Hessian goes to `xtb_thermo`, so
the symmetry number stays explicit and both backends' free energies stay comparable.

Security: every invocation is an argv list with `shell=False`, built from a typed request; every
argv value is checked for a leading `-`; the run uses a fresh tempdir, a scrubbed environment, and
`run_isolated`, which kills the whole process group on timeout so no forked worker survives.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel

from chemclaw_mcp_calc.engine.budget import TIME_BUDGET_MARKER, TimeBudgetError
from chemclaw_mcp_calc.engine.config import settings
from chemclaw_mcp_calc.engine.metrics import (
    PROCESS_GROUP_KILLS,
    SUBPROCESS_DURATION,
    SUBPROCESS_TIMEOUTS,
)
from chemclaw_mcp_calc.engine.structure import Structure
from chemclaw_mcp_calc.engine.xtb_engine import ANGSTROM_TO_BOHR, HARTREE_TO_KCAL

logger = logging.getLogger(__name__)

#: What `binary_version` answers where there is no binary. `engine/identity.py` recognises it so a
#: key naming a program that never ran is never minted.
ABSENT = "absent"

#: The substring a `calc_version` carries when the binary was selected and is missing; one definition
#: shared with the check in `engine/identity.py`.
ABSENT_XTB_VERSION = f"xtb-{ABSENT}"

__all__ = [
    "ABSENT",
    "ABSENT_XTB_VERSION",
    "METHOD_FLAGS",
    "AtomicRow",
    "CliError",
    "CliResult",
    "CliTask",
    "SurfacePotential",
    "binary_path",
    "binary_version",
    "is_available",
    "require_binary_path",
    "run",
    "run_isolated",
    "scratch_dir",
    "supports",
]

# GFN parametrization name -> the flags that select it. `GFN-FF` is a force field: no orbitals, no
# charges worth reading, but it optimizes a 118-atom molecule in under a second.
METHOD_FLAGS: dict[str, list[str]] = {
    "GFN2-xTB": ["--gfn", "2"],
    "GFN1-xTB": ["--gfn", "1"],
    "GFN0-xTB": ["--gfn", "0"],
    "GFN-FF": ["--gfnff"],
}

# Which xtb run to perform. `sp` is a single point, `opt` an ANCopt relaxation, `hess` a Hessian at
# the given geometry, `ohess` both in one process.
CliTask = Literal["sp", "opt", "hess", "ohess"]


def _task_flags(task: CliTask, opt_level: str | None) -> list[str]:
    """The flags for `task`, with the optimization tightness this layer requires.

    xtb's default "normal" level is looser than the gradient tolerance `xtb_opt` promises, and that
    promise is what makes a finite-difference Hessian on top meaningful. `opt_level` comes from
    `OptSpec` because it belongs in the key; `None` uses the configured default.
    """
    if task in ("opt", "ohess"):
        # Config-supplied, but checked anyway: every value reaching argv is checked, no exceptions.
        level = opt_level if opt_level is not None else settings.xtb_cli_opt_level
        return [f"--{task}", _safe(level, "optimization level")]
    return {"sp": [], "hess": ["--hess"]}[task]


# Environment passed to the child: an allowlist, so the server's bearer token never reaches a
# subprocess.
_ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL")


@contextmanager
def scratch_dir(prefix: str) -> Iterator[Path]:
    """A temporary directory for one run, whose *removal* is reported rather than raised.

    A cleanup failure (typically an orphan still writing) must not discard a completed result; it is
    logged at WARNING instead.

    Args:
        prefix: The `mkdtemp` prefix, so a leaked directory names the run that leaked it.
    """
    workdir = tempfile.TemporaryDirectory(prefix=prefix)
    try:
        yield Path(workdir.name)
    finally:
        try:
            workdir.cleanup()
        except OSError as exc:
            logger.warning(
                "could not remove the scratch directory %s: %s — a process from this run may "
                "still be writing into it",
                workdir.name,
                exc,
            )


# How long to wait for a killed group's pipes. Only reached where `killpg` was not, so the process
# may still be alive and the caller's budget is spent.
_REAP_TIMEOUT_SECONDS = 5.0


def run_isolated(
    argv: list[str], *, cwd: Path, env: dict[str, str], timeout: float, label: str
) -> subprocess.CompletedProcess[str]:
    """Run `argv` in its own process group, and kill the whole group on timeout.

    `subprocess.run(timeout=...)` kills only the tracked PID and orphans forked workers.
    `start_new_session=True` gives the child its own process group, so `os.killpg` reaches
    everything it spawned; otherwise this matches `subprocess.run(..., capture_output=True,
    text=True, check=False)`. Completion logs at INFO; a kill logs at WARNING with pgid and elapsed
    seconds.

    Args:
        argv: The command, already built and checked by the caller.
        cwd: The scratch directory the run owns.
        env: The scrubbed environment (`_ENV_ALLOWLIST`), never the parent's.
        timeout: Wall-clock budget in seconds, after which the whole group is killed.
        label: What this run is, for the log line only — never a metric label (metrics are labelled
            by binary, bounded by the image).

    Raises:
        subprocess.TimeoutExpired: the budget was spent; the group has been killed by then.
    """
    binary = Path(argv[0]).name
    started = time.monotonic()
    # S603: same as `crest_cli` - argv is built here from the configured binary and a private
    # tempdir, and `start_new_session=True` is what makes the timeout able to kill the group.
    process = subprocess.Popen(  # noqa: S603
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # The child leads a fresh group, so its pid is the pgid and this reaches every forked
        # process. A process that exited in the meantime is not an error, and the kill counter and
        # log live inside the block that reached `killpg`, so only real kills are counted.
        killed: int | None = None
        with suppress(ProcessLookupError):
            pgid = os.getpgid(process.pid)
            os.killpg(pgid, signal.SIGKILL)
            killed = pgid
            PROCESS_GROUP_KILLS.labels(binary).inc()
        elapsed = time.monotonic() - started
        SUBPROCESS_DURATION.labels(binary).observe(elapsed)
        SUBPROCESS_TIMEOUTS.labels(binary).inc()
        logger.warning(
            "%s %s exceeded its %gs budget after %.1fs; %s",
            binary,
            label,
            timeout,
            elapsed,
            f"SIGKILLed process group {killed}"
            if killed is not None
            else "the process had already exited, so no process group was killed",
        )
        # Collect what the run wrote. Bounded, because if `killpg` was not reached the process may
        # still be running.
        try:
            stdout, stderr = process.communicate(timeout=_REAP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        raise subprocess.TimeoutExpired(argv, timeout, output=stdout, stderr=stderr) from None
    elapsed = time.monotonic() - started
    SUBPROCESS_DURATION.labels(binary).observe(elapsed)
    logger.info(
        "%s %s finished: exit=%d elapsed_s=%.1f stdout_bytes=%d",
        binary,
        label,
        process.returncode,
        elapsed,
        len(stdout),
    )
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


class CliError(RuntimeError):
    """An `xtb` invocation failed. Carries the tail of its output, which names the cause.

    Not a `ValueError`: a stderr tail is internal state, so `connector_app` logs it and returns a
    generic notice. A missing binary is a configuration fault and raises a worded `ValueError` from
    `require_binary_path` instead.
    """


def require_binary_path() -> str:
    """The resolved `xtb` path, or a worded refusal naming the deployment fault.

    A `ValueError` (reaches the model verbatim) rather than a `CliError`: a missing binary is a
    configuration fault every call will hit, and the model can act on it.

    Returns:
        The absolute path to the binary.

    Raises:
        ValueError: There is no `xtb` on `PATH`.
    """
    path = binary_path()
    if path is None:
        raise ValueError(
            f"the {settings.xtb_binary!r} binary is not installed in this deployment, so no "
            "calculation that needs it can run here. This is a deployment's configuration rather "
            "than anything about the molecule: the same question will fail the same way until xtb "
            "is in the image. The tblite-backed tools — compute_xtb_energy, "
            "compute_electronic_properties, predict_site_reactivity, compute_properties_at and "
            "compute_fukui_at — need no binary and still answer."
        )
    return path


class AtomicRow(BaseModel):
    """One row of xtb's per-atom property table — the descriptors tblite does not expose.

    Printed by every xtb run under the header `#  Z  covCN  q  C6AA  a(0)`, in the input's atom
    order. All four are atomic units. `coordination_number` is the fractional *covalent* CN the
    GFN Hamiltonian uses, `c6_au` the atom's dispersion coefficient and `polarisability_au` its
    static isotropic polarisability — the last two being what a dispersion or halogen-bonding
    question is actually about, and neither derivable from anything `tblite.Result` returns.
    """

    index: int
    element: str
    coordination_number: float
    charge: float
    c6_au: float
    polarisability_au: float


class SurfacePotential(BaseModel):
    """The extrema of the molecular electrostatic potential on xtb's own surface grid.

    `minimum` marks the most electron-rich patch (a lone pair, a pi face) and `maximum` the most
    electron-poor (an acidic hydrogen, a halogen's sigma-hole). Both in kcal/mol, converted at the
    unit boundary rather than left in Hartree/e, because every published V_s,min a chemist would
    compare these against is quoted in kcal/mol.
    """

    minimum_kcal_per_mol: float
    maximum_kcal_per_mol: float
    grid_points: int


class CliResult(BaseModel):
    """What one `xtb` run produced, in this layer's units.

    `structure` is present only for an optimizing task; `hessian` (Hartree/Angstrom^2),
    `ir_intensities` (km/mol) and `ir_wavenumbers_cm` only for a Hessian task. `properties` is the
    parsed `xtbout.json`, which carries the energy, orbital energies, dipole and partial charges the
    same run computed.

    **The two IR lists are one datum split in two, and they are only meaningful together.** An
    intensity says nothing without the band it belongs to, and which band that is has to be settled
    by *something* — either both sides agreeing, forever, on how many external modes xtb writes for
    this molecule, or the wavenumber travelling beside the intensity. `ir_wavenumbers_cm` is the
    second, and it costs 3N floats.
    """

    model_config = {"arbitrary_types_allowed": True}

    energy_hartree: float
    structure: Structure | None = None
    hessian: Any = None
    ir_intensities: list[float] | None = None
    # xtb's own wavenumbers (cm^-1), one per entry of `ir_intensities` and in the same order:
    # negative for an imaginary mode, exactly zero for a projected-out translation or rotation.
    ir_wavenumbers_cm: list[float] | None = None
    cycles: int | None = None
    properties: dict[str, Any] = {}
    atomic_rows: list[AtomicRow] = []


@lru_cache(maxsize=1)
def binary_path() -> str | None:
    """Absolute path to the configured `xtb` binary, or None when it is not installed.

    Cached: asked on every spec construction, and constant within a process.
    """
    return shutil.which(settings.xtb_binary)


def is_available() -> bool:
    """Whether the `xtb` binary can be used at all."""
    return binary_path() is not None


@lru_cache(maxsize=1)
def binary_version() -> str:
    """The installed xtb version, for `calc_version`.

    An xtb upgrade changes results, so it must be a cache miss on the Chemclaw3 side. Returns
    `"absent"` rather than raising; derived here and shipped in the result, never re-derived by a
    client. With `CHEMCLAW_XTB_ENGINE=xtb` and no binary, `app._readiness` refuses traffic, so the
    string never reaches a key.

    Cached; `app.py` warms it at startup so no request pays the subprocess on the event loop.
    """
    path = binary_path()
    if path is None:
        return ABSENT
    # S603: `path` is the resolved binary and `--version` is a literal; nothing here is a caller's.
    output = subprocess.run(  # noqa: S603
        [path, "--version"], capture_output=True, text=True, timeout=30, check=False
    ).stdout
    for line in output.splitlines():
        if "version" in line.lower():
            parts = [word for word in line.replace(",", " ").split() if word[:1].isdigit()]
            if parts:
                return str(parts[0])
    return "unknown"


def supports(method: str) -> bool:
    """Whether this backend can run `method` at all."""
    return method in METHOD_FLAGS


def _safe(value: str, what: str) -> str:
    """Reject an argv value that could be read as an option.

    Without a shell, a leading dash is the only way a data string becomes a flag.
    """
    if value.startswith("-"):
        raise ValueError(f"{what} {value!r} may not start with '-'")
    return value


def _to_xyz(structure: Structure) -> str:
    """Serialize a structure as an XYZ file (Angstrom), which is xtb's input format."""
    lines = [str(len(structure.elements)), structure.smiles or ""]
    lines += [
        f"{symbol} {x:.10f} {y:.10f} {z:.10f}"
        for symbol, (x, y, z) in zip(structure.symbols, structure.positions, strict=True)
    ]
    return "\n".join(lines) + "\n"


def _from_xyz(text: str, template: Structure, origin: str | None) -> Structure:
    """Read an xtb-written XYZ back into a `Structure`, keeping the template's identity.

    Elements come from the template, so an element mismatch fails validation loudly.
    """
    rows = text.splitlines()[2 : 2 + len(template.elements)]
    positions = [[float(value) for value in row.split()[1:4]] for row in rows]
    return Structure(
        elements=template.elements,
        positions=positions,
        charge=template.charge,
        multiplicity=template.multiplicity,
        smiles=template.smiles,
        origin=origin,
    )


def _read_hessian(path: Path, size: int) -> np.ndarray:
    """Parse xtb's Turbomole-format `hessian` file into a (3N, 3N) Hartree/Angstrom^2 matrix."""
    numbers: list[float] = []
    for line in path.read_text().splitlines():
        if line.startswith("$"):
            continue
        numbers.extend(float(value) for value in line.split())
    if len(numbers) != size * size:
        raise CliError(f"hessian has {len(numbers)} entries, expected {size * size}")
    # xtb writes Hartree/Bohr^2; this layer works in Angstrom.
    return np.array(numbers).reshape(size, size) * ANGSTROM_TO_BOHR**2


def _read_vibspectrum(path: Path) -> list[tuple[float, float]]:
    """Parse `vibspectrum` into (wavenumber cm^-1, IR intensity km/mol) pairs, in file order.

    File order is the external modes first, then vibrations ascending (pinned by
    `tests/test_vibspectrum.py` against real xtb output). The pairing is positional downstream, so
    the order matters. External modes (5 linear, 6 otherwise, by xtb's own criterion) are kept here
    and dropped by the caller, which asserts that its own projection agrees.
    """
    entries: list[tuple[float, float]] = []
    for line in path.read_text().splitlines():
        if line.startswith(("$", "#")):
            continue
        fields = line.split()
        # Indexed from the left by float-parseability: the symmetry label is missing on external
        # modes, and `--raman` appends extra numeric columns on the right.
        numeric = [value for value in fields if _is_float(value)]
        if len(numeric) >= 3:
            entries.append((float(numeric[1]), float(numeric[2])))
    return entries


def _is_float(value: str) -> bool:
    """Whether a whitespace-separated field parses as a number."""
    try:
        float(value)
    except ValueError:
        return False
    return True


def _energy_from_log(log: str) -> float | None:
    """The total energy printed in xtb's summary block.

    The fallback for GFN-FF, which writes no `xtbout.json`.
    """
    for line in reversed(log.splitlines()):
        if "TOTAL ENERGY" in line:
            for word in line.split():
                if _is_float(word):
                    return float(word)
    return None


def _cycles(log: str) -> int | None:
    """The ANC optimization cycle count xtb reports, when it ran one."""
    for line in log.splitlines():
        if "CONVERGED AFTER" in line:
            digits = [word for word in line.split() if word.isdigit()]
            if digits:
                return int(digits[-1])
    return None


def run(
    structure: Structure,
    *,
    task: CliTask,
    method: str,
    solvent: str | None = None,
    accuracy: float | None = None,
    opt_level: str | None = None,
    max_cycles: int | None = None,
) -> CliResult:
    """Run one `xtb` invocation on `structure` and parse everything it produced.

    Args:
        structure: The molecule, its charge and its multiplicity.
        task: Which run to perform (`sp`, `opt`, `hess`, `ohess`).
        method: GFN parametrization name; must be a key of `METHOD_FLAGS`.
        solvent: ALPB implicit solvent name, or None for gas phase.
        accuracy: xtb's `--acc` numerical accuracy; None uses the configured default. Callers
            holding a spec pass `spec.accuracy`, which is what puts it in the key.
        opt_level: ANCopt convergence level for `opt`/`ohess`; None uses the configured default.
        max_cycles: Optimization cycle cap; None uses the configured default.

    Returns:
        The energy, plus the optimized geometry and/or Hessian the task produced.

    Raises:
        TimeBudgetError: the run was killed at its timeout, opening with `TIME_BUDGET_MARKER`.
        CliError: the run exited non-zero.
        ValueError: the binary is missing, or the method is not one this backend supports.
    """
    path = require_binary_path()
    if not supports(method):
        raise ValueError(f"the xtb backend does not support method {method!r}")

    argv = [path, "input.xyz", *_task_flags(task, opt_level), *METHOD_FLAGS[method], "--json"]
    argv += ["--chrg", str(structure.charge), "--uhf", str(structure.uhf)]
    argv += ["--acc", str(accuracy if accuracy is not None else settings.xtb_cli_accuracy)]
    if settings.xtb_cli_threads > 0:
        argv += ["--parallel", str(settings.xtb_cli_threads)]
    if solvent is not None:
        argv += ["--alpb", _safe(solvent, "solvent")]
    if task in ("opt", "ohess"):
        cycles = max_cycles if max_cycles is not None else settings.xtb_opt_max_steps
        argv += ["--cycles", str(cycles)]

    with scratch_dir("xtb-") as directory:
        (directory / "input.xyz").write_text(_to_xyz(structure))
        environment = {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}
        if settings.xtb_cli_threads > 0:
            environment["OMP_NUM_THREADS"] = str(settings.xtb_cli_threads)
        try:
            completed = run_isolated(
                argv,
                cwd=directory,
                env=environment,
                timeout=settings.xtb_cli_timeout_seconds,
                label=task,
            )
        except subprocess.TimeoutExpired as error:
            # A stop by the clock, named as one (`TIME_BUDGET_MARKER`), so Chemclaw3 does not retry
            # the same work against the same clock. `run_isolated` has already logged and counted
            # the kill.
            raise TimeBudgetError(
                f"{TIME_BUDGET_MARKER} xtb {task} timed out after "
                f"{settings.xtb_cli_timeout_seconds}s"
            ) from error
        if completed.returncode != 0 and not _produced_everything(directory, task):
            tail = "\n".join(completed.stdout.splitlines()[-12:])
            raise CliError(f"xtb {task} failed (exit {completed.returncode}):\n{tail}")
        if completed.returncode != 0:
            logger.warning(
                "xtb %s exited %d but wrote every expected output; using it",
                task,
                completed.returncode,
            )
        return _collect(directory, structure, task, completed.stdout)


# What each task must leave behind to count as successful. The exit code alone is unreliable: xtb
# can write a complete result (e.g. a linear molecule's Hessian) and then abort during teardown.
_REQUIRED_OUTPUTS: dict[CliTask, tuple[str, ...]] = {
    "sp": ("xtbout.json",),
    "opt": ("xtbopt.xyz",),
    "hess": ("hessian", "vibspectrum"),
    "ohess": ("xtbopt.xyz", "hessian", "vibspectrum"),
}


def _read_atomic_table(log: str, atom_count: int) -> list[AtomicRow]:
    """Parse xtb's per-atom property table out of its stdout.

    Rows follow the input order: `index  Z  symbol  covCN  q  C6AA  alpha(0)`. Read from the log
    because `xtbout.json` carries none of these. Returns an empty list when the table is missing or
    short; the caller words the refusal.
    """
    lines = log.splitlines()
    header = next((index for index, line in enumerate(lines) if "covCN" in line), None)
    if header is None:
        return []
    rows: list[AtomicRow] = []
    for line in lines[header + 1 : header + 1 + atom_count]:
        fields = line.split()
        if len(fields) != 7:
            break
        try:
            rows.append(
                AtomicRow(
                    index=int(fields[0]) - 1,
                    element=fields[2],
                    coordination_number=float(fields[3]),
                    charge=float(fields[4]),
                    c6_au=float(fields[5]),
                    polarisability_au=float(fields[6]),
                )
            )
        except ValueError:  # pragma: no cover - a non-numeric row is not a table row
            break
    return rows if len(rows) == atom_count else []


def run_surface_potential(
    structure: Structure, *, method: str, solvent: str | None = None, accuracy: float | None = None
) -> SurfacePotential:
    """Compute the molecular electrostatic potential on xtb's surface grid and return its extrema.

    A separate invocation: an `--esp` run writes `xtb_esp.dat` and may then abort in teardown before
    `xtbout.json`, so success is judged by the grid file, not the exit code. `--acc` is passed so
    both descriptor calculations honour the same accuracy setting their keys name.

    Raises:
        CliError: the run timed out or produced no grid.
        ValueError: the binary is missing, or the method is not one this backend supports.
    """
    path = require_binary_path()
    if not supports(method):
        raise ValueError(f"the xtb backend does not support method {method!r}")

    argv = [path, "input.xyz", *METHOD_FLAGS[method], "--esp"]
    argv += ["--chrg", str(structure.charge), "--uhf", str(structure.uhf)]
    argv += ["--acc", str(accuracy if accuracy is not None else settings.xtb_cli_accuracy)]
    if solvent is not None:
        argv += ["--alpb", _safe(solvent, "solvent")]

    with scratch_dir("xtb-esp-") as directory:
        (directory / "input.xyz").write_text(_to_xyz(structure))
        environment = {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}
        try:
            run_isolated(
                argv,
                cwd=directory,
                env=environment,
                timeout=settings.xtb_cli_timeout_seconds,
                label="esp",
            )
        except subprocess.TimeoutExpired as error:
            raise CliError(
                f"xtb --esp timed out after {settings.xtb_cli_timeout_seconds}s"
            ) from error
        grid = directory / "xtb_esp.dat"
        if not grid.exists():
            raise CliError("xtb --esp produced no surface grid")
        return _read_surface(grid.read_text())


def _read_surface(text: str) -> SurfacePotential:
    """The extrema of an `xtb_esp.dat` grid, converted to kcal/mol.

    Each line is `x y z potential` (Bohr, Hartree per electron); only the potential is read.
    """
    values = [
        float(fields[3])
        for line in text.splitlines()
        if len(fields := line.split()) == 4 and _is_float(fields[3])
    ]
    if not values:
        raise CliError("xtb --esp wrote a surface grid with no potential values")
    return SurfacePotential(
        minimum_kcal_per_mol=round(min(values) * HARTREE_TO_KCAL, 3),
        maximum_kcal_per_mol=round(max(values) * HARTREE_TO_KCAL, 3),
        grid_points=len(values),
    )


def _produced_everything(directory: Path, task: CliTask) -> bool:
    """Whether the run left every file its task is defined by."""
    return all((directory / name).exists() for name in _REQUIRED_OUTPUTS[task])


def _collect(directory: Path, structure: Structure, task: CliTask, log: str) -> CliResult:
    """Read the files one run left behind into a typed result."""
    properties: dict[str, Any] = {}
    output = directory / "xtbout.json"
    if output.exists():
        properties = json.loads(output.read_text())

    relaxed: Structure | None = None
    optimized = directory / "xtbopt.xyz"
    if task in ("opt", "ohess") and optimized.exists():
        relaxed = _from_xyz(optimized.read_text(), structure, origin=None)

    hessian = None
    intensities: list[float] | None = None
    wavenumbers: list[float] | None = None
    if task in ("hess", "ohess"):
        size = 3 * len(structure.elements)
        hessian = _read_hessian(directory / "hessian", size)
        spectrum = _read_vibspectrum(directory / "vibspectrum")
        wavenumbers = [wavenumber for wavenumber, _ in spectrum]
        intensities = [intensity for _, intensity in spectrum]

    energy = properties.get("total energy", _energy_from_log(log))
    if energy is None:
        raise CliError("xtb produced no total energy")
    return CliResult(
        atomic_rows=_read_atomic_table(log, len(structure.elements)),
        energy_hartree=float(energy),
        structure=relaxed,
        hessian=hessian,
        ir_intensities=intensities,
        ir_wavenumbers_cm=wavenumbers,
        cycles=_cycles(log),
        properties=properties,
    )
