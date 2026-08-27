"""The `crest` binary: conformer, tautomer, protomer and non-covalent-complex sampling.

**This image ships the binary** (`Containerfile`, crest 3.0.2 from conda-forge). It did not, for as
long as this module existed, and that absence is what let three of the four searches ship broken:
`--deprotonate` raised a validation error on every molecule, `--protonate` looked for a filename no
version of CREST writes, and both would have labelled a charged species with the neutral's charge.
None of it was visible, because the only machine that ever ran the tests was one where the whole
module refused on line one. `tests/test_crest_ensembles.py` now drives the real binary where it is
present and a stand-in where it is not.

A deployment that removes the binary still works: `is_available()` goes False and the searches
refuse by name rather than degrading into a single-conformer answer.

CREST removes the caveat attached to every other number here. Everything else describes **one**
conformer — whichever geometry was embedded and relaxed — and for a flexible molecule that is a
shape, not the molecule. CREST searches conformational space by metadynamics and returns the
ensemble with the energies and rotamer degeneracies that make populations computable.

Four searches, one binary:

- **conformers** — the ensemble. Measured on n-butane: two unique conformers, the anti at 59% once
  degeneracy is weighted in — the textbook answer.
- **tautomers** — enumerate and rank tautomers, which is the one question in this system where
  getting it wrong silently invalidates *every* downstream number, because a pKa, a Fukui ranking
  and a reaction energy all describe whichever tautomer was drawn.
- **protomers** / **deprotomers** — where a molecule protonates or deprotonates, ranked.
- **complex** (`--nci`) — how *two* molecules associate. The wall potential is what makes it a
  binding search rather than two molecules drifting apart.

**This module returns the ensemble and nothing else.** Boltzmann populations, conformational
entropy and the ensemble free-energy correction are pure arithmetic over these energies and
degeneracies, and they stayed in Chemclaw3 with the durable jobs that report them — the split is
physics here, orchestration and arithmetic there. What crosses the wire is what only the binary can
produce.

**Licensing, stated once because it is a real decision and not an engineering one.** CREST is
GPL-3.0. It is invoked here as a separate process over files, never linked, so the usual analysis is
that it does not affect the licence of this codebase — but shipping the binary in an image is a
distribution question that belongs to whoever owns the product, not to this module.

**Security.** Same rule as `xtb_cli` and for the same reason: argv list, `shell=False`, a fresh
temporary directory, a scrubbed environment, a timeout, and no value that could be read as an
option. The run itself uses `xtb_cli.run_isolated`: CREST forks worker subprocesses for its parallel
metadynamics steps, so a naive `subprocess.run(timeout=...)` only ever killed the one PID it tracked
and left every forked worker running as an orphan, still writing into a temporary directory that had
already been removed.
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

# What to search for. Each is a different CREST run mode over the same machinery.
# Searches over **one** molecule. Separate from the union below because the
# ensemble tool takes exactly these — a complex search needs a second molecule, so it is a
# different tool rather than a fifth option on this one.
EnsembleSearch = Literal["conformers", "tautomers", "protomers", "deprotomers"]
CrestSearch = Literal["conformers", "tautomers", "protomers", "deprotomers", "complex"]
_SEARCH_FLAGS: dict[CrestSearch, list[str]] = {
    "conformers": [],
    "tautomers": ["--tautomerize"],
    "protomers": ["--protonate"],
    "deprotomers": ["--deprotonate"],
    # Non-covalent mode: adds a logfermi wall potential around the pair, without which a
    # metadynamics search simply lets two molecules drift apart instead of sampling how
    # they bind (`crest_search`).
    "complex": ["--nci"],
}

# How hard to search. `quick` trades completeness for wall clock and is the right default
# for a screening question; `extensive` is for the case where a missed conformer changes
# the answer. CREST's own names, so the mapping stays legible against its documentation.
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

# The file each search writes its ensemble to. **Exactly one name per search, and the absence of a
# fallback is the correction**: `crest_conformers.xyz` used to stand behind the three
# constitution-changing searches, and it holds the *input* molecule's conformers. A protonation run
# that wrote no ensemble would therefore have returned the neutral species' conformers, relabelled
# with a shifted charge — a converged energy for a molecule nobody asked about. Measured against
# crest 3.0.2: `--protonate` writes `protonated.xyz` (not `protomers.xyz`, which this table named
# and which never exists), `--deprotonate` writes `deprotonated.xyz`, `--tautomerize` writes
# `tautomers.xyz`. Missing the file is now an error, which is what it always was.
_ENSEMBLE_FILE: dict[CrestSearch, str] = {
    "conformers": "crest_conformers.xyz",
    "tautomers": "tautomers.xyz",
    "protomers": "protonated.xyz",
    "deprotomers": "deprotonated.xyz",
    "complex": "crest_conformers.xyz",
}

# What each search does to the net charge. CREST adds or removes a **proton** — a nucleus with no
# electrons — so the electron count is unchanged and the multiplicity carries over untouched; only
# the charge moves. Getting this wrong is not a labelling error: the members feed `relax_structure`
# and `compute_hessian` on the caller's side, and an anion relaxed at charge 0 is a converged
# number for a species that does not exist.
_CHARGE_SHIFT: dict[CrestSearch, int] = {
    "conformers": 0,
    "tautomers": 0,
    "protomers": +1,
    "deprotomers": -1,
    "complex": 0,
}

# Whether every member of this search *is* the molecule that was sent in. A conformer or binding
# mode is; a tautomer, protomer or deprotomer is a different constitution, so carrying the input's
# SMILES onto it would name the wrong molecule — the label is perceived from the geometry instead.
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
    output = subprocess.run(
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

    Format: a count, then one line per conformer whose first field is how many rotamers
    it represents. Only the conformer search writes it; a tautomer or protomer ensemble
    has no rotamer grouping, and every member then weighs the same.
    """
    members = directory / "cre_members"
    if not members.exists():
        return [1] * count
    rows = [line.split() for line in members.read_text().splitlines() if line.split()]
    degeneracies = [int(row[0]) for row in rows[1:] if row[0].isdigit()]
    return degeneracies if len(degeneracies) == count else [1] * count


