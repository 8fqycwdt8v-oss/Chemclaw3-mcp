"""The prose about which binaries this image carries, checked against the image that carries them.

A tool docstring is the prompt: telling the agent the image ships no `crest` while the
`Containerfile` installs it talks the agent out of the tautomer search. The module docstrings
about the `xtb` binary are swept too.

The phrases checked are literal ones that were wrong; both halves are read from the repository,
so restoring either side of the contradiction fails.
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
    """The refusal path stays, worded for a deployment that trimmed the binary from the image."""
    from chemclaw_mcp_calc.engine import crest_search

    assert crest_search.require_crest.__doc__
    source = (PACKAGE / "engine" / "crest_search.py").read_text()
    assert "The shipped image " in source and "replaced or trimmed that image" in source
