"""The `xtb` binary as a second calculation backend — and the source of half a `calc_version`.

**Read the availability note first.** This server's shipped image does **not** carry the `xtb`
binary (see `servers/calc/README.md` for why, and what to do about it). With no binary,
`is_available()` is False, `XtbSpec.resolve_backend()` picks `tblite`, and nothing in this module
runs — which is exactly the situation Chemclaw3 was already in before the split, so the numbers and
the `calc_version` strings are unchanged by the port. What the module buys is that a deployment
*can* add the binary, and when it does, the version string moves to say so **on the side that has
it**. That is the whole reason it was ported rather than dropped.

`binary_version()` is also the specific trap the port exists to close: it returns the literal string
`"absent"` rather than raising when the binary is missing. A client deriving `calc_version` locally
would therefore produce a well-formed string matching zero rows in Chemclaw3's calibration ledger,
and `calculator_trust("pka")` would confidently report `UNCALIBRATED`. Silent, not loud.

Why a subprocess at all, when `tblite` gives the same Hamiltonians in-process: because the binary
carries the *machinery* around them that tblite does not expose. Two pieces of that machinery decide
this system's economics on real substrates.

**ANCopt.** xtb optimizes in approximate normal coordinates, not Cartesians. Measured on the
molecules this system is pointed at:

| molecule                   | atoms | in-process | binary | speedup |
|----------------------------|-------|------------|--------|---------|
| atorvastatin core (MW 559) |    76 |  ~266 s    | 38.1 s |  ~7x    |
| erythromycin (MW 734)      |   118 | ~1283 s    |  142 s |  ~9x    |

(Optimization plus Hessian. The in-process figures are the measured Hessian time plus the measured
optimization time *after* the ANC preconditioner halved the latter — so they are composed from
measurements rather than re-timed end to end.) Most of the remaining gap is the **Hessian**, not the
optimizer: preconditioning narrowed the optimization component to ~3x and left the second-derivative
step untouched, because the in-process path takes it by finite differences and xtb does not.

**GFN-FF.** A force field with xTB's parameterization, which optimized the 118-atom substrate in
**0.7 s**. It is not a quantum method and gives no orbitals, but it makes pre-optimization and
large-system screening free.

**What this module does not do: thermochemistry.** xtb prints its own, and this backend deliberately
ignores it, taking the **Hessian matrix** instead and handing it to `xtb_thermo`. One RRHO
implementation keeps the symmetry number an explicit input rather than xtb's silent guess, keeps the
quasi-RRHO treatment identical across backends, and therefore keeps free energies from the two
backends comparable. A backend supplies geometry, energy and second derivatives; it does not get to
have opinions about thermodynamics.

**Security.** Every invocation is an **argv list with `shell=False`**, built from a typed request;
there is no control file, no shell string, and no path from model-authored text to a flag. Values
that reach argv are checked for a leading `-` — the one way a data string can become an option — and
the process runs in a fresh temporary directory with a scrubbed environment and a timeout.

The timeout is enforced by `run_isolated`, not a bare `subprocess.run(timeout=...)`: xtb's own
`--parallel` flag can leave more than one process running, and killing only the one PID
`subprocess.run` tracks orphans the rest — still burning CPU, and still writing into the tempdir
after `TemporaryDirectory.__exit__` has removed it. `run_isolated` starts the child in its own
process group and kills the whole group on a timeout.

**What was left behind in the port**: the D-124 artifact capture. Chemclaw3 read the run's
`hessian`/`vibspectrum` files out of the tempdir and handed them to a content-addressed blob store.
There is no store here, nothing would ever read them, and keeping the code would be a copy of a
cache with none of the value — so `CliResult` has no `artifacts` field and `_capture` is gone.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import tempfile
from contextlib import suppress
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel

from chemclaw_mcp_calc.engine.config import settings
from chemclaw_mcp_calc.engine.structure import Structure
from chemclaw_mcp_calc.engine.xtb_engine import ANGSTROM_TO_BOHR

logger = logging.getLogger(__name__)

__all__ = [
    "METHOD_FLAGS",
    "CliError",
    "CliResult",
    "CliTask",
    "binary_path",
    "binary_version",
    "is_available",
    "run",
    "run_isolated",
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


def _task_flags(task: CliTask) -> list[str]:
    """The flags for `task`, with the optimization tightness this layer requires.

    xtb's default level ("normal") converges to ~1e-3 Hartree/Bohr, which is looser than the
    gradient tolerance `xtb_opt` promises — measured, ethanol stops at 6.3e-4 Hartree/Angstrom
    against a 5e-4 target and is then correctly rejected. Asking for a tighter level is the fix;
    loosening the promise would have been the other one, and the promise is what makes the
    finite-difference Hessian on top of it meaningful.
    """
    if task in ("opt", "ohess"):
        # Config-supplied rather than model-supplied, but checked all the same: the module's stated
        # rule is that *every* value reaching argv is checked, and a rule with a quiet exception is
        # one nobody can rely on when adding the next flag.
        return [f"--{task}", _safe(settings.xtb_cli_opt_level, "optimization level")]
    return {"sp": [], "hess": ["--hess"]}[task]


# Environment passed to the child. An allowlist rather than the parent's environment: xtb reads
# XTBPATH, XTBHOME and OMP_*, and inheriting a server's full environment into a subprocess is how a
# bearer token leaks into a tool that writes files it does not own.
_ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL")


def run_isolated(
    argv: list[str], *, cwd: Path, env: dict[str, str], timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run `argv` in its own process group, and kill the whole group on timeout.

    The naive `subprocess.run(argv, timeout=...)` this replaced was wrong: on a timeout it kills
    only the one PID it is tracking, and a forked worker is not in that process's process group by
    default, so it survives as an orphan — still burning CPU, and still writing into the tempdir
    after the caller's `TemporaryDirectory.__exit__` has removed it.

    `start_new_session=True` puts the child in a new session and process group of its own, so
    `os.killpg` on a timeout reaches every process the run spawned. Everything else matches
    `subprocess.run(..., timeout=..., capture_output=True, text=True, check=False)`.
    """
    process = subprocess.Popen(
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
        # The group leader's pgid is its own pid (start_new_session guarantees the child is the
        # leader of a fresh group), so this reaches every process the run forked. A race where the
        # process has already exited between the timeout and here is not an error.
        with suppress(ProcessLookupError):
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        # Reap the now-dead group leader and collect whatever it had written; the pipes are closed
        # and the process is gone, so this returns immediately rather than blocking again.
        stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(argv, timeout, output=stdout, stderr=stderr) from None
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


class CliError(RuntimeError):
    """An `xtb` invocation failed. Carries the tail of its output, which names the cause.

    Deliberately **not** a `ValueError`: `mcp_server_kit.connector_app` passes a `ValueError` to the
    model verbatim, and a subprocess's stderr tail is internal state rather than a caller-safe
    explanation. It is logged and replaced with a generic notice, which is the correct handling for
    an infrastructure fault.
    """


class CliResult(BaseModel):
    """What one `xtb` run produced, in this layer's units.

    `structure` is present only for an optimizing task; `hessian` (Hartree/Angstrom^2) and
    `ir_intensities` (km/mol) only for a Hessian task. `properties` is the parsed `xtbout.json`,
    which carries the energy, orbital energies, dipole and partial charges the same run computed.
    """

    model_config = {"arbitrary_types_allowed": True}

    energy_hartree: float
    structure: Structure | None = None
    hessian: Any = None
    ir_intensities: list[float] | None = None
    cycles: int | None = None
    properties: dict[str, Any] = {}


@lru_cache(maxsize=1)
def binary_path() -> str | None:
    """Absolute path to the configured `xtb` binary, or None when it is not installed.

    Cached: this is asked on every spec construction to decide which backend to use, and the answer
    cannot change within a process.
    """
    return shutil.which(settings.xtb_binary)


def is_available() -> bool:
    """Whether the `xtb` binary can be used at all."""
    return binary_path() is not None


@lru_cache(maxsize=1)
def binary_version() -> str:
    """The installed xtb version, for `calc_version`.

    An xtb upgrade changes energies and geometries, so it must be a cache miss on the Chemclaw3
    side rather than a silent stale hit — the same rule `engine_version` applies to tblite.

    **Returns `"absent"` rather than raising**, and that is the behaviour the whole port exists to
    contain. Here it is honest: this process really did resolve the backend, found no binary, and
    `resolve_backend()` will therefore never select `"xtb"`, so the string never reaches a key. A
    *client* calling an equivalent function would get the same word for a different reason — it
    never had the binary to begin with — and would stamp a valid-looking version onto a prediction
    the ledger cannot match. Hence: derived here, shipped in the result, never re-derived.

    The first call in a process shells out; `lru_cache` means it happens once. `app.py` warms it at
    startup so no request pays the 30 s worst case on the event loop.
    """
    path = binary_path()
    if path is None:
        return "absent"
    output = subprocess.run(
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

    The only way a *data* string becomes a flag in an argv-based tool is by starting with a dash, so
    that is the check. Everything else — spaces, quotes, semicolons — is inert without a shell, and
    there is no shell here.
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

    The elements come from the template rather than from the file: xtb echoes the same atoms in the
    same order, and trusting the template makes an element mismatch a loud validation failure
    instead of a silently different molecule.
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

    The leading entries are the projected-out translations and rotations; they are kept here and
    dropped by the caller, which knows how many modes its own projection found. Reconciling the two
    counts is the point — a mismatch means the two projections disagree about the molecule, which
    must fail loudly rather than shift every intensity by one mode.
    """
    entries: list[tuple[float, float]] = []
    for line in path.read_text().splitlines():
        if line.startswith(("$", "#")):
            continue
        fields = line.split()
        # "index [symmetry] wavenumber intensity [selection rules...]" — the two numbers before the
        # selection-rule columns are what matter, and the symmetry label is absent on the external
        # modes, so index from the left by float-parseability.
        numeric = [value for value in fields if _is_float(value)]
        if len(numeric) >= 3:
            entries.append((float(numeric[-2]), float(numeric[-1])))
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

    The fallback for GFN-FF, which writes no `xtbout.json`: a force field has no SCC, no orbitals
    and nothing else that file exists to carry, so the printed summary is the only place its energy
    appears.
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
    max_cycles: int | None = None,
) -> CliResult:
    """Run one `xtb` invocation on `structure` and parse everything it produced.

    Args:
        structure: The molecule, its charge and its multiplicity.
        task: Which run to perform (`sp`, `opt`, `hess`, `ohess`).
        method: GFN parametrization name; must be a key of `METHOD_FLAGS`.
        solvent: ALPB implicit solvent name, or None for gas phase.
        accuracy: xtb's `--acc` numerical accuracy; None uses the configured default.
        max_cycles: Optimization cycle cap; None uses the configured default.

    Returns:
        The energy, plus the optimized geometry and/or Hessian the task produced.

    Raises:
        CliError: the binary is absent, timed out, or exited non-zero.
        ValueError: the method is not one this backend supports.
    """
    path = binary_path()
    if path is None:
        raise CliError(f"the {settings.xtb_binary!r} binary is not installed")
    if not supports(method):
        raise ValueError(f"the xtb backend does not support method {method!r}")

    argv = [path, "input.xyz", *_task_flags(task), *METHOD_FLAGS[method], "--json"]
    argv += ["--chrg", str(structure.charge), "--uhf", str(structure.uhf)]
    argv += ["--acc", str(accuracy if accuracy is not None else settings.xtb_cli_accuracy)]
    if settings.xtb_cli_threads > 0:
        argv += ["--parallel", str(settings.xtb_cli_threads)]
    if solvent is not None:
        argv += ["--alpb", _safe(solvent, "solvent")]
    if task in ("opt", "ohess"):
        cycles = max_cycles if max_cycles is not None else settings.xtb_opt_max_steps
        argv += ["--cycles", str(cycles)]

    with tempfile.TemporaryDirectory(prefix="xtb-") as workdir:
        directory = Path(workdir)
        (directory / "input.xyz").write_text(_to_xyz(structure))
        environment = {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}
        if settings.xtb_cli_threads > 0:
            environment["OMP_NUM_THREADS"] = str(settings.xtb_cli_threads)
        try:
            completed = run_isolated(
                argv, cwd=directory, env=environment, timeout=settings.xtb_cli_timeout_seconds
            )
        except subprocess.TimeoutExpired as error:
            raise CliError(
                f"xtb {task} timed out after {settings.xtb_cli_timeout_seconds}s"
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


# What each task must leave behind for its run to have succeeded. Checked because xtb's exit code is
# not reliable on its own: measured, a Hessian on **linear CO2** computes correctly — the file holds
# its textbook 655/1345/2446 cm^-1 — and then the process aborts during teardown with SIGABRT.
# Discarding a complete calculation over a crash in its own cleanup would silently lose every linear
# molecule.
_REQUIRED_OUTPUTS: dict[CliTask, tuple[str, ...]] = {
    "sp": ("xtbout.json",),
    "opt": ("xtbopt.xyz",),
    "hess": ("hessian", "vibspectrum"),
    "ohess": ("xtbopt.xyz", "hessian", "vibspectrum"),
}


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
    if task in ("hess", "ohess"):
        size = 3 * len(structure.elements)
        hessian = _read_hessian(directory / "hessian", size)
        intensities = [intensity for _, intensity in _read_vibspectrum(directory / "vibspectrum")]

    energy = properties.get("total energy", _energy_from_log(log))
    if energy is None:
        raise CliError("xtb produced no total energy")
    return CliResult(
        energy_hartree=float(energy),
        structure=relaxed,
        hessian=hessian,
        ir_intensities=intensities,
        cycles=_cycles(log),
        properties=properties,
    )