def _read_ensemble(path: Path, template: Structure, search: CrestSearch) -> list[EnsembleMember]:
    """Parse a multi-structure XYZ; CREST writes the energy on each comment line.

    **The elements are read from the file, not inherited from the template**, and that is the
    difference between this parser and `xtb_cli._from_xyz`. xtb echoes the same atoms in the same
    order, so trusting the template turns an element mismatch into a loud failure there. CREST does
    neither: `--protonate` returns *more* atoms than it was given, `--deprotonate` fewer, and all
    three protonation modes presort the input so that every hydrogen is written last. Measured on
    phenol before this: the deprotomer search raised "12 positions for 13 elements" — it had never
    once returned an ensemble — and had the counts happened to match, the template's element order
    would have relabelled the atoms of a molecule that had been resorted underneath it.

    Charge comes from the template plus the search's own shift, and the multiplicity carries over:
    a proton is a nucleus without electrons, so the electron count does not move.

    The SMILES is perceived from the geometry for a search that changes the constitution, because
    the input's SMILES is the wrong label for a tautomer or a protomer — and perception answers
    `None` rather than guessing, so a member whose bonding cannot be read travels unlabelled.
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
        CliError: CREST is absent, timed out, exited non-zero, or wrote no ensemble.
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
        # Announced before it starts, and this is the one place in the fleet where that is not
        # noise. A caller waiting on a CREST search waits minutes to hours and gets nothing until
        # the answer — the fleet promises statelessness, so there is no progress channel and there
        # is not going to be one. One line at the start is what tells an operator that the pod
        # burning CPU is working rather than wedged, and what the budget it is working against is.
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
            # See the sibling in `xtb_cli`: `run_isolated` has already logged and counted the kill,
            # so this raise is not the only record of a four-hour run being abandoned.
            raise CliError(
                f"crest {search} timed out after {settings.crest_timeout_seconds}s; "
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
        # Sorted here rather than assumed. CREST does write its ensembles lowest first, but
        # Chemclaw3's `ConformerEnsemble.lowest` is `conformers[0]` and the member list is
        # truncated to `max_members` on the way to a reader — so a file that ever came back
        # in another order would silently drop the lowest conformer and report the wrong
        # one, which is not a failure any test would show as a failure.
        return sorted(paired, key=lambda member: member.energy_hartree)


def _environment() -> dict[str, str]:
    """The scrubbed environment the child runs in."""
    environment = {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}
    if settings.crest_threads > 0:
        environment["OMP_NUM_THREADS"] = str(settings.crest_threads)
    return environment
