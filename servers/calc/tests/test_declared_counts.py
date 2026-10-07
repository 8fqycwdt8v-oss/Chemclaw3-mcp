"""Every count this server writes about itself, checked against the surface it describes.

A tool docstring is the prompt and cannot hold a live expression, so the counts in it, in the
manifest comments and in the README are derived here from `COMPUTE_TOOLS` and the manifest.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from chemclaw_mcp_calc import tools
from chemclaw_mcp_calc.engine.identity import COMPUTE_TOOLS, calculation_identity

_SERVER = Path(__file__).resolve().parents[1]
_MANIFEST = yaml.safe_load((_SERVER / "connector.yaml").read_text())
_README = (_SERVER / "README.md").read_text()

_WORDS = {
    3: "three",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
    17: "seventeen",
    18: "eighteen",
    20: "twenty",
}


def _word(count: int) -> str:
    """The English spelling these documents use, so prose can be compared with a number."""
    return _WORDS[count]


def _compute_tools_taking(subject: str) -> int:
    """How many compute tools name `subject` among the arguments they accept."""
    return sum(subject in accepted for accepted, _ in COMPUTE_TOOLS.values())


COMPUTE = len(COMPUTE_TOOLS)
SMILES_IN = _compute_tools_taking("smiles")
STRUCTURE_IN = _compute_tools_taking("structure")
SERVED = len(_MANIFEST["endpoint"]["tools"])
HELPERS = SERVED - COMPUTE


def test_the_manifest_partitions_the_surface_the_way_the_groups_do() -> None:
    """The arithmetic every other assertion here rests on, so a bad premise cannot pass quietly."""
    assert SMILES_IN + STRUCTURE_IN == COMPUTE
    assert set(_MANIFEST["endpoint"]["state_changing"]) == set(COMPUTE_TOOLS)
    assert len(_MANIFEST["endpoint"]["read_only"]) == HELPERS
    assert SERVED == COMPUTE + HELPERS


def test_calculation_key_offers_the_number_of_tools_it_actually_accepts() -> None:
    """The sentence a model reads before choosing what to name in `tool`."""
    assert tools.calculation_key.__doc__ is not None
    assert f"one of the {_word(COMPUTE)} on this server" in tools.calculation_key.__doc__


def test_calculation_key_names_only_tools_that_exist_as_the_ones_without_a_key() -> None:
    """A named exception must be callable, or the docstring routes a model into a refusal.

    Derived rather than transcribed: the keyless set is whatever the SMILES-in tools actually answer
    with no `key`. (The structure-in primitives all key on the geometry they are handed, so there is
    nothing to discover there and no embedding to pay for.)
    """
    keyless = {
        tool
        for tool, (accepted, _) in COMPUTE_TOOLS.items()
        if "smiles" in accepted and _answers_without_a_key(tool)
    }
    assert keyless == {"predict_logd"}
    doc = tools.calculation_key.__doc__ or ""
    assert "`predict_logd`" in doc
    for tool in COMPUTE_TOOLS:
        if tool not in keyless:
            assert f"`{tool}`" not in doc, f"{tool} has a key and must not be named as lacking one"
    assert "compute_thermochemistry" not in doc


def _answers_without_a_key(tool: str) -> bool:
    """Whether `tool` derives an identity carrying no key — which a *refusal* is not.

    The four tools that need a program this image may not carry refuse instead of answering, and a
    refusal is the opposite of what this set is about: "no key" reads to a caller as "not computed
    yet", which is why it has to be named in the docstring, while a refusal says what to fix. Before
    the two binary-only xTB panels refused, they answered a key naming `xtb-absent` and fell through
    this comprehension as "has a key" — which was true and was the defect.
    """
    try:
        return calculation_identity(tool, {"smiles": "CCO"}).key is None
    except ValueError:
        return False


def test_a_tool_the_docstring_used_to_name_is_still_not_on_this_server() -> None:
    """Why the assertion above is worth its own test: the old sentence named a tool that refuses."""
    with pytest.raises(ValueError, match="not a compute tool on this server"):
        calculation_identity("compute_thermochemistry", {"smiles": "CCO"})


def test_calculation_identity_documents_the_split_it_dispatches_on() -> None:
    """`Args:` tells a caller which subject each group takes; both counts are derivable."""
    doc = calculation_identity.__doc__ or ""
    assert f"One of the {_word(COMPUTE)} compute tools' names" in doc
    assert (
        f"`smiles` for the {_word(SMILES_IN)} SMILES-in tools, `structure` for the "
        f"{_word(STRUCTURE_IN)} primitives" in doc
    )


def test_the_manifest_comments_count_the_lists_they_introduce() -> None:
    """The comments a reviewer reads beside the declared surface."""
    text = (_SERVER / "connector.yaml").read_text()
    assert f"# The {_word(SMILES_IN)} backing Chemclaw3's own SMILES-in tools" in text
    assert f"# The {_word(STRUCTURE_IN)} primitives" in text
    assert f"# {_word(HELPERS).capitalize()} helpers that compute nothing" in text


def test_the_readme_counts_and_its_tables_agree_with_each_other() -> None:
    """Both halves, because the README miscounted *and* omitted a tool from each table."""
    assert f"{_word(SERVED).capitalize()} tools, and **no model reads any of them**" in _README
    assert f"**{_word(SMILES_IN).capitalize()}** back its SMILES-in tools" in _README
    assert f"**{_word(STRUCTURE_IN)}** are structure-in primitives" in _README
    assert f"**{_word(HELPERS)}** are helpers that compute nothing" in _README
    for tool in _MANIFEST["endpoint"]["tools"]:
        assert f"`{tool}`" in _README, f"{tool} is served and appears nowhere in the README"
