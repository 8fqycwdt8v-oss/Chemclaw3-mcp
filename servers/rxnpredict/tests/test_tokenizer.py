"""The Molecular Transformer tokenizer's round-trip check — the arm that used to be an `assert`.

This predictor's tokenizer is a regular expression, and a character it does not cover is not
reported: `re.findall` simply returns fewer tokens. The consequence is the worst shape a chemistry
tool has — a confident answer about a *different molecule* — so the round trip that catches it is
the whole safety of the function and has to survive a `python -O` process, which an `assert` does
not.

The module is imported directly rather than through the predictor registry: it registers itself
only when `onmt` is installed, which is an optional extra no test environment here carries, and the
tokenizer is a module-level function that needs none of it.
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
    """The message is bounded by the same `truncate_echo` every other refusal in this server uses.

    An `AssertionError`'s message is written as a debugging aid, so the version this replaced
    interpolated the caller's SMILES raw. `connector_app` keeps an `AssertionError` away from the
    model, but not away from the log — and this refusal is a `ValueError`, which reaches both.
    """
    payload = "K" * 50_000
    with pytest.raises(ValueError) as raised:
        _tokenize_smiles(payload)
    message = str(raised.value)
    assert payload not in message
    assert len(message) < 500, message
    # The length is named rather than hidden, so nothing about the size of the input is lost.
    assert "50000 chars" in message
