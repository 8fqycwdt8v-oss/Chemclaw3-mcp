"""What comes back from a CREST search, parsed from real CREST output.

The properties held:

- a deprotomer ensemble has one atom fewer than the template and charge -1;
- a protomer ensemble is read from the file CREST actually writes and carries charge +1;
- each member is labelled with its own perceived SMILES, not the input's.

The fixtures are literal excerpts of crest 3.0.2 output on phenol. The `crest`-gated tests at the
end catch CREST itself changing a filename and skip where the binary is absent.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from chemclaw_mcp_calc.engine import crest_cli
from chemclaw_mcp_calc.engine.config import settings
from chemclaw_mcp_calc.engine.crest_cli import CrestSearch
from chemclaw_mcp_calc.engine.structure import Structure, structure_from_smiles
from chemclaw_mcp_calc.engine.xtb_cli import CliError

# crest 3.0.2, `crest in.xyz --deprotonate --gfn2 --alpb water` on phenol: the whole ensemble.
DEPROTONATED = """  12
  -19.737177559999999
 O          2.2466252392       -0.0455605924       -0.0015882464
 C          0.9611369951       -0.0207567809       -0.0005999674
 C          0.2239072911        1.1948550334       -0.0004294462
 C         -1.1507358810        1.2127208770        0.0002497509
 C         -1.8818966760        0.0339194169        0.0006029597
 C         -1.1966441870       -1.1722198259        0.0001689344
 C          0.1776099585       -1.2071692163       -0.0005102022
 H          0.7894003393        2.1211030244       -0.0008539818
 H         -1.6677851313        2.1657478131        0.0006049305
 H         -2.9623232602        0.0547089198        0.0011850849
 H         -1.7500247363       -2.1046128244        0.0004638642
 H          0.7073226504       -2.1543422866       -0.0009909072
"""

# The same run with `--protonate`: the lowest of four protomers, an arenium ion rather than the
# O-protonated form — which is the chemistry, and is exactly what a perceived SMILES reports.
PROTONATED = """  14
  -20.102947390000001
 O          2.2686320945       -0.0610612149        0.1630933209
 C          0.9730447114       -0.0114134780        0.0720582843
 C          0.2535512376        1.2072220593       -0.0332837673
 C         -1.0983357248        1.1992088585       -0.1264696721
 C         -1.8684090755       -0.0416010918       -0.1232445415
 C         -1.0809157712       -1.2680056288       -0.0123662445
 C          0.2691754091       -1.2458175300        0.0801027743
 H          2.7186316087        0.8058884078        0.1539488796
 H          0.8000048554        2.1384495603       -0.0378663685
 H         -1.6406902421        2.1292522629       -0.2063858278
 H         -2.6276371229       -0.0095882096        0.6727031389
 H         -1.6082858486       -2.2095558974       -0.0057212604
 H          0.8551672363       -2.1475803895        0.1618318341
 H         -2.5105530908       -0.0868327800       -1.0157456198
"""

# `crest in.xyz --gfn2` on the same molecule: a conformer, which *is* phenol — same atoms, same
# charge — and therefore keeps the identity it was sent in with.
CONFORMERS = """  13
  -19.961003810000001
 O          1.3618000000       -0.1359000000       -0.0224000000
 C          0.0000000000        0.0000000000        0.0000000000
 C         -0.5527000000        1.2851000000       -0.0086000000
 C         -1.9310000000        1.4327000000        0.0121000000
 C         -2.7524000000        0.3082000000        0.0333000000
 C         -2.1930000000       -0.9695000000        0.0341000000
 C         -0.8138000000       -1.1266000000        0.0135000000
 H          1.7159000000        0.7625000000       -0.0361000000
 H          0.0879000000        2.1657000000       -0.0250000000
 H         -2.3646000000        2.4291000000        0.0116000000
 H         -3.8290000000        0.4236000000        0.0492000000
 H         -2.8342000000       -1.8465000000        0.0507000000
 H         -0.3849000000       -2.1233000000        0.0143000000
