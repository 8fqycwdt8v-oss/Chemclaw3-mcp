"""The Molecular Transformer tokenizer's round-trip check, which must survive `python -O`.

The tokenizer is a regex, and an uncovered character silently yields fewer tokens: a confident
answer about a different molecule. The round trip is the function's whole safety, so it is an
explicit raise, not an `assert`. Imported directly, since the predictor registers only when the
optional `onmt` extra is installed.
"""

from __future__ import annotations

import pytest
from chemclaw_mcp_rxnpredict.engine.predictors.forward.molecular_transformer import (
    TOKEN_PATTERN,
    _tokenize_smiles,
)


def test_a_covered_structure_round_trips_to_spaced_tokens() -> None:
    """The success path. Without it, a function that always raised would pass every test below."""
    assert _tokenize_smiles("O=C(O)c1ccccc1") == "O = C ( O ) c 1 c c c c c 1"
    # Bracketed atoms are one token each, which is how every element outside the organic subset
    # reaches this model at all.
    assert _tokenize_smiles("CC[Fe]C") == "C C [Fe] C"


#: The two ways RDKit writes a stereodefined double bond, built with `chr(92)` so the backslash
#: cannot change meaning between raw and non-raw string literals.
BACKSLASH = chr(92)
TRANS_ALKENE = "C/C=C/C"
CIS_ALKENE = "C/C=C" + BACKSLASH + "C"


@pytest.mark.parametrize("smiles", [TRANS_ALKENE, CIS_ALKENE])
def test_both_halves_of_a_stereodefined_double_bond_are_covered(smiles: str) -> None:
    """Both bond-direction characters of a stereodefined double bond round-trip.

    Parametrised over both directions: a pattern covering the forward slash but not the backslash
    passes a test using only the common form, while half the alkenes RDKit emits would be refused
    by a predictor whose caller drops a raising model silently.
    """
    assert "".join(TOKEN_PATTERN.findall(smiles)) == smiles
    assert _tokenize_smiles(smiles) == " ".join(smiles)


@pytest.mark.parametrize(
    ("smiles", "dropped"),
    [
        # Selenium written outside brackets: `Se` matches `S` and the `e` is lost, so a selenoether
        # is handed to the model as a thioether. Measured against this pattern.
        ("CCSeC", "CCSC"),
        # The three-digit ring-closure form loses its `%`.
        ("C%(123)CC", "C(123)CC"),
        # Anything the pattern simply has no branch for.
        ("CCcCKC", "CCcCC"),
    ],
)
def test_a_structure_the_pattern_does_not_cover_is_refused_not_silently_shortened(
    smiles: str, dropped: str
) -> None:
    """The refusal, and the evidence that the alternative is silence rather than an error.

    `dropped` is what the tokenizer would have sent to the model had the check not been there —
    asserted here so the parametrisation cannot decay into "some strings raise".
    """
    assert "".join(TOKEN_PATTERN.findall(smiles)) == dropped

    with pytest.raises(ValueError) as raised:
        _tokenize_smiles(smiles)
    assert smiles in str(raised.value)


def test_the_refusal_does_not_echo_a_megastring_whole() -> None:
    """The refusal message is bounded by `mcp_server_kit.limits.echo`, as every refusal here is.

    This message only reaches the log (`tools._survivors` reports a failed predictor by exception
    type), and the bound keeps a megastring from making that log line unreadable.
    """
    payload = "K" * 50_000
    with pytest.raises(ValueError) as raised:
        _tokenize_smiles(payload)
    message = str(raised.value)
    assert payload not in message
    assert len(message) < 500, message
    # The length is named rather than hidden, so nothing about the size of the input is lost.
    assert "50000 chars" in message
