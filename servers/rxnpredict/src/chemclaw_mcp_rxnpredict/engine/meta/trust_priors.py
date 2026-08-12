"""Per-model and per-class trust priors: the weights every vote in the aggregator is scaled by.

A trust prior says how much this server believes a given predictor, optionally per reaction class —
`reaction_t5_v2` is excellent on USPTO-MIT overall and that says little about how it does on a
Suzuki coupling specifically, which is the whole point of gating by class.

**The priors are a vendored dataset here, not a file in a home directory.** Upstream wrote them to
`~/.cache/chemclaw2_forward/trust_priors.json` and loaded them on startup if present, which meant
the numbers driving every ranking had no licence, no checksum, and no record of which calibration
run produced them. They are now read through `mcp_server_kit.load_dataset`, so a swapped or
truncated file fails at startup with both hashes in the message.

Calibration itself stays where it belongs — `scripts/calibrate_rxnpredict_priors.py`, run by a
person outside the serving image, whose output is reviewed in a pull request.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from mcp_server_kit import load_dataset

from chemclaw_mcp_rxnpredict.engine.meta.classifier import CLASS_OTHER

logger = logging.getLogger(__name__)

PRIORS_FILE = "trust_priors.json"


def _coerce(data: object, source: str) -> dict[str, dict[str, float]]:
    """Coerce parsed JSON into the `{class: {model: weight}}` shape, or warn and return empty."""
    if not isinstance(data, dict):
        logger.warning("trust priors at %s are not a JSON object; ignoring", source)
        return {}
    return {
        str(klass): {str(model): float(weight) for model, weight in (weights or {}).items()}
        for klass, weights in data.items()
    }


def load_vendored_priors(directory: Path) -> dict[str, dict[str, float]]:
    """The per-class priors shipped with this server, verified against their checksum.

    Args:
        directory: The server's `data/` directory — `dataset.json` plus `trust_priors.json`.

    Returns:
        `{reaction_class: {model_name: weight}}`. Empty when no calibration has been run, which is
        the shipped state: the aggregator then falls back to the global priors in `Settings`.

    Raises:
        DatasetError: the file is missing, unlisted, or not the one the manifest approved. This is
            deliberately fatal — a ranking weight that silently reverted to a default is a change
            in every answer nobody would notice.
    """
    dataset = load_dataset(directory, records_file=PRIORS_FILE)
    return _coerce(json.loads(dataset.records_path.read_text(encoding="utf-8")), str(directory))


def load_priors_file(path: Path) -> dict[str, dict[str, float]]:
    """Read a priors JSON file directly, with no checksum. For the calibration script only.

    The serving path uses `load_vendored_priors`; this exists so the script can read back what it
    just wrote without a `dataset.json` having been regenerated yet.
    """
    if not path.exists():
        return {}
    try:
        return _coerce(json.loads(path.read_text(encoding="utf-8")), str(path))
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("could not parse %s: %s", path, exc)
        return {}


def save_priors_file(path: Path, priors: dict[str, dict[str, float]]) -> None:
    """Write a priors file. Used by the calibration script, never by the server."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(priors, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def effective_prior(
    model_name: str,
    reaction_class: str | None,
    global_priors: dict[str, float],
    per_class_priors: dict[str, dict[str, float]],
    *,
    default: float = 0.5,
) -> float:
    """The most specific prior available for `(model_name, reaction_class)`.

    Falls back through per-class → global → `default`. `CLASS_OTHER` never selects a per-class
    weight, because "we could not classify this reaction" is not a class a prior can be about.
    """
    if reaction_class and reaction_class != CLASS_OTHER:
        class_map = per_class_priors.get(reaction_class)
        if class_map and model_name in class_map:
            return class_map[model_name]
    return global_priors.get(model_name, default)
