"""The `crest` binary: conformer, tautomer, protomer and non-covalent-complex sampling.

The image ships CREST (GPL-3.0, invoked as a separate process over files, never linked); without
it `is_available()` is False and the searches refuse by name rather than degrading to one
conformer. Searches: conformers, tautomers, protomers/deprotomers, and complex (`--nci`). Only the
ensemble (energies, degeneracies, geometries) is returned; populations and entropies are
arithmetic done in Chemclaw3.

Security as in `xtb_cli`: argv list, `shell=False`, a fresh temp directory, a scrubbed environment,
nothing that could read as an option, and `run_isolated` so CREST's forked workers are killed
with their process group on timeout.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from chemclaw_mcp_calc.engine.budget import TIME_BUDGET_MARKER, TimeBudgetError
from chemclaw_mcp_calc.engine.chem import atomic_numbers, perceive_smiles
from chemclaw_mcp_calc.engine.config import settings
from chemclaw_mcp_calc.engine.structure import Structure
from chemclaw_mcp_calc.engine.xtb_cli import (
    CliError,
    _safe,
    _to_xyz,
    run_isolated,
    scratch_dir,
)

logger = logging.getLogger(__name__)

# Searches over one molecule; a complex needs a second molecule, so it is a different tool.
EnsembleSearch = Literal["conformers", "tautomers", "protomers", "deprotomers"]
CrestSearch = Literal["conformers", "tautomers", "protomers", "deprotomers", "complex"]
_SEARCH_FLAGS: dict[CrestSearch, list[str]] = {
    "conformers": [],
    "tautomers": ["--tautomerize"],
    "protomers": ["--protonate"],
    "deprotomers": ["--deprotonate"],
    # Non-covalent mode: a wall potential keeps the pair together so the search samples binding.
    "complex": ["--nci"],
}

# Search depth, in CREST's own names: `quick` for screening, `extensive` when a missed conformer
# matters.
CrestEffort = Literal["quick", "normal", "extensive"]
_EFFORT_FLAGS: dict[CrestEffort, list[str]] = {
    "quick": ["--quick"],
    "normal": [],
    "extensive": ["--mrest", "10"],
}

_METHOD_FLAGS = {
    "GFN2-xTB": ["--gfn2"],
    "GFN1-xTB": ["--gfn1"],
    "GFN-FF": ["--gfnff"],
}

_ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL")

# The file each search writes its ensemble to, verified against crest 3.0.2. No fallback: falling
# back to `crest_conformers.xyz` would return the *input's* conformers relabelled with a shifted
# charge. A missing file is an error.
_ENSEMBLE_FILE: dict[CrestSearch, str] = {
    "conformers": "crest_conformers.xyz",
    "tautomers": "tautomers.xyz",
    "protomers": "protonated.xyz",
    "deprotomers": "deprotonated.xyz",
    "complex": "crest_conformers.xyz",
}

# Net charge shift per search: CREST adds or removes a proton (no electrons), so only the charge
# moves and the multiplicity carries over.
_CHARGE_SHIFT: dict[CrestSearch, int] = {
    "conformers": 0,
    "tautomers": 0,
    "protomers": +1,
    "deprotomers": -1,
    "complex": 0,
}

# Whether every member is the input molecule; otherwise its SMILES is perceived from the geometry.
_KEEPS_CONSTITUTION: dict[CrestSearch, bool] = {
    "conformers": True,
    "tautomers": False,
    "protomers": False,
    "deprotomers": False,
    "complex": True,
}


class EnsembleMember(BaseModel):
    """One structure of a CREST ensemble, with the energy it was ranked by.

    `degeneracy` is how many **rotamers** collapse onto this conformer — n-butane's
    gauche is two mirror-image rotamers, and its methyl rotations multiply further. It is
    not bookkeeping: a population that ignores it is simply wrong, and by a lot. Measured
    on n-butane, degeneracy-weighted populations give the anti 59.2% against CREST's own
    reported 59.14%; ignoring degeneracy gives 73%.
    """

    energy_hartree: float
    degeneracy: int = 1
    structure: Structure


@lru_cache(maxsize=1)
def binary_path() -> str | None:
    """Absolute path to the configured `crest` binary, or None when it is absent."""
    return shutil.which(settings.crest_binary)


def is_available() -> bool:
    """Whether ensemble sampling can run at all."""
    return binary_path() is not None


@lru_cache(maxsize=1)
def binary_version() -> str:
    """The installed CREST version, for the cache key (an upgrade must recompute)."""
    path = binary_path()
    if path is None:
        return "absent"
    # S603: the argv is this module's own, built from the configured binary path and files it
    # wrote into a private tempdir. No caller string reaches it - `tools.py` takes SMILES.
    output = subprocess.run(  # noqa: S603
        [path, "--version"], capture_output=True, text=True, timeout=60, check=False
    ).stdout
    for line in output.splitlines():
        if "version" in line.lower():
            words = [word.strip(",") for word in line.split()]
            for index, word in enumerate(words):
                if word.lower() == "version" and index + 1 < len(words):
                    return words[index + 1]
    return "unknown"


def _read_degeneracies(directory: Path, count: int) -> list[int]:
    """Rotamer counts per conformer from CREST's `cre_members`, or all ones if absent.

    Format: a count, then one line per conformer led by its rotamer count. Only conformer searches
    write it.
    """
    members = directory / "cre_members"
    if not members.exists():
        return [1] * count
    rows = [line.split() for line in members.read_text().splitlines() if line.split()]
    degeneracies = [int(row[0]) for row in rows[1:] if row[0].isdigit()]
    return degeneracies if len(degeneracies) == count else [1] * count


def _read_ensemble(path: Path, template: Structure, search: CrestSearch) -> list[EnsembleMember]:
    """Parse a multi-structure XYZ; CREST writes the energy on each comment line.

    Elements are read from the file, not the template: protonation modes add or remove atoms and
    presort hydrogens last. Charge is the template's plus the search's shift; multiplicity carries
    over. For a constitution-changing search the SMILES is perceived from the geometry, or `None`.
    """
    lines = path.read_text().splitlines()
    charge = template.charge + _CHARGE_SHIFT[search]
    members: list[EnsembleMember] = []
    cursor = 0
    while cursor < len(lines) and lines[cursor].strip():
        count = int(lines[cursor].split()[0])
        energy = float(lines[cursor + 1].split()[0])
        rows = [line.split() for line in lines[cursor + 2 : cursor + 2 + count]]
        elements = atomic_numbers([row[0] for row in rows])
        positions = [[float(value) for value in row[1:4]] for row in rows]
        smiles = (
            template.smiles
            if _KEEPS_CONSTITUTION[search]
            else perceive_smiles(elements, positions, charge)
        )
        members.append(
            EnsembleMember(
                energy_hartree=energy,
                structure=Structure(
                    elements=elements,
                    positions=positions,
                    charge=charge,
                    multiplicity=template.multiplicity,
                    smiles=smiles,
                ),
            )
        )
        cursor += 2 + count
    return members


def run(
    structure: Structure,
    *,
    search: CrestSearch,
    method: str,
    effort: CrestEffort = "quick",
    solvent: str | None = None,
    temperature_k: float | None = None,
) -> list[EnsembleMember]:
    """Run one CREST search and return its ensemble, lowest energy first.

    Args:
        structure: The starting geometry, its charge and its multiplicity.
        search: Which space to sample.
        method: GFN parametrization; CREST accepts GFN1/GFN2 and GFN-FF.
        effort: How hard to search.
        solvent: ALPB implicit solvent name, or None for gas phase.
        temperature_k: Sampling temperature; None uses the configured default.

    Returns:
        The ensemble members ordered by energy.

    Raises:
        TimeBudgetError: the search was killed at its timeout, opening with `TIME_BUDGET_MARKER`.
        CliError: CREST is absent, exited non-zero, or wrote no ensemble.
        ValueError: the method is not one CREST accepts.
    """
    path = binary_path()
    if path is None:
        raise CliError(
            f"the {settings.crest_binary!r} binary is not installed: conformer, tautomer "
            "and protomer sampling are unavailable in this deployment"
        )
    if method not in _METHOD_FLAGS:
        raise ValueError(f"CREST does not support method {method!r}")

    argv = [path, "input.xyz", *_METHOD_FLAGS[method], *_SEARCH_FLAGS[search]]
    argv += [*_EFFORT_FLAGS[effort], "--chrg", str(structure.charge)]
    argv += ["--uhf", str(structure.uhf)]
    argv += ["--temp", str(temperature_k or settings.xtb_thermo_temperature_k)]
    if settings.crest_threads > 0:
        argv += ["-T", str(settings.crest_threads)]
    if solvent is not None:
        argv += ["--alpb", _safe(solvent, "solvent")]

    with scratch_dir("crest-") as directory:
        (directory / "input.xyz").write_text(_to_xyz(structure))
        environment = _environment()
        # Announced at start: a search has no progress channel, so this line tells an operator the
        # busy pod is working and against what budget.
        logger.info(
            "crest %s sampling started: atoms=%d effort=%s budget=%ss",
            search,
            len(structure.elements),
            effort,
            settings.crest_timeout_seconds,
        )
        try:
            completed = run_isolated(
                argv,
                cwd=directory,
                env=environment,
                timeout=settings.crest_timeout_seconds,
                label=search,
            )
        except subprocess.TimeoutExpired as error:
            # A stop by the clock, named as one; `run_isolated` already logged and counted the kill.
            raise TimeBudgetError(
                f"{TIME_BUDGET_MARKER} crest {search} timed out after "
                f"{settings.crest_timeout_seconds}s; "
                "a larger molecule needs a longer budget or a cheaper effort level"
            ) from error
        if completed.returncode != 0:
            tail = "\n".join(completed.stdout.splitlines()[-12:])
            raise CliError(f"crest {search} failed (exit {completed.returncode}):\n{tail}")
        candidate = directory / _ENSEMBLE_FILE[search]
        if not candidate.exists():
            raise CliError(f"crest {search} wrote no {_ENSEMBLE_FILE[search]}")
        members = _read_ensemble(candidate, structure, search)
        degeneracies = _read_degeneracies(directory, len(members))
        paired = [
            member.model_copy(update={"degeneracy": degeneracy})
            for member, degeneracy in zip(members, degeneracies, strict=True)
        ]
        # Sorted rather than assumed: Chemclaw3 reads `conformers[0]` as the lowest and truncates
        # the list.
        return sorted(paired, key=lambda member: member.energy_hartree)


def _environment() -> dict[str, str]:
    """The scrubbed environment the child runs in."""
    environment = {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}
    if settings.crest_threads > 0:
        environment["OMP_NUM_THREADS"] = str(settings.crest_threads)
    return environment
