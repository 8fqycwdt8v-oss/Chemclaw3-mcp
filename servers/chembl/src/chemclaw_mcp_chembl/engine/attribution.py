"""The notice every answer carries, because the data is CC BY-SA 3.0 and every answer quotes it.

ChEMBL asks for attribution, the release number and the ChEMBL ids (`REQUIRED.ATTRIBUTION`); the
licence asks for the credit, the licence name and a statement of changes. All of it is one
structured field, built here, so no tool can answer without it and no tool words it differently
from another. The two URLs it names are read from the checksummed subset's own `meta` table rather
than written in code: they are provenance of the file, and the server's source names no host.

This module states what the licence text says; it is not legal advice and decides no legal
question. The sui generis database right the 3.0 licence does not address is covered in the
server's README, not here.
"""

from __future__ import annotations

from pydantic import BaseModel

from chemclaw_mcp_chembl.engine import store

__all__ = ["CAVEAT", "Attribution", "attribution", "source_line"]

LICENCE = "CC BY-SA 3.0"
LICENCE_NAME = "Creative Commons Attribution-ShareAlike 3.0 Unported"
CREDIT = (
    "ChEMBL, EMBL-EBI (European Molecular Biology Laboratory, European Bioinformatics Institute)"
)
CITATION = (
    "Mendez D, Gaulton A, Bento AP, et al. ChEMBL: towards direct deposition of bioassay data. "
    "Nucleic Acids Res. 2019 47(D1):D930-D940. DOI: 10.1093/nar/gky1075"
)
CHANGES = (
    "subset + aggregation: a selection of ChEMBL compounds, their names and synonyms, and their "
    "activities aggregated per compound, target, standard type and unit (median, minimum, "
    "maximum and counts over exact-valued measurements). These are summaries computed from "
    "ChEMBL's records, not ChEMBL's records."
)
SHARE_ALIKE = (
    "An adaptation of this data that is distributed must be licensed under CC BY-SA 3.0 or a "
    "compatible licence, with this notice and the ChEMBL ids kept."
)

#: What a ChEMBL summary is not, repeated with every answer because a model reads one answer at a
#: time. The tool docstrings carry the longer form.
CAVEAT = (
    "Literature and database bioactivity summaries; not clinical evidence, not a safety or "
    "efficacy claim, not a dose or a prescribing statement. Values come from heterogeneous assays "
    "and are aggregated across them; check the assay-level records in ChEMBL before relying on one."
)


class Attribution(BaseModel):
    """The credit, licence, release and changes statement the licence and ChEMBL ask for."""

    source: str
    source_url: str
    credit: str
    licence: str
    licence_name: str
    licence_url: str
    changes: str
    share_alike: str
    citation: str
    data_tier: str


def attribution() -> Attribution:
    """The notice for the subset this process verified, naming its release and its tier."""
    found = store.meta()
    release = found["release"]
    return Attribution(
        source=f"ChEMBL release {release}",
        source_url=found["source_url"],
        credit=CREDIT,
        licence=LICENCE,
        licence_name=LICENCE_NAME,
        licence_url=found["licence_url"],
        changes=CHANGES,
        share_alike=SHARE_ALIKE,
        citation=CITATION,
        data_tier=found["tier"],
    )


def source_line(notice: Attribution) -> str:
    """The one-line `source` every answer carries beside the structured notice."""
    return f"{notice.source}, {notice.licence} — {notice.source_url}"
