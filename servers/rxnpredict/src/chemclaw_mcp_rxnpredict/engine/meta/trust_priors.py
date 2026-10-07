"""Per-model and per-class trust priors: the weights every vote in the aggregator is scaled by.

Per-class gating matters because a predictor's overall benchmark says little about a specific class.
The priors are a vendored dataset read through `mcp_server_kit.load_dataset`, so a swapped or
truncated file fails the `/healthz` probe with both hashes. Calibration is
`scripts/calibrate_rxnpredict_priors.py`, run outside the serving image and reviewed in a pull
request.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

from mcp_server_kit import Dataset, load_dataset

from chemclaw_mcp_rxnpredict.engine.meta.classifier import CLASS_OTHER

logger = logging.getLogger(__name__)

PRIORS_FILE = "trust_priors.json"


@lru_cache(maxsize=1)
def priors_dataset(directory: Path) -> Dataset:
    """The vendored `trust_priors.json`, checksum-verified. Cached: the checksum is paid once.

    Separate so `/healthz` can name the table's version without re-hashing.
    """
    return load_dataset(directory, records_file=PRIORS_FILE)


def _coerce(data: object, source: str) -> dict[str, dict[str, float]]:
    """Coerce parsed JSON into the `{class: {model: weight}}` shape, or warn and return empty."""
    if not isinstance(data, dict):
        logger.warning("trust priors at %s are not a JSON object; ignoring", source)
        return {}
    return {
        str(klass): {str(model): float(weight) for model, weight in (weights or {}).items()}
        for klass, weights in data.items()
    }


@lru_cache(maxsize=1)
def load_vendored_priors(directory: Path) -> dict[str, dict[str, float]]:
    """The per-class priors shipped with this server, verified against their checksum.

    Cached, because `Settings.class_priors()` calls it on every aggregation.

    Args:
        directory: The server's `data/` directory — `dataset.json` plus `trust_priors.json`.

    Returns:
        `{reaction_class: {model_name: weight}}`. Empty when no calibration has been run (the
        shipped state); the aggregator then uses the global priors.

    Raises:
        DatasetError: The file is missing, unlisted, or not the approved one. Fatal on purpose,
        since a silently defaulted weight changes every answer; raised here rather than at import so
        `/healthz` answers 503.
    """
    dataset = priors_dataset(directory)
    priors = _coerce(json.loads(dataset.records_path.read_text(encoding="utf-8")), str(directory))
    if priors:
        logger.info("loaded per-class trust priors for %d reaction classes", len(priors))
    return priors


def load_priors_file(path: Path) -> dict[str, dict[str, float]]:
    """Read a priors JSON file directly, with no checksum. For the calibration script only."""
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

    Falls back per-class → global → `default`. `CLASS_OTHER` never selects a per-class weight.
    """
    if reaction_class and reaction_class != CLASS_OTHER:
        class_map = per_class_priors.get(reaction_class)
        if class_map and model_name in class_map:
            return class_map[model_name]
    return global_priors.get(model_name, default)
