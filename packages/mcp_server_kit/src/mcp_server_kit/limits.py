"""A structural-size bound every server applies before it canonicalises a SMILES.

**RDKit's `MolToSmiles` and the tautomer canonicalizer recurse over the molecular graph, and a
large enough linear molecule overflows the C stack** — the process dies with SIGSEGV (exit 139),
which no `try`/`except` in Python can catch. Measured: `MolToSmiles(MolFromSmiles("C" * 20000))`
segfaults, while the *parse* that produced the molecule returns normally in ~40 ms. So one ~20 KB
authenticated tool call takes the whole pod down, and with it every other session sharing it — a
denial of service that costs the caller one request.

The defence has to sit **before** any canonicalisation and it is the same in four servers
(`chem`, `safety`, `rxnlabel`, `rxnpredict`), so it lives here once. Two independent bounds, both
config-driven (a bound written as a magic number is one nobody can loosen for a real megamolecule
without editing code):

- **`MAX_SMILES_CHARS`** — a cheap guard applied to the raw string *before* it is parsed, so a
  pathological megastring never reaches `MolFromSmiles` at all. A real reagent SMILES is tens of
  characters; the default is far above anything a process chemist submits.
- **`MAX_MOLECULE_ATOMS`** — applied to `mol.GetNumAtoms()` after a successful parse and before
  canonicalisation. This is the bound that actually stops the segfault, because the recursion depth
  scales with the atom count, not the string length.

Neither function raises: they return a *worded reason* or `None`. A server that refuses (a chemist
is waiting) raises its own `ValueError` subclass with the reason; a server that ingests a corpus
leniently (`rxnlabel`) treats a reason as "could not be read" and drops the one species. The reason
string is caller-safe — it quotes only sizes, never the offending megastring — so it is safe to
surface to the model verbatim through `connector_app`.
"""

from __future__ import annotations

import os

__all__ = [
    "MAX_MOLECULE_ATOMS",
    "MAX_SMILES_CHARS",
    "atom_count_error",
    "smiles_length_error",
]

MAX_SMILES_CHARS = int(os.environ.get("MCP_MAX_SMILES_CHARS", "4000"))
MAX_MOLECULE_ATOMS = int(os.environ.get("MCP_MAX_MOLECULE_ATOMS", "2000"))


def smiles_length_error(
    smiles: str,
    *,
    subject: str = "the structure given",
    max_chars: int = MAX_SMILES_CHARS,
) -> str | None:
    """A worded reason `smiles` is too long to parse safely, or `None` if it is within bounds.

    Applied to the raw string *before* `MolFromSmiles`, so a megastring never reaches the parser.
    The message quotes the length and the limit, never the string itself — a 500 KB SMILES echoed
    into a refusal would flood the log and the model context it is meant to protect.
    """
    if len(smiles) > max_chars:
        return (
            f"{subject} is {len(smiles)} characters, above the {max_chars}-character limit. A real "
            "reagent SMILES is far shorter; this bound stops a pathological string from exhausting "
            "the canonicaliser."
        )
    return None


def atom_count_error(
    num_atoms: int,
    *,
    subject: str = "the structure given",
    max_atoms: int = MAX_MOLECULE_ATOMS,
) -> str | None:
    """A worded reason a molecule of `num_atoms` atoms is too large, or `None` if within bounds.

    Applied after a successful parse and **before** any `MolToSmiles`/tautomer canonicalisation,
    which recurse over the graph and overflow the C stack (an uncatchable SIGSEGV) on a large
    linear molecule. `max_atoms` is far above any real reagent.
    """
    if num_atoms > max_atoms:
        return (
            f"{subject} has {num_atoms} atoms, above the {max_atoms}-atom limit. Canonicalising a "
            "molecule this large can overflow the underlying C library's stack and crash the "
            "server; no real reagent approaches this size."
        )
    return None
