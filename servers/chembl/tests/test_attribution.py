"""Every answer this server gives carries the CC BY-SA 3.0 notice, as a field, in every outcome.

ChEMBL asks for attribution and the release number, the licence asks for the credit, the licence
name and a statement of changes, and a model reads one answer at a time. So the notice is not a
README sentence: it is a structured field of each tool's result, in a hit, a miss and an empty
filter alike. The notice's two URLs come from the checksummed subset, so the file that is
distributed is the file that names its source.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from chemclaw_mcp_chembl import tools
from chemclaw_mcp_chembl.engine import store
from chemclaw_mcp_chembl.engine.attribution import CAVEAT, attribution
from pydantic import BaseModel

CALLS: list[tuple[str, dict[str, Any]]] = [
    ("chembl_lookup_compound", {"identifier": "CHEMBL25"}),
    ("chembl_lookup_compound", {"identifier": "aspirin", "kind": "name"}),
    ("chembl_lookup_compound", {"identifier": "CC(=O)Oc1ccccc1C(=O)O", "kind": "smiles"}),
    ("chembl_lookup_compound", {"identifier": "CHEMBL999999999"}),
    ("chembl_lookup_compound", {"identifier": "no such compound anywhere"}),
    ("chembl_bioactivity_summary", {"identifier": "CHEMBL25"}),
    ("chembl_bioactivity_summary", {"identifier": "CHEMBL25", "standard_type": "NoSuchType"}),
    ("chembl_bioactivity_summary", {"identifier": "CHEMBL999999999"}),
]


def _call(name: str, arguments: dict[str, Any]) -> BaseModel:
    result = asyncio.run(getattr(tools, name)(**arguments))
    assert isinstance(result, BaseModel)
    return result


@pytest.mark.parametrize(
    ("name", "arguments"), CALLS, ids=[f"{n}:{a['identifier']}" for n, a in CALLS]
)
def test_every_tool_answer_carries_the_attribution(name: str, arguments: dict[str, Any]) -> None:
    """Hit, miss, filtered to nothing: all carry the notice, the source line and the caveat."""
    answer = _call(name, arguments).model_dump()
    notice = answer["attribution"]
    release = store.meta()["release"]
    assert notice["source"] == f"ChEMBL release {release}"
    assert notice["licence"] == "CC BY-SA 3.0"
    assert notice["licence_name"] == "Creative Commons Attribution-ShareAlike 3.0 Unported"
    assert notice["licence_url"] == store.meta()["licence_url"]
    assert notice["source_url"] == store.meta()["source_url"]
    assert "subset + aggregation" in notice["changes"]
    assert "CC BY-SA 3.0" in notice["share_alike"]
    assert "Mendez" in notice["citation"] and "10.1093/nar/gky1075" in notice["citation"]
    assert notice["data_tier"] == store.meta()["tier"]
    assert f"ChEMBL release {release}" in answer["source"] and "CC BY-SA 3.0" in answer["source"]
    assert answer["caveat"] == CAVEAT


def test_the_notice_names_what_chembl_asked_to_be_kept() -> None:
    """`REQUIRED.ATTRIBUTION`: the release number displayed, the ids preserved, the paper cited."""
    notice = attribution()
    assert notice.source.endswith(store.meta()["release"])
    assert "ChEMBL ids" in notice.share_alike
    assert "EMBL-EBI" in notice.credit


def test_no_answer_model_can_be_built_without_a_notice() -> None:
    """The field is required on both result models, so a new code path cannot omit it."""
    from chemclaw_mcp_chembl.engine.bioactivity import BioactivitySummary
    from chemclaw_mcp_chembl.engine.lookup import LookupResult

    for model in (LookupResult, BioactivitySummary):
        assert model.model_fields["attribution"].is_required()
        assert model.model_fields["source"].is_required()
        assert model.model_fields["caveat"].is_required()
