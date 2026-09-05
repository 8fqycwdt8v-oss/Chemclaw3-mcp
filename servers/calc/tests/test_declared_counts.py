"""Every count this server writes down about itself, checked against the surface it describes.

A tool docstring **is** the prompt, so it cannot hold a live expression — but a test can hold the
prose against the thing it counts, and that is the difference between a number that decays and one
that fails. It had decayed in five places at once, all describing the same server: `calculation_key`
offered "one of the nine on this server" over seventeen compute tools and named
`compute_thermochemistry` as a tool with no key, which is not on this server at all and whose
identity raises — so a model following that sentence calls something that refuses. The manifest's
own comments read "The eight backing…" over a list of ten and "The six primitives" over a list of
seven; `MODULES.md` said "Seventeen tools in three groups" over a breakdown summing to eighteen; and
the README's two tables were each missing a tool as well as miscounting.

Nothing in that list was a lie when it was written. Each was true of a commit, and this repository's
own rule is that a number in prose is a claim about a commit — so the numbers are derived here, from
`COMPUTE_TOOLS` and from the manifest, and spelled the way each document spells them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from chemclaw_mcp_calc import tools
from chemclaw_mcp_calc.engine.identity import COMPUTE_TOOLS, calculation_identity

_SERVER = Path(__file__).resolve().parents[1]
_MANIFEST = yaml.safe_load((_SERVER / "connector.yaml").read_text())
_MODULES = (_SERVER.parents[1] / "MODULES.md").read_text()
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
        if "smiles" in accepted and calculation_identity(tool, {"smiles": "CCO"}).key is None
    }
    assert keyless == {"predict_logd"}
    doc = tools.calculation_key.__doc__ or ""
    assert "`predict_logd`" in doc
    for tool in COMPUTE_TOOLS:
        if tool not in keyless:
            assert f"`{tool}`" not in doc, f"{tool} has a key and must not be named as lacking one"
    assert "compute_thermochemistry" not in doc


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


def test_the_catalogue_row_counts_the_surface_it_catalogues() -> None:
    """`MODULES.md` is the fleet's catalogue; its calc row is a claim about this manifest."""
    assert f"{_word(SERVED).capitalize()} tools in three groups" in _MODULES
    assert f"**{_word(SMILES_IN).capitalize()}** back its SMILES-in tools" in _MODULES
    assert f"**{_word(STRUCTURE_IN).capitalize()}** structure-in primitives" in _MODULES
    assert f"**{_word(HELPERS).capitalize()}** helpers that compute nothing" in _MODULES


def test_the_readme_counts_and_its_tables_agree_with_each_other() -> None:
    """Both halves, because the README miscounted *and* omitted a tool from each table."""
    assert f"{_word(SERVED).capitalize()} tools, and **no model reads any of them**" in _README
    assert f"**{_word(SMILES_IN).capitalize()}** back its SMILES-in tools" in _README
    assert f"**{_word(STRUCTURE_IN)}** are structure-in primitives" in _README
    assert f"**{_word(HELPERS)}** are helpers that compute nothing" in _README
    for tool in _MANIFEST["endpoint"]["tools"]:
        assert f"`{tool}`" in _README, f"{tool} is served and appears nowhere in the README"
