"""Reading a species string, whole or not at all.

**The parser is lenient in the one direction that matters here.** RDKit treats whitespace as the
end of a structure and ignores the rest, so `"CCO junk"` is ethanol and `"CCO (2 vol)"` is ethanol —
a malformed or concatenated string does not fail, it narrows to a *different, smaller molecule* than
the caller submitted. It also skips a run of non-ASCII bytes at a string's edges, so `"°C"` is
methane: prose is what produces that, and an ELN cell reading `` `80 °C` `` offers it as a
structure.

This server is the one that eats a multi-million-row corpus of ELN and patent-extracted free text —
which is to say it is the one place concatenated strings actually come from — so it is the last
place that should parse leniently. The rules are the sister `chem` server's
(`chemclaw_mcp_chem.engine.chem.require_molecule`), transcribed rather than imported because the two
are separate processes with separate dependency closures.

**What differs is the answer to a bad string, and deliberately.** `chem` raises, because a chemist
typed the string and is waiting. Here nothing raises: a patent extract's fiftieth species may be an
OCR artefact and losing the other forty-nine over it is a worse answer than losing that one's
(`roles._canonical_set` records the same argument). So this returns `None`, and every caller must
keep "could not be read" distinguishable from "read it, and it carries nothing" — an empty group
list stored for an unreadable species is counted as a negative by every later query.
"""

from __future__ import annotations

from rdkit import Chem

__all__ = ["read_molecule"]


def read_molecule(smiles: str) -> Chem.Mol | None:
    """The parsed molecule, or `None` unless RDKit reads `smiles` **whole**.

    Surrounding whitespace is stripped rather than refused: a leading newline is a copy-paste
    artefact, not a second molecule. Whitespace *inside* the string is refused, because that is the
    silent-truncation case; so is a non-ASCII character, tested on the string rather than on the
    parsed molecule, since once RDKit has skipped the character nothing about the molecule says it
    was ever there.
    """
    stripped = smiles.strip()
    if not stripped or any(character.isspace() for character in stripped):
        return None
    if not stripped.isascii():
        return None
    mol = Chem.MolFromSmiles(stripped)
    return mol if mol is not None and mol.GetNumAtoms() > 0 else None
