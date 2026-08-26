"""The prose about which binaries this image carries, checked against the image that carries them.

**A tool docstring is the prompt.** Argument names, defaults and this prose are what an agent reads
before deciding whether to call a tool, and two of them told it, in bold, that the image ships no
`crest` and that the call would refuse — while `Containerfile` installs crest 3.0.2 and xtb 6.7.1
and `crest_search.require_crest` says the opposite in its own message. The consequence is not a
wrong number, it is a capability described as unavailable: by those same docstrings the tautomer
search is "the search that matters most … a pKa, a Fukui ranking and a reaction energy all describe
whichever tautomer was drawn", so the tool that removes the largest silent error on this server was
the one being talked out of.

The three `xtb`-binary paragraphs are the same staleness with a smaller blast radius — they are
module docstrings rather than prompts — and they are swept here too, because a reader who finds one
false claim has no way to know which of the others still holds.

This checks the claims that were actually made rather than every claim that could be: the phrases
below are literal, taken from the text that was wrong. A test is evidence about a known failure, not
a proof that prose is true — what makes it worth keeping is that both halves are read from the
repository, so restoring either half of the contradiction fails.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "src" / "chemclaw_mcp_calc"
CONTAINERFILE = Path(__file__).resolve().parent.parent / "Containerfile"

# Sentences asserting that a binary is missing from the image. Each was in this package while the
# `Containerfile` installed the binary it denied.
DENIALS = re.compile(
    r"does not ship"
    r"|does \*\*not\*\* carry"
    r"|no `xtb` binary at all"
    r"|absent from the shipped image"
    r"|which is the shipped image here"
    r"|which is the shipped default here"
    r"|shipped image, which carries no binary"
)


@pytest.mark.parametrize("binary", ["crest", "xtb"])
def test_the_image_installs_the_binary_the_prose_is_about(binary: str) -> None:
    """The premise of the sweep below, asserted rather than assumed.

    If a future image genuinely stops shipping one of these, this fails first and names it — which
    is the signal to rewrite the prose in the other direction, not to delete the check.
    """
    assert re.search(rf'"{binary}=\$\{{[A-Z_]+}}"', CONTAINERFILE.read_text()), (
        f"{binary} is no longer installed by the Containerfile; every docstring describing its "
        "availability now says the wrong thing in the other direction"
    )


def test_no_module_tells_a_caller_the_image_lacks_a_binary_it_installs() -> None:
    """The sweep.

    A false docstring misleads the model directly; a false comment misleads the next author.
    """
    offences = [
        f"{path.relative_to(PACKAGE)}:{number}: {line.strip()}"
        for path in sorted(PACKAGE.rglob("*.py"))
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if DENIALS.search(line)
    ]
    assert not offences, "prose claiming the image lacks a binary it ships:\n" + "\n".join(offences)


def test_the_refusal_a_trimmed_deployment_gets_is_still_worded_for_one() -> None:
    """The refusal path stays, and says what it means: the image carries it, this deployment does
    not.

    Rewriting the docstrings must not turn into deleting the refusal — an operator who trims the
    image is exactly who needs it, and `require_crest`'s message is the one that already gets this
    right.
    """
    from chemclaw_mcp_calc.engine import crest_search

    assert crest_search.require_crest.__doc__
    source = (PACKAGE / "engine" / "crest_search.py").read_text()
    assert "The shipped image " in source and "replaced or trimmed that image" in source
