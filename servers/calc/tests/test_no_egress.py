"""This server's own code holds no way to call out. Three lines, and every server ships them.

The scan covers the whole package — engine, tools and transport — because the rule is about the
server, not about one layer of it. `app.py` names loopback in its docstring, which the scanner
exempts on purpose: showing somebody how to reach the server they are running is documentation,
while naming somebody else's host is the thing being forbidden.
"""

from __future__ import annotations

from pathlib import Path

import chemclaw_mcp_calc
from mcp_server_kit.no_egress import assert_no_egress_sources

PACKAGE = Path(chemclaw_mcp_calc.__file__).parent


def test_no_module_can_reach_the_network() -> None:
    """No HTTP client imported, no remote host named — checked by AST, not by grep.

    Worth one note for this server specifically: `engine/xtb_cli.py` imports `subprocess`, which the
    scanner permits and should. A subprocess is not egress — the binary it launches is on the
    image's own filesystem, runs in a fresh temporary directory with a four-variable environment
    allowlist, and the NetworkPolicy beside this package denies it a socket regardless of what it
    tries.
    """
    assert_no_egress_sources(PACKAGE)


def test_every_answer_is_computed_in_process_with_the_guard_armed() -> None:
    """The positive half, and the one this server has to earn differently from the other three.

    `props`, `chem` and `safety` prove sufficiency by pointing at a vendored, checksummed corpus.
    This server ships **no dataset at all**: every number is computed from tblite's compiled GFN
    parameters, RDKit's Crippen/QED tables and closed-form arithmetic, all of which arrive inside
    their own wheels. So the property to demonstrate is that the computation itself needs nothing
    from outside the process — and the whole suite runs with the egress guard armed (root
    `conftest.py`), which makes running one of each kind of calculation the proof.

    The failure this rules out is the quiet one a numerical library can produce: a package fetching
    parameters, model weights or a licence check on first use. The guard raises `EgressForbidden`
    rather than letting it succeed, so any such call fails this test instead of silently making the
    image depend on a network at runtime.
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