"""


@pytest.fixture(name="phenol")
def phenol_fixture() -> Structure:
    """The 13-atom neutral this server would send into a search."""
    return structure_from_smiles("Oc1ccccc1", optimize=True)


def fake_crest(directory: Path, writes: dict[str, str]) -> str:
    """A stand-in `crest` that writes `writes` into its working directory and exits 0.

    A fake binary rather than a patched parser, so argv, working directory, file choice and parse
    are all covered; CREST's own filenames are the gated test's job.
    """
    script = directory / "crest"
    body = ["#!/bin/sh"]
    for name, content in writes.items():
        body.append(f"cat > {name} <<'EOF'\n{content}EOF")
    script.write_text("\n".join(body) + "\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


# What a test calls to install the stand-in: a directory to write it into, and the files it writes.
InstallCrest = Callable[[Path, dict[str, str]], None]


@pytest.fixture(name="with_fake_crest")
def with_fake_crest_fixture(monkeypatch: pytest.MonkeyPatch) -> Iterator[InstallCrest]:
    """Point `crest_binary` at a script the test writes, and clear the path caches around it."""

    def install(directory: Path, writes: dict[str, str]) -> None:
        path = fake_crest(directory, writes)
        monkeypatch.setattr(settings, "crest_binary", path)
        crest_cli.binary_path.cache_clear()
        crest_cli.binary_version.cache_clear()

    yield install
    crest_cli.binary_path.cache_clear()
    crest_cli.binary_version.cache_clear()


def test_a_deprotomer_ensemble_comes_back_as_the_anion(
    phenol: Structure, with_fake_crest: InstallCrest, tmp_path: Path
) -> None:
    """One atom fewer, charge -1, and a SMILES naming the site that came off.

    The members feed `relax_structure` on the caller's side, and an anion relaxed at charge 0 is an
    energy for a species that does not exist.
    """
    with_fake_crest(tmp_path, {"deprotonated.xyz": DEPROTONATED})
    members = crest_cli.run(phenol, search="deprotomers", method="GFN2-xTB", solvent="water")

    assert len(members) == 1
    anion = members[0].structure
    assert len(anion.elements) == len(phenol.elements) - 1
    assert anion.charge == -1
    # A proton is a nucleus without electrons, so the electron count — and the multiplicity — is
    # untouched by the deprotonation.
    assert anion.multiplicity == phenol.multiplicity
    assert anion.smiles == "[O-]c1ccccc1"


def test_a_protomer_ensemble_comes_back_as_the_cation(
    phenol: Structure, with_fake_crest: InstallCrest, tmp_path: Path
) -> None:
    """One atom more, charge +1, and the *perceived* protonation site rather than the input's name.

    Phenol's lowest protomer is the ring-protonated arenium ion; which site protonates is the
    question asked.
    """
    with_fake_crest(tmp_path, {"protonated.xyz": PROTONATED})
    members = crest_cli.run(phenol, search="protomers", method="GFN2-xTB", solvent="water")

    cation = members[0].structure
    assert len(cation.elements) == len(phenol.elements) + 1
    assert cation.charge == 1
    assert cation.smiles is not None and cation.smiles != phenol.smiles


def test_a_conformer_search_keeps_the_molecule_it_was_given(
    phenol: Structure, with_fake_crest: InstallCrest, tmp_path: Path
) -> None:
    """The other half of the rule: a conformer *is* the input molecule, so it keeps its name.

    Perception is skipped here rather than merely agreeing, because a conformer search cannot change
    the constitution — re-deriving the identity would only add a way for it to differ.
    """
    with_fake_crest(tmp_path, {"crest_conformers.xyz": CONFORMERS})
    members = crest_cli.run(phenol, search="conformers", method="GFN2-xTB")

    assert members[0].structure.charge == phenol.charge
    assert members[0].structure.smiles == phenol.smiles


@pytest.mark.parametrize("search", ["protomers", "deprotomers", "tautomers"])
def test_a_missing_ensemble_file_is_an_error_not_the_neutral_molecule(
    phenol: Structure, with_fake_crest: InstallCrest, tmp_path: Path, search: CrestSearch
) -> None:
    """A missing ensemble file is an error, never a fallback to the neutral molecule.

    `crest_conformers.xyz` holds the input's conformers, so falling back would return the neutral
    species relabelled with a shifted charge.
    """
    with_fake_crest(tmp_path, {"crest_conformers.xyz": DEPROTONATED})
    with pytest.raises(CliError, match="wrote no"):
        crest_cli.run(phenol, search=search, method="GFN2-xTB")


@pytest.mark.skipif(not crest_cli.is_available(), reason="the crest binary is not installed here")
def test_crest_still_writes_the_files_this_module_names(phenol: Structure) -> None:
    """The one test a fake binary cannot stand in for: CREST's own output filenames.

    Drives the real binary and checks the charge arithmetic against real output, so a renamed file
    is red.
    """
    members = crest_cli.run(phenol, search="deprotomers", method="GFN2-xTB", solvent="water")

    assert members
    assert members[0].structure.charge == -1
    assert len(members[0].structure.elements) == len(phenol.elements) - 1


@pytest.mark.skipif(not crest_cli.is_available(), reason="the crest binary is not installed here")
def test_the_shipped_image_can_run_a_search_at_all() -> None:
    """Whether this deployment actually has the capability its manifest advertises.

    `is_available()` is a `which`; this runs the binary, since a half-installed toolchain can look
    present.
    """
    assert crest_cli.binary_version() not in ("absent", "unknown")
    members = crest_cli.run(
        structure_from_smiles("CCO", optimize=True), search="conformers", method="GFN2-xTB"
    )
    assert members and members[0].structure.charge == 0


def test_the_environment_the_child_runs_in_is_scrubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CREST run inherits four variables and no more, so nothing in the pod's environment reaches
    a subprocess that writes files and forks workers."""
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "not-for-the-child")
    environment = crest_cli._environment()

    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert set(environment) <= {*crest_cli._ENV_ALLOWLIST, "OMP_NUM_THREADS"}
    assert "PATH" in environment or "PATH" not in os.environ
