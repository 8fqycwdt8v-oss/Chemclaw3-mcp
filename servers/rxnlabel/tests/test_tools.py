"""The tool surface: what a batch answers, and what it says when it cannot answer.

The caller is a background drain over a corpus that may be millions of rows, so the properties that
matter here are the ones that decide whether such a drain converges: a batch that partially fails
must return what it has, an unlabellable reaction must be *absent* rather than wrong, and the
version must name every component whose output survives into a label.
"""

from __future__ import annotations

import pytest
from chemclaw_mcp_rxnlabel import tools
from chemclaw_mcp_rxnlabel.engine import roles, version

BUCHWALD = (
    "Brc1ccccc1.NC1CCCCC1"
    ">CC(C)(C)P(C(C)(C)C)C(C)(C)C.CC(C)(C)[O-].CC#N.CC(=O)O[Pd]OC(C)=O"
    ">c1ccc(NC2CCCCC2)cc1"
)


async def test_represent_reaction_answers_the_question_it_is_named_for() -> None:
    """Reaction representation *and* per-species representation with roles — one call."""
    found = await tools.represent_reaction(
        BUCHWALD, ["CC(C)(C)P(C(C)(C)C)C(C)(C)C", "c1ccc(NC2CCCCC2)cc1"]
    )
    ligand, product = found.species
    assert ligand.role == roles.LIGAND
    assert product.role == roles.PRODUCT
    # The molecular half: each species carries what a facet query filters on.
    assert product.functional_groups == ["secondary amine", "aniline", "arene"]
    assert product.scaffold == "c1ccc(NC2CCCCC2)cc1"
    assert ligand.scaffold is None, "an acyclic species has no scaffold, and null says so"


async def test_omitting_the_species_list_classifies_everything_in_the_reaction() -> None:
    """The one-shot form is for a person, who should not have to restate the reaction's contents."""
    found = await tools.represent_reaction(BUCHWALD)
    assert {s.role for s in found.species} == {
        roles.STARTING_MATERIAL,
        roles.LIGAND,
        roles.BASE,
        roles.SOLVENT,
        roles.CATALYST,
        roles.PRODUCT,
    }


async def test_the_answer_is_positional_against_the_species_that_were_sent() -> None:
    """The contract the caller's ordinals depend on.

    A caller's species order comes from its own record; the reaction string groups the agents
    together. If the answer were ordered by the reaction instead, every stored role on every
    reaction with a solvent would be attached to the wrong structure.
    """
    sent = ["CC#N", "Brc1ccccc1", "CC(=O)O[Pd]OC(C)=O"]
    found = await tools.represent_reactions(
        [tools.ReactionRequest(id="r", reaction_smiles=BUCHWALD, species=sent)]
    )
    assert [s.role for s in found.results[0].species] == [
        roles.SOLVENT,
        roles.STARTING_MATERIAL,
        roles.CATALYST,
    ]


async def test_a_batch_returns_what_it_could_do_and_omits_what_it_could_not() -> None:
    """A partial batch must not be a failed one: a drain records what it got and moves on.

    A reaction that cannot be read is *absent* from `results` rather than present with empty
    fields, because the two mean different things to the caller — absent is "not this pass",
    present-and-empty would be "we looked and there is nothing", which would stamp the row.
    """
    found = await tools.represent_reactions(
        [
            tools.ReactionRequest(id="good", reaction_smiles=BUCHWALD, species=["CC#N"]),
            tools.ReactionRequest(id="bad", reaction_smiles="not a reaction", species=["CC#N"]),
            tools.ReactionRequest(id="no-product", reaction_smiles="CCO.CC(=O)O>>", species=[]),
        ]
    )
    assert [r.id for r in found.results] == ["good"]


async def test_an_oversized_batch_is_refused_with_the_number_to_ask_for() -> None:
    """A body under the transport's byte cap can still be minutes of transformer time.

    Refused as a `ValueError`, which `mcp_server_kit` passes through untouched — so the caller sees
    the worded refusal and classifies it as bad data rather than retrying it as an outage.
    """
    too_many = [
        tools.NamingRequest(id=str(n), reaction_smiles=BUCHWALD) for n in range(tools.MAX_BATCH + 1)
    ]
    with pytest.raises(ValueError, match="batch limit"):
        await tools.name_reactions(too_many)


async def test_the_version_names_every_component_and_marks_the_absent_ones() -> None:
    """A caller stores this beside a label, and a row is stale when it differs.

    The `absent` entries are what make optional dependencies safe here: a corpus labelled without
    the mapper carries a different version from one labelled with it, so installing the extra
    re-opens those rows instead of leaving two qualities of answer under one label forever.
    """
    reported = await tools.labeller_version()
    assert reported.version == version.labeller_version()
    assert set(reported.components) == {"server", "rdkit", "atom_mapper", "reaction_namer"}
    assert reported.components["rdkit"] != "absent", "RDKit is not optional"
    # In a checkout without the `models` extra, both models report absent — and the version says so
    # rather than claiming a labelling quality it did not have.
    if reported.components["atom_mapper"] == "absent":
        assert "mapper@absent" in reported.version


async def test_naming_reports_a_miss_rather_than_a_placeholder() -> None:
    """`OtherReaction` is not a name, and neither is a null passed off as one.

    Without the classifier installed every field is null, which is the same shape a genuine
    no-match produces — the two are told apart by `labeller_version`, not by the payload, because
    the payload is about the reaction and the version is about the deployment.
    """
    found = await tools.name_reaction(BUCHWALD)
    assert found.id == "1"
    assert found.rxno_id is None, "this server never invents an ontology id"
    assert found.confidence is None, "a SMIRKS matched or it did not"
    if found.named_reaction is None:
        assert found.method is None, "a method on an unnamed row would claim a derivation"
