"""SMILES canonicalisation and reaction parsing utilities."""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


def canonical_smiles(smiles: str) -> str:
    """Return canonical SMILES via RDKit. Raises ValueError if invalid."""
    from rdkit import Chem  # local import: RDKit is heavy

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError(f"Invalid SMILES: {smiles!r}")
    return Chem.MolToSmiles(mol)


def canonical_multi_smiles(dot_separated: str) -> str:
    """Canonicalise a dot-separated list of SMILES and return them sorted for reproducibility."""
    parts = [p.strip() for p in dot_separated.split(".") if p.strip()]
    canon = sorted(canonical_smiles(p) for p in parts)
    return ".".join(canon)


_RXN_RE = re.compile(r"^(?P<reactants>[^>]*)>(?P<agents>[^>]*)>(?P<products>[^>]*)$")


def parse_reaction(text: str) -> tuple[str, str, str]:
    """Split a reaction SMILES `reactants>agents>products` into its three parts.

    Accepts shortened forms where the user passed only reactants (no `>`); in that
    case agents and products are empty strings.
    """
    if ">" not in text:
        return text, "", ""
    match = _RXN_RE.match(text)
    if not match:
        raise ValueError(f"Malformed reaction SMILES: {text!r}")
    return match.group("reactants"), match.group("agents"), match.group("products")


def build_reaction_smiles(reactants: str, product: str, agents: str = "") -> str:
    """Build a canonical reaction SMILES `R>A>P` with each side canonicalised."""
    r = canonical_multi_smiles(reactants)
    a = canonical_multi_smiles(agents) if agents.strip() else ""
    p = canonical_multi_smiles(product)
    return f"{r}>{a}>{p}"


def canonical_reaction_input(s: str) -> str:
    """Canonicalise a possibly `>`-segmented reaction input string.

    Splits on `>`, canonicalises the dot-separated SMILES in each segment, and
    rejoins preserving the segment structure. Empty segments stay empty.

    Unlike taking only `s.split(">")[0]`, this keeps reagent/agent context
    distinct so two chemically different inputs (e.g. `A.B>reagent>` vs `A.B`)
    do not collapse to the same string. Raises ValueError if any segment is
    invalid.
    """
    segments = s.split(">")
    canon = [canonical_multi_smiles(seg.strip()) if seg.strip() else "" for seg in segments]
    return ">".join(canon)
