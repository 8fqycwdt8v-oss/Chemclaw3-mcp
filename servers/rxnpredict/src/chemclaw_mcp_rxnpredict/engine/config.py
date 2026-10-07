"""Runtime configuration for `rxnpredict`.

Every variable is prefixed `CHEMCLAW_RXNPREDICT_`. The model directory is read, never created:
weights are baked into a read-only image. Per-class trust priors are a vendored dataset with a
licence and checksum (`data/trust_priors.json`), read lazily by `Settings.class_priors()`.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

from mcp_server_kit.limits import echo, report_settings
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

#: Per-model trust priors, seeded from each predictor's published benchmarks. Read-only, so the
#: table every `Settings` starts from cannot be edited in place.
DEFAULT_MODEL_TRUST_PRIORS: Mapping[str, float] = MappingProxyType(
    {
        # Forward
        "reaction_t5_v2": 1.00,  # ~97.5% top-1 USPTO-MIT (Sagawa 2024)
        "molecular_transformer": 0.90,  # ~90% top-1
        "t5chem": 0.92,
        "chemformer": 0.85,
        "megan": 0.80,
        "graphrxn": 0.80,
        # Conditions
        "parrot": 0.95,  # +13.44% top-3 over the Coley baseline
        "rxn_insight": 0.70,  # rule-based: fast, coarse
        "two_stage_dnn": 0.90,  # 73% top-10 exact match
        "reagents_mt": 0.80,
        "askcos_condition": 0.85,
    }
)


def _predictor_weights(supplied: object, field: str) -> dict[str, float]:
    """`supplied` as `{predictor id: weight}`, or a `ValueError` naming what it cannot mean.

    Shared by the global and per-class tables. Each key must be a predictor
    `DEFAULT_MODEL_TRUST_PRIORS` weights (a typo would set nothing), and each weight finite and
    above zero (zero silences a voter, a negative inverts it).
    """
    if not isinstance(supplied, dict):
        raise ValueError(
            f"{field} must be a JSON object of predictor id to weight, "
            f"not {type(supplied).__name__}"
        )
    unknown = sorted(str(name) for name in set(supplied) - set(DEFAULT_MODEL_TRUST_PRIORS))
    if unknown:
        raise ValueError(
            f"{field} names {echo(repr(unknown))}, which this server does not "
            f"weight; the predictors it does are {sorted(DEFAULT_MODEL_TRUST_PRIORS)}"
        )
    weights: dict[str, float] = {}
    for name, weight in supplied.items():
        if isinstance(weight, bool) or not isinstance(weight, int | float):
            raise ValueError(f"{field}[{echo(name)!r}] must be a number, not {echo(repr(weight))}")
        if not (math.isfinite(weight) and weight > 0):
            raise ValueError(
                f"{field}[{echo(name)!r}] must be finite and above zero, not {weight!r}; "
                "to stop a predictor voting, name it in CHEMCLAW_RXNPREDICT_DISABLED_MODELS"
            )
        weights[str(name)] = float(weight)
    return weights


class Settings(BaseSettings):
    """Server configuration, from `CHEMCLAW_RXNPREDICT_*` environment variables.

    Predictor selection is comma-separated, and `*` means "every predictor that registered":

        CHEMCLAW_RXNPREDICT_ENABLED_FORWARD_MODELS=reaction_t5_v2
        CHEMCLAW_RXNPREDICT_DISABLED_MODELS=megan
    """

    model_config = SettingsConfigDict(
        env_prefix="CHEMCLAW_RXNPREDICT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Model selection ---
    enabled_forward_models: str = Field(default="*", description="Comma list or '*' for all.")
    enabled_conditions_models: str = Field(default="*", description="Comma list or '*' for all.")
    disabled_models: str = Field(
        default="", description="Comma list of predictor IDs to force off."
    )

    # --- Baked model weights ---
    # Read-only: the path the Containerfile bakes, or a mounted read-only volume. Never created or
    # written.
    model_dir: Path = Field(default=DATA_DIR / "models")

    # --- Compute ---
    device: str = Field(default="auto", description="'cpu', 'cuda', 'cuda:0', or 'auto'.")
    default_top_k: int = Field(default=5, ge=1, le=50)

    # --- Prediction cache (in-process, bounded; see `cache.py`) ---
    cache_enabled: bool = Field(default=True)
    cache_max_entries: int = Field(default=2048, ge=0)

    # --- Mixture-of-experts gating ---
    use_class_priors: bool = Field(
        default=True,
        description="Use per-reaction-class trust priors in the aggregator when available.",
    )

    # Per-model trust priors for the aggregator; higher means more voting weight. A JSON env value
    # adjusts named entries of `DEFAULT_MODEL_TRUST_PRIORS` (see `_merge_priors`).
    model_trust_priors: dict[str, float] = Field(
        default_factory=lambda: dict(DEFAULT_MODEL_TRUST_PRIORS)
    )

    # Per-reaction-class adjustments, `{class: {predictor: weight}}`, laid over the vendored corpus
    # by `class_priors()`. Empty means the corpus as calibrated. Never read this field directly on a
    # serving path; use `class_priors()`.
    model_trust_priors_by_class: dict[str, dict[str, float]] = Field(default_factory=dict)

    @field_validator("model_trust_priors", mode="before")
    @classmethod
    def _merge_priors(cls, value: Any) -> Any:
        """Overlay a supplied table onto `DEFAULT_MODEL_TRUST_PRIORS`, refusing what it cannot mean.

        Named entries replace their defaults and the rest stand, so changing one weight does not
        reset the others; a full replacement names every predictor. Unknown predictors and
        non-positive or non-finite weights are refused (`DISABLED_MODELS` is how to silence one).
        Accepts a JSON string or a mapping.
        """
        supplied = json.loads(value) if isinstance(value, str) else value
        merged = dict(DEFAULT_MODEL_TRUST_PRIORS)
        merged.update(_predictor_weights(supplied, "model_trust_priors"))
        return merged

    @field_validator("model_trust_priors_by_class", mode="before")
    @classmethod
    def _parse_class_priors(cls, value: Any) -> Any:
        """Validate a per-class adjustment; `class_priors()` is what lays it over the corpus.

        The same rules as `_merge_priors`, per class. Every label must be one of
        `classifier.ALL_CLASSES` other than `CLASS_OTHER` (which never gets a per-class weight). The
        corpus is not read here, so a checksum failure cannot crash settings load at import.
        """
        from chemclaw_mcp_rxnpredict.engine.meta.classifier import ALL_CLASSES, CLASS_OTHER

        supplied = json.loads(value) if isinstance(value, str) else value
        if not isinstance(supplied, dict):
            raise ValueError(
                "model_trust_priors_by_class must be a JSON object of reaction class to "
                f"{{predictor id: weight}}, not {type(supplied).__name__}"
            )
        selectable = ALL_CLASSES - {CLASS_OTHER}
        unknown = sorted(str(label) for label in supplied if label not in selectable)
        if unknown:
            raise ValueError(
                f"model_trust_priors_by_class names {echo(repr(unknown))}, which is not a class "
                f"a per-class prior is ever read for; the classes are {sorted(selectable)}"
            )
        return {
            label: _predictor_weights(weights, f"model_trust_priors_by_class[{label!r}]")
            for label, weights in supplied.items()
        }

    def parse_enabled(self, raw: str) -> set[str] | None:
        """`None` for `*` or empty (meaning "all"), otherwise the named predictor IDs."""
        stripped = raw.strip()
        if stripped in {"*", ""}:
            return None
        return {part.strip() for part in stripped.split(",") if part.strip()}

    def class_priors(self) -> dict[str, dict[str, float]]:
        """The per-reaction-class trust priors this process ranks by: the override over the corpus.

        The corpus is read here, not in `get_settings`, so a corrupt `trust_priors.json` fails the
        readiness probe (`app.py`) rather than the import of `tools.py`. Each `(class, predictor)`
        pair in the override replaces that calibrated weight; everything else stands. Returns a new
        dict every call, so the cached corpus is never edited.

        Returns:
            `{reaction_class: {model_name: weight}}`; empty when no calibration and no override
                exist, in which case the aggregator uses the global priors.
        """
        from chemclaw_mcp_rxnpredict.engine.meta.trust_priors import load_vendored_priors

        corpus = load_vendored_priors(DATA_DIR)
        if not self.model_trust_priors_by_class:
            return corpus
        merged = {label: dict(weights) for label, weights in corpus.items()}
        for label, weights in self.model_trust_priors_by_class.items():
            merged.setdefault(label, {}).update(weights)
        return merged

    def parse_disabled(self) -> set[str]:
        """The predictor IDs forced off, whatever the enabled list says."""
        return {part.strip() for part in self.disabled_models.split(",") if part.strip()}

    def resolve_device(self) -> str:
        """`device`, with `auto` resolved to CUDA when torch can see a GPU."""
        if self.device != "auto":
            return self.device
        try:
            import torch
        except ImportError:
            return "cpu"
        return "cuda" if torch.cuda.is_available() else "cpu"


def inference_threads() -> int:
    """Cores one predictor's forward pass may spend, which is what the admission gate charges it.

    Read from `torch.get_num_threads()`, which torch sizes from the node rather than the cgroup; the
    image pins `OMP_NUM_THREADS=1`, but a deployment may raise it. `1` without torch, and never
    below `1`, so the tool is always counted. On CUDA this still charges the host threads, which is
    what this CPU ceiling sees.
    """
    try:
        import torch
    except ImportError:
        return 1
    return max(1, int(torch.get_num_threads()))


_settings: Settings | None = None


def get_settings() -> Settings:
    """The process-wide settings: the environment, parsed once, and nothing read off disk.

    Called at import of `tools.py`, so it must never touch a vendored corpus; that is
    `Settings.class_priors()`'s job, run by the readiness check.
    """
    global _settings
    if _settings is None:
        _settings = Settings()
        # Its numbers, for `/healthz`.
        report_settings(_settings)
    return _settings


def reset_settings_for_tests() -> None:
    """Force a fresh `Settings`, and a fresh corpus read, on the next `get_settings()`.

    The priors are cached beside the settings, so both are reset.
    """
    global _settings
    _settings = None
    from chemclaw_mcp_rxnpredict.engine.meta.trust_priors import (
        load_vendored_priors,
        priors_dataset,
    )

    load_vendored_priors.cache_clear()
    priors_dataset.cache_clear()
