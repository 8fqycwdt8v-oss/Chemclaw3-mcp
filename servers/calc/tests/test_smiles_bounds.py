"""A caller-supplied SMILES must not be able to kill this pod, and must not flood the model.

RDKit's canonicaliser recurses over the graph, and a long linear molecule overflows the C stack:
a `SIGSEGV` no `except` can catch, taking every in-flight calculation with it.
`mcp_server_kit.limits` bounds the input.

Each case runs in a child process, because a regression is a segfault: exit `0` with a refusal is
the fix, `-11` the defect. Call sites are parametrised, including `read_only` tools that bypass
admission, so the guard must sit in the shared definition.
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from chemclaw_mcp_calc.engine.chem import InvalidSmilesError, require_canonical_smiles
from chemclaw_mcp_calc.engine.pka import PkaInput, predict_pka
from mcp_server_kit.limits import MAX_ECHO_CHARS, MAX_MOLECULE_ATOMS, MAX_SMILES_CHARS

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
    # This path produces the biggest string, so the refusal must not echo the caller's megastring
    # back into the model's context.
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

    A short SMILES can parse to an enormous molecule, so this uses one inside the character bound
    and outside the atom bound.
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


def test_a_refusal_of_an_accepted_structure_is_echoed_bounded_too() -> None:
    """A refusal of an accepted structure echoes the SMILES bounded too.

    A long alkane parses and is inside both bounds but has no ionisable site, so `predict_pka`
    refuses on chemistry; that message must not interpolate the full string.
    """
    payload = "C" * 1500
    with pytest.raises(ValueError) as raised:
        predict_pka(PkaInput(smiles=payload))
    message = str(raised.value)
    assert "C" * (MAX_ECHO_CHARS + 1) not in message, f"the refusal is {len(message)} characters"
    assert "1500 chars" in message, "the length must survive the truncation"
