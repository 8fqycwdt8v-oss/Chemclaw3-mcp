"""A caller-supplied SMILES must not be able to kill this pod, and must not flood the model.

**RDKit's canonicaliser recurses over the molecular graph, and a long enough linear molecule
overflows the C stack.** The process dies with `SIGSEGV` — an exit code, not an exception, so no
`try`/`except` anywhere in this server can catch it and no amount of admission control can contain
it. `mcp_server_kit.limits` exists for exactly this and four servers already apply it; this one is
the heaviest and was the last without it, which made the failure *worse* here than anywhere else:
a `calc` pod carries in-flight CREST searches that have been running for minutes, and one ~20 kB
`tools/call` — far inside the 1 MB body cap — takes every one of them down with it.

**The test has to run in a child process**, because a regression is a segfault: an in-process
assertion would take the test runner with it and report nothing. So each case is a `python -c`
whose exit code is the assertion — `0` with a refusal on stdout is the fix, `-11` is the defect.

Two of the entry points below (`calculation_key`, `embed_structure`) are `read_only` in the
manifest, which is why the parametrisation names call sites rather than testing the one function
they share: a `read_only` tool is reachable under an *unapproved* plan and sits outside
`engine/admission.py`, so the guard has to be in the definition every one of them funnels through,
not on the tools a plan gate happens to hold back.
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from chemclaw_mcp_calc.engine.chem import InvalidSmilesError, require_canonical_smiles
from mcp_server_kit.limits import MAX_MOLECULE_ATOMS, MAX_SMILES_CHARS

# Long enough to overflow the canonicaliser's C stack on the measured build (20,000 atoms
# segfaults; 8,000 is an OOM kill), and far past `MAX_SMILES_CHARS` either way.
_MEGASTRING = "C" * 20_000

# One expression per reachable call site, each naming a tool the manifest serves. Every one of them
# reached `require_canonical_smiles` with the caller's raw string before this bound existed.
_CALL_SITES = {
    "require_canonical_smiles": (
        "from chemclaw_mcp_calc.engine.chem import require_canonical_smiles as f; f(S)"
    ),
    "embed_structure": (
        "from chemclaw_mcp_calc.engine.structure import structure_from_smiles as f; f(S)"
    ),
    "calculation_key": (
        "from chemclaw_mcp_calc.engine.identity import calculation_identity as f;"
        " f('compute_xtb_energy', {'smiles': S})"
    ),
    "predict_solubility": (
        "from chemclaw_mcp_calc.engine.solubility import predict_solubility as f, SolubilityInput;"
        " f(SolubilityInput(smiles=S))"
    ),
    "predict_pka": (
        "from chemclaw_mcp_calc.engine.pka import predict_pka as f, PkaInput; f(PkaInput(smiles=S))"
    ),
    "predict_developability_profile": (
        "from chemclaw_mcp_calc.engine.descriptors import compute_descriptor_profile as f,"
        " DescriptorInput; f(DescriptorInput(smiles=S))"
    ),
    "search_binding_modes": (
        "from chemclaw_mcp_calc.engine.crest_search import ordered_pair as f; f(S, 'CCO')"
    ),
}


def _run_in_child(expression: str) -> subprocess.CompletedProcess[str]:
    """Run one call site against the megastring in its own process, so a crash is a return code."""
    script = (
        f"S = 'C' * {len(_MEGASTRING)}\n"
        "try:\n"
        f"    {expression}\n"
        "except Exception as error:\n"
        "    print(type(error).__name__, len(str(error)))\n"
        "else:\n"
        "    print('NO-REFUSAL 0')\n"
    )
    return subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=300, check=False
    )


@pytest.mark.parametrize("tool", sorted(_CALL_SITES))
def test_a_megastring_smiles_is_refused_rather_than_crashing_the_pod(tool: str) -> None:
    """Every SMILES-taking call site answers with a refusal instead of a signal."""
    finished = _run_in_child(_CALL_SITES[tool])
    assert finished.returncode == 0, (
        f"{tool} died with returncode {finished.returncode} "
        f"(negative = killed by a signal; -11 is the canonicaliser's stack overflow)"
    )
    kind, length = finished.stdout.split()[-2:]
    assert kind == "InvalidSmilesError", f"{tool} answered {kind}, not a worded refusal"
    # Finding 3, checked here too because this is the path that produces the biggest string: the
    # refusal must not echo the caller's megastring back into the model's context.
    assert int(length) < 500, f"{tool}'s refusal is {length} characters"


def test_the_refusal_names_the_limit_rather_than_quoting_the_string() -> None:
    """The message is the kit's, so a chemist reads the same bound every other server states."""
    with pytest.raises(InvalidSmilesError) as raised:
        require_canonical_smiles("C" * (MAX_SMILES_CHARS + 1))
    message = str(raised.value)
    assert str(MAX_SMILES_CHARS) in message
    assert "C" * 100 not in message


def test_a_parseable_molecule_past_the_atom_ceiling_is_refused_before_canonicalisation() -> None:
    """The bound that actually stops the overflow is the atom count, not the string length.

    A SMILES can be short and still parse to an enormous molecule — `MAX_SMILES_CHARS` alone would
    let one through — so this drives the second half of the kit's pair with a string that is inside
    the character bound and outside the atom bound.
    """
    # `[H]` costs three characters per atom, so this stays well inside `MAX_SMILES_CHARS` while
    # parsing to more than `MAX_MOLECULE_ATOMS` atoms.
    atoms = MAX_MOLECULE_ATOMS + 10
    smiles = "C" * atoms
    assert len(smiles) <= MAX_SMILES_CHARS
    with pytest.raises(InvalidSmilesError) as raised:
        require_canonical_smiles(smiles)
    assert str(MAX_MOLECULE_ATOMS) in str(raised.value)


def test_an_ordinary_refusal_still_quotes_enough_of_the_string_to_act_on() -> None:
    """Truncating the echo must not cost the reason: a short bad SMILES is still quoted whole."""
    with pytest.raises(InvalidSmilesError) as raised:
        require_canonical_smiles("CCO junk")
    assert "'CCO junk'" in str(raised.value)


def test_a_long_unparseable_smiles_is_echoed_bounded() -> None:
    """A 3,000-character parse failure is head-plus-length, not 3,018 characters of the caller's."""
    with pytest.raises(InvalidSmilesError) as raised:
        require_canonical_smiles("Q" * 3_000)
    message = str(raised.value)
    assert len(message) < 300, f"the refusal is {len(message)} characters"
    assert "3000" in message, "the length must survive the truncation"
