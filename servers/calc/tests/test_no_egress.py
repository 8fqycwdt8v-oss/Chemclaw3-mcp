"""This server's own code holds no way to call out.

The scan covers the whole package. Loopback named in `app.py`'s docstring is exempt by design.
"""

from __future__ import annotations

from pathlib import Path

import chemclaw_mcp_calc
from mcp_server_kit.no_egress import assert_no_egress_sources

PACKAGE = Path(chemclaw_mcp_calc.__file__).parent


def test_no_module_can_reach_the_network() -> None:
    """No HTTP client imported, no remote host named — checked by AST, not by grep.

    `engine/xtb_cli.py` imports `subprocess`, which is permitted: the binary is on the image, runs
    in a temp directory with an allow-listed environment, and the NetworkPolicy denies it a socket.
    """
    assert_no_egress_sources(PACKAGE)


def test_every_answer_is_computed_in_process_with_the_guard_armed() -> None:
    """Every answer is computed in process with the guard armed.

    This server ships no dataset; numbers come from tblite's compiled parameters, RDKit's tables and
    arithmetic inside their wheels. Running one of each kind of calculation under the armed guard
    proves none fetches parameters, weights or a licence check at runtime.
    """
    from chemclaw_mcp_calc.engine.descriptors import DescriptorInput, compute_descriptor_profile
    from chemclaw_mcp_calc.engine.pka import PkaInput, predict_pka
    from chemclaw_mcp_calc.engine.solubility import SolubilityInput, predict_solubility
    from chemclaw_mcp_calc.engine.xtb import XtbInput, run_xtb

    # tblite: a full GFN2 SCF, the one thing here that loads compiled parameter data.
    assert run_xtb(XtbInput(smiles="CCO")).total_energy_hartree < 0
    # tblite with ALPB solvation, plus RDKit embedding — the pKa path touches both.
    assert predict_pka(PkaInput(smiles="CC(=O)O")).site == "acid"
    # RDKit's Crippen contribution tables.
    assert predict_solubility(SolubilityInput(smiles="CCO")).log_s_mol_per_l > -2
    # RDKit's QED parameter set, which is a separate data load from Crippen's.
    assert 0.0 < compute_descriptor_profile(DescriptorInput(smiles="CCO")).qed <= 1.0
